import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem import cli, consolidate
from lrdmem.lint import lint
from lrdmem.store import Memory, Store
from lrdmem.sync import sync
from tests.fixtures import make_store

W = "acme/web/"
X = W + "experiences/"


def exp(name, kind="discovery", agent="claude", device="mac", date="2026-10-20T10:00:00Z"):
    head = f"id: {name}\ndate: {date}\nkind: {kind}\nagent: {agent}\ndevice: {device}\nsource: chat\n"
    return X + name + ".md", f"---\n{head}---\n\nwhat happened\n"


def mem(name, state=None, mtype=None, evidence=(), created="2026-10-01", desc=None, body=None, folder=W, **meta):
    m = {"type": "feedback", "author": "me", "created": created, "updated": created,
         **({"memory_type": mtype} if mtype else {}), **({"state": state} if state else {}), **meta}
    lines = "".join(f"  {k}: {v}\n" for k, v in m.items())
    ev = ("evidence: [" + ", ".join(f"{{ref: {r}, kind: {k}, weight: {w}}}" for r, k, w in evidence) + "]\n"
          if evidence else "")
    return (folder + name + ".md",
            f"---\nname: {name}\ndescription: {desc or 'About ' + name}\nmetadata:\n{lines}{ev}---\n\n"
            f"{body or name + ' body text'}\n")


def edge(name, source, target, type="related_to", state="proposed", evidence=()):
    ev = ("evidence: [" + ", ".join(f"{{ref: {r}, kind: {k}, weight: {w}}}" for r, k, w in evidence) + "]\n"
          if evidence else "")
    return (W + "edges/" + name + ".md",
            f"---\nid: {name}\nsource: {source}\ntarget: {target}\ntype: {type}\nstate: {state}\n{ev}---\n")


def problems(store: Store) -> list[str]:
    """Lint problems other than the fixture's own shortcuts (memories not indexed, no Why/How lines)."""
    return [p for p in lint(store) if "not listed in MEMORY.md" not in p and "**Why:**" not in p]


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


BASE = {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": ""}


class _Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        (self.tmp / "tmpdir").mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmp / "tmpdir"))  # the lock lives here
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, *files) -> Store:
        self.store = Store(make_store(self.tmp, {**BASE, **dict(files)}))
        return self.store

    def mem(self, ref) -> Memory:
        node = Store(self.store.root).node(ref)
        assert isinstance(node, Memory), ref
        return node

    def revs(self, ref=None):
        revs = [(r.action, r.after) for r in Store(self.store.root).revisions() if ref in (None, r.target)]
        return sorted(revs, key=lambda x: (x[0], sorted(x[1].items())))

    def queue(self):
        return {(q.kind, tuple(q.subjects)): q for q in Store(self.store.root).queue()}


class PassesTest(_Base):
    def test_valid_until_past_becomes_obsolete(self):
        self.build(mem("old", valid_until="2020-01-01"), mem("later", valid_until="2999-01-01"),
                   mem("gone", state="superseded", valid_until="2020-01-01"))
        r = consolidate.run(self.store)
        self.assertEqual(r.obsoleted, [W + "old"])
        self.assertEqual(self.mem(W + "old").state, "obsolete")
        self.assertEqual(self.mem(W + "later").state, "active")
        self.assertEqual(self.mem(W + "gone").state, "superseded")
        self.assertEqual(self.revs(W + "old"), [("state_change", {"state": "obsolete"})])

    def test_exact_duplicates_merge_into_the_oldest(self):
        self.build(exp("e1"), exp("e2", agent="codex"),
                   mem("a", created="2026-10-05", evidence=[(X + "e1", "supports", 1.0)]),
                   mem("b", created="2026-10-02", desc="ABOUT  same", body="Same\n  body", evidence=[(X + "e2", "observed_in", 1.0)]),
                   mem("c", created="2026-10-09", desc="about same", body="same body", evidence=[(X + "e1", "supports", 1.0)]),
                   mem("d", desc="about same", body="same body", folder="acme/_global/"))
        r = consolidate.run(self.store)
        self.assertEqual(r.merged, [W + "c"])
        self.assertEqual(self.mem(W + "c").state, "superseded")
        self.assertEqual(self.mem("acme/_global/d").state, "active")  # another scope: never merged
        self.assertEqual(sorted((e.ref, e.kind) for e in self.mem(W + "b").evidence),
                         [(X + "e1", "supports"), (X + "e2", "observed_in")])
        es = [e for e in Store(self.store.root).edges() if e.type == "supersedes"]
        self.assertEqual([(e.source, e.target, e.state) for e in es], [(W + "b", W + "c", "validated")])
        self.assertEqual(self.revs(W + "b"), [("merge", {"evidence": W + "c"})])
        self.assertEqual(self.revs(W + "c"), [("supersede", {"state": "superseded"})])
        self.assertEqual(self.mem(W + "a").state, "active")
        self.assertEqual(problems(Store(self.store.root)), [])

    def test_promotion_counts_independent_supports_only(self):
        self.build(exp("s1", date="2026-10-20T10:00:00Z"), exp("s2", date="2026-10-20T18:00:00Z"),
                   exp("o1", agent="codex"), exp("f1", kind="feedback", agent="opencode"),
                   mem("same", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 1.0)]),
                   mem("obs", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "o1", "observed_in", 1.0)]),
                   mem("fake", state="candidate", evidence=[(X + "s1", "supports", 1.0), ("https://x.test/y", "supports", 1.0)]),
                   mem("two", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "f1", "supports", 0.5)]),
                   mem("hyp", state="candidate", mtype="hypothesis",
                       evidence=[(X + "s1", "supports", 1.0), (X + "o1", "supports", 1.0)]),
                   mem("fought", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "o1", "supports", 1.0)]),
                   mem("rival"),
                   edge("c1", W + "rival", W + "fought", "contradicts", "validated"))
        r = consolidate.run(self.store)
        self.assertEqual(r.promoted, [W + "two"])
        for name in ("same", "obs", "fake", "hyp", "fought"):
            self.assertEqual(self.mem(W + name).state, "candidate", name)
        self.assertEqual(self.mem(W + "two").state, "active")
        self.assertEqual(self.revs(W + "two"), [("state_change", {"state": "active"})])

    def test_hypothesis_with_outcome_is_queued_never_promoted(self):
        self.build(exp("o", kind="outcome"), exp("k", kind="check", agent="hook"), exp("d", agent="codex"),
                   mem("h1", state="candidate", mtype="hypothesis", evidence=[(X + "o", "supports", 1.0)]),
                   mem("h2", state="candidate", mtype="hypothesis", evidence=[(X + "k", "supports", 1.0)]),
                   mem("h3", state="candidate", mtype="hypothesis",
                       evidence=[(X + "d", "supports", 1.0), (X + "o", "observed_in", 1.0)]))
        r = consolidate.run(self.store)
        q = self.queue()
        self.assertEqual(set(q), {("promote-hypothesis", (W + "h1",)), ("promote-hypothesis", (W + "h2",))})
        item = q[("promote-hypothesis", (W + "h1",))]
        self.assertEqual(item.id, consolidate.item_id("promote-hypothesis", [W + "h1"]))
        self.assertEqual(item.path, self.store.root / W / "queue" / f"{item.id}.md")
        self.assertEqual((item.status, item.data["resolution"]), ("open", ""))
        self.assertNotIn("lrdmem ", item.body)  # agent-facing data: no commands
        self.assertEqual(sorted(r.queued), sorted(i.id for i in q.values()))
        self.assertEqual({self.mem(W + h).state for h in ("h1", "h2", "h3")}, {"candidate"})
        self.assertEqual(problems(Store(self.store.root)), [])

    def test_contested_by_refutes_and_restored_when_supports_dominate(self):
        files = [exp("f1", kind="feedback", agent="a1"), exp("f2", kind="feedback", agent="a2"),
                 exp("s1", agent="a3"), exp("s2", agent="a4"), exp("s3", agent="a5")]
        self.build(*files,
                   mem("one", evidence=[(X + "f1", "refutes", 0.5)]),
                   mem("two", evidence=[(X + "f1", "refutes", 0.5), (X + "f2", "refutes", 0.5)]),
                   mem("held", evidence=[(X + "s1", "supports", 1.0), (X + "f1", "refutes", 0.5),
                                         (X + "f2", "refutes", 0.5)]))
        r = consolidate.run(self.store)
        self.assertEqual(r.contested, [W + "two"])  # one negative feedback never changes state
        self.assertEqual(self.mem(W + "one").state, "active")
        self.assertEqual(self.mem(W + "held").state, "active")
        p = self.store.root / W / "two.md"
        p.write_text(p.read_text().replace("evidence: [", f"evidence: [{{ref: {X}s1, kind: supports, weight: 1.0}}, "
                                                          f"{{ref: {X}s2, kind: supports, weight: 1.0}}, "))
        r = consolidate.run(self.store)
        self.assertEqual(r.restored, [W + "two"])
        self.assertEqual(self.revs(W + "two"), [("state_change", {"state": "active"}),
                                                ("state_change", {"state": "contested"})])

    def test_contested_by_a_stronger_contradicting_memory(self):
        self.build(exp("s1", agent="a1"), exp("s2", agent="a2"),
                   mem("strong", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 1.0)]),
                   mem("weak", evidence=[(X + "s1", "supports", 1.0)]),
                   mem("cand", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 0.9)]),
                   mem("solid", evidence=[(X + "s1", "supports", 0.1)]),
                   edge("c1", W + "strong", W + "weak", "contradicts"),
                   edge("c2", W + "cand", W + "solid", "contradicts", "invalidated"))
        r = consolidate.run(self.store)
        self.assertEqual(r.contested, [W + "weak"])
        self.assertEqual(self.mem(W + "strong").state, "active")

    def test_edges_validated_with_two_independent_evidences(self):
        self.build(exp("e1", agent="a1"), exp("e2", agent="a1"), exp("e3", agent="a2", device="pc"),
                   mem("a"), mem("b"), mem("c"),
                   edge("x1", W + "a", W + "b", evidence=[(X + "e1", "observed_in", 1.0), (X + "e3", "supports", 1.0)]),
                   edge("x2", W + "a", W + "c", evidence=[(X + "e1", "observed_in", 1.0), (X + "e2", "observed_in", 1.0)]))
        r = consolidate.run(self.store)
        self.assertEqual(r.edges_validated, [W + "edges/x1"])
        self.assertEqual({e.id: e.state for e in Store(self.store.root).edges()}, {"x1": "validated", "x2": "proposed"})
        self.assertEqual(self.revs(W + "edges/x1"), [("state_change", {"state": "validated"})])

    def test_repeated_experiences_without_procedure_are_queued(self):
        ev3 = [(X + f"e{i}", "observed_in", 1.0) for i in range(3)]
        self.build(*[exp(f"e{i}", agent=f"a{i}") for i in range(3)],
                   mem("ep", mtype="episodic", evidence=ev3), mem("ep2", mtype="episodic", evidence=ev3),
                   mem("few", mtype="episodic", evidence=ev3[:2]), mem("proc", mtype="procedural"),
                   edge("g", W + "proc", W + "ep2", "generalizes"))
        consolidate.run(self.store)
        self.assertEqual(set(self.queue()), {("generalize-procedure", (W + "ep",))})

    def test_paraphrase_suspects_are_queued(self):
        text = "run composer install before phpunit in the docker container"
        self.build(mem("p1", state="active", body=text),
                   mem("p2", body="always run composer install before phpunit inside the docker container"),
                   mem("p3", state="active", body="unrelated tabs versus spaces discussion"),
                   mem("l1", state="active", body=text + " today"), mem("l2", body=text + " now"),
                   edge("k", W + "l1", W + "l2"), mem("far", body=text, folder="acme/_global/"))
        consolidate.run(self.store)
        pairs = {s for k, s in self.queue() if k == "possible-duplicate"}
        self.assertIn((W + "p1", W + "p2"), pairs)
        self.assertNotIn((W + "l1", W + "l2"), pairs)  # already linked
        self.assertFalse(any(W + "p3" in s or "acme/_global/far" in s for s in pairs))

    def test_second_run_changes_nothing(self):
        self.build(exp("s1", agent="a1"), exp("s2", agent="a2"), exp("o", kind="outcome"),
                   mem("c", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 1.0)]),
                   mem("h", state="candidate", mtype="hypothesis", evidence=[(X + "o", "supports", 1.0)]),
                   mem("old", valid_until="2020-01-01"), mem("d1", desc="dup", body="dup"),
                   mem("d2", desc="dup", body="dup", created="2026-10-09"))
        first = consolidate.run(self.store)
        self.assertTrue(first.promoted and first.queued and first.obsoleted and first.merged)
        before = snapshot(self.store.root)
        second = consolidate.run(self.store)
        self.assertEqual(snapshot(self.store.root), before)
        self.assertEqual(second, consolidate.ConsolidateResult())

    def test_budget_cut_resumes_without_duplicates(self):
        files = [exp("s1", agent="a1"), exp("s2", agent="a2"), exp("o", kind="outcome"),
                 *[mem(f"c{i}", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 1.0)])
                   for i in range(4)],
                 mem("h", state="candidate", mtype="hypothesis", evidence=[(X + "o", "supports", 1.0)]),
                 mem("old", valid_until="2020-01-01"), mem("d1", desc="dup", body="dup"),
                 mem("d2", desc="dup", body="dup", created="2026-10-09")]
        ref = self.build(*files)
        consolidate.run(ref)
        expected = snapshot(ref.root)
        (self.tmp / "second").mkdir()  # a fresh root: write.py caches the format marker per root
        st = Store(make_store(self.tmp / "second", {**BASE, **dict(files)}))
        before = snapshot(st.root)
        r = consolidate.run(st, budget=0)
        self.assertTrue(r.incomplete)
        self.assertEqual(snapshot(st.root), before)
        clock = iter(range(1000))
        with mock.patch.object(consolidate.time, "monotonic", lambda: next(clock)):
            r = consolidate.run(st, budget=4)  # cut in the middle of the passes
        self.assertTrue(r.incomplete)
        self.assertNotEqual(snapshot(st.root), before)
        r = consolidate.run(st)
        self.assertFalse(r.incomplete)
        self.assertEqual(snapshot(st.root), expected)

    def test_lock_held_skips_without_writing(self):
        self.build(mem("old", valid_until="2020-01-01"))
        before = snapshot(self.store.root)
        with consolidate.lock() as held:
            self.assertTrue(held)
            r = consolidate.run(self.store)
        self.assertTrue(r.incomplete)
        self.assertEqual(snapshot(self.store.root), before)
        self.assertTrue(consolidate.run(self.store).obsoleted)

    def test_v01_store_writes_nothing(self):
        ctx = "---\nid: {0}\ndate: 2026-0{1}-01\nagent: a{1}\nsource: chat\nmemories: [{2}]\n---\n\nx\n"
        fact = ("---\nname: {0}\ndescription: {1}\nmetadata:\n  type: project\n  author: me\n  updated: 2026-01-01\n"
                "---\n{2}\n\n**Why:** w\n**How to apply:** h\n")
        same = "run composer install before phpunit in the docker container"
        self.build(("acme/web/notes.md", fact.format("notes", "Notes", "See [[tabs]].")),
                   ("acme/web/tabs.md", fact.format("tabs", "Tabs", same)),
                   ("acme/web/tabs2.md", fact.format("tabs2", "Tabs too", same + " always")),
                   *[(f"acme/web/contexts/c{i}.md", ctx.format(f"c{i}", i, "notes")) for i in range(1, 5)])
        before = snapshot(self.store.root)
        r = consolidate.run(self.store)
        self.assertEqual(r, consolidate.ConsolidateResult())
        self.assertEqual(snapshot(self.store.root), before)
        self.assertFalse((self.store.root / ".lrdmem-format").exists())


class ResolveTest(_Base):
    def setUp(self):
        super().setUp()
        text = "run composer install before phpunit in the docker container"
        self.build(exp("o", kind="outcome"), exp("e1", agent="a1"),
                   mem("p1", state="active", created="2026-10-05", body=text, evidence=[(X + "e1", "supports", 1.0)]),
                   mem("p2", created="2026-10-02", body="always " + text),
                   mem("h", state="candidate", mtype="hypothesis", evidence=[(X + "o", "supports", 1.0)]),
                   mem("gen", state="candidate", mtype="semantic"), mem("hyp2", state="candidate", mtype="hypothesis"),
                   mem("glob", folder="_global/"))
        consolidate.run(self.store)
        q = self.queue()
        self.dup = q[("possible-duplicate", (W + "p1", W + "p2"))].id
        self.hyp = q[("promote-hypothesis", (W + "h",))].id

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LRDMEM_STORE": str(self.store.root)}), redirect_stdout(out), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            code = cli.main(["consolidate", *argv])
        return code, out.getvalue() + err.getvalue()

    def test_merge_behaves_like_fusion_and_first_resolution_wins(self):
        code, out = self.cli("--resolve", self.dup, "merge", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mem(W + "p1").state, "superseded")  # p2 is older
        self.assertIn((X + "e1", "supports"), [(e.ref, e.kind) for e in self.mem(W + "p2").evidence])
        item = self.queue()[("possible-duplicate", (W + "p1", W + "p2"))]
        self.assertEqual((item.status, item.resolution, item.data["resolved_by"]), ("resolved", "merge", "codex"))
        before = snapshot(self.store.root)
        code, out = self.cli("--resolve", self.dup, "keep-separate", "--agent", "claude")
        self.assertEqual(code, 0)
        self.assertIn("already resolved by codex at", out)
        self.assertEqual(snapshot(self.store.root), before)
        self.assertEqual(problems(Store(self.store.root)), [])

    def test_keep_separate_only_resolves(self):
        before = {p: t for p, t in snapshot(self.store.root).items() if "/queue/" not in p}
        code, _ = self.cli("--resolve", self.dup, "keep-separate", "--note", "different containers")
        self.assertEqual(code, 0)
        self.assertEqual({p: t for p, t in snapshot(self.store.root).items() if "/queue/" not in p}, before)
        item = self.queue()[("possible-duplicate", (W + "p1", W + "p2"))]
        self.assertEqual((item.status, item.resolution, item.data["note"]), ("resolved", "keep-separate", "different containers"))
        self.assertFalse(consolidate.run(self.store).queued)  # a resolved item is never re-opened

    def test_promote_requires_a_visible_generalisation(self):
        for argv in ((), ("--into", W + "nope"), ("--into", W + "hyp2"), ("--into", "_global/glob")):
            code, _ = self.cli("--resolve", self.hyp, "promote", *argv)
            self.assertEqual(code, 1, argv)
        code, _ = self.cli("--resolve", self.hyp, "maybe")
        self.assertEqual(code, 1)
        self.assertEqual(self.queue()[("promote-hypothesis", (W + "h",))].status, "open")
        code, out = self.cli("--resolve", self.hyp, "promote", "--into", W + "gen")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mem(W + "h").state, "superseded")
        es = [(e.source, e.target, e.type, e.state) for e in Store(self.store.root).edges()]
        self.assertIn((W + "gen", W + "h", "generalizes", "validated"), es)
        self.assertEqual(self.revs(W + "h"), [("supersede", {"state": "superseded"})])

    def test_queue_listing_and_json_run(self):
        code, out = self.cli("--queue", "--json")
        self.assertEqual(code, 0)
        items = json.loads(out)
        self.assertEqual({i["id"] for i in items}, {self.dup, self.hyp})
        code, out = self.cli("--queue")
        self.assertIn(self.dup, out)
        self.cli("--resolve", self.hyp, "reject")
        code, out = self.cli("--queue", "--json")
        self.assertEqual([i["id"] for i in json.loads(out)], [self.dup])
        code, out = self.cli("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["incomplete"], False)


HERMETIC = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


class CrossDeviceTest(_Base):
    def test_two_clones_consolidate_then_sync_identically(self):
        patcher = mock.patch.dict(os.environ, HERMETIC)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / "seed").mkdir()
        seed = make_store(self.tmp / "seed", {
            "acme/_global/MEMORY.md": "", "acme/web/MEMORY.md": "",
            **dict([exp("s1", agent="a1"), exp("s2", agent="a2"), exp("o", kind="outcome"),
                    mem("c", state="candidate", evidence=[(X + "s1", "supports", 1.0), (X + "s2", "supports", 1.0)]),
                    mem("h", state="candidate", mtype="hypothesis", evidence=[(X + "o", "supports", 1.0)]),
                    mem("old", valid_until="2020-01-01"), mem("d1", desc="dup", body="dup"),
                    mem("d2", desc="dup", body="dup", created="2026-10-09")])}) / "acme"
        remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", str(remote))
        git(seed, "init", "-q")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "push", "-q", str(remote), "HEAD:main")
        stores = []
        for name in ("a", "b"):
            (self.tmp / name).mkdir()
            git(self.tmp / name, "clone", "-q", str(remote), "acme")
            stores.append(Store(self.tmp / name))
        for st in stores:
            r = consolidate.run(st)
            self.assertTrue(r.merged and r.promoted and r.queued and r.obsoleted)
        names = [sorted(p.relative_to(st.root).as_posix() for p in st.root.rglob("*.md")
                        if p.parent.name in ("revisions", "queue", "edges")) for st in stores]
        self.assertEqual(names[0], names[1])
        self.assertTrue(any("/revisions/" in n for n in names[0]) and any("/queue/" in n for n in names[0]))
        for st in stores + stores[:1]:
            self.assertEqual([r.error for r in sync(st)], [None])
        a, b = (snapshot(st.root / "acme") for st in stores)
        self.assertEqual(a, b)
        for st in stores:
            self.assertEqual(git(st.root / "acme", "status", "--porcelain"), "")
            self.assertEqual(git(st.root / "acme", "ls-files", "-u"), "")


if __name__ == "__main__":
    unittest.main()
