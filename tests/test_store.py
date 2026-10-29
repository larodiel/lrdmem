import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lrdmem.store import (SUPPORTED_FORMAT, AmbiguousRepo, Context, Edge, Experience, Memory, QueueItem, Revision,
                          Store, StoreFormatError, repo_key, resolve, store_root)
from tests.fixtures import make_store


def _mem(type_="feedback", extra="", body="body [[other]] and [[x]]\n"):
    return f"---\nname: n\ndescription: d\nmetadata:\n  type: {type_}\n{extra}---\n{body}"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def test_dict_items_in_lists_are_ignored(self):
        root = make_store(self.tmp, {"work/_global/a.md": _mem(extra="triggers:\n  paths: [{a:b}, x.py]\nsupersedes: [{a:b}, old]\n")})
        m = Store(root).memories()[0]
        self.assertEqual(m.triggers["paths"], ["x.py"])
        self.assertEqual(m.supersedes, ["old"])

    def test_environments_filtered(self):
        root = make_store(self.tmp, {"work/_global/a.md": _mem(), "_global/g.md": _mem(),
                                     "bin/x": "", ".git/x": "", "_x/y": "", "home/app/a.md": _mem()})
        (root / "file.txt").write_text("x")
        real = self.tmp / "real"
        (real / "app").mkdir(parents=True)
        (root / "linked").symlink_to(real)
        self.assertEqual(Store(root).environments(), ["home", "linked", "work"])

    def test_memories_scopes_and_ignored_files(self):
        root = make_store(self.tmp, {
            "_global/MEMORY.md": "idx", "_global/g.md": _mem("user"),
            "work/_global/w.md": _mem(), "work/app/README.md": "r",
            "work/app/a.md": _mem(), "work/app/contexts/2026-01-01-c.md": "---\nid: c\n---\n",
        })
        by = {m.slug: m for m in Store(root).memories()}
        self.assertEqual(set(by), {"g", "w", "a"})
        self.assertEqual((by["g"].env, by["g"].repo), (None, None))
        self.assertEqual((by["w"].env, by["w"].repo), ("work", None))
        self.assertEqual((by["a"].env, by["a"].repo), ("work", "app"))

    def test_kind_inference_and_explicit(self):
        root = make_store(self.tmp, {
            "_global/u.md": _mem("user"), "_global/r.md": _mem("reference"),
            "_global/f.md": _mem("feedback"), "_global/p.md": _mem("project"),
            "_global/e.md": _mem("user", "  kind: obligation\n"),
        })
        k = {m.slug: m.kind for m in Store(root).memories()}
        self.assertEqual(k, {"u": "fact", "r": "fact", "f": "pattern", "p": "pattern", "e": "obligation"})

    def test_properties(self):
        text = ("---\nname: n\ndescription: d\nmetadata:\n  type: feedback\n"
                "triggers:\n  paths: [\"*.php\"]\ncheck:\n  run: x\n  on: stop\nsupersedes: [old]\n---\nsee [[a]] [[b]]\n")
        m = Store(make_store(self.tmp, {"_global/m.md": text})).memories()[0]
        self.assertEqual((m.type, m.description, m.links, m.supersedes), ("feedback", "d", ["a", "b"], ["old"]))
        self.assertEqual((m.triggers, m.check), ({"paths": ["*.php"]}, {"run": "x", "on": "stop"}))

    def test_defaults_without_frontmatter(self):
        root = make_store(self.tmp, {"_global/m.md": "just text\n"})
        m = Store(root).memories()[0]
        self.assertEqual((m.type, m.description, m.triggers, m.check, m.links, m.supersedes),
                         ("", "", {}, None, [], []))

    def test_contexts(self):
        root = make_store(self.tmp, {
            "work/app/a.md": _mem(),
            "work/app/contexts/2026-10-28-c.md": "---\nid: 2026-10-28-c\ndate: 2026-10-28\nmemories: [a]\n---\nx\n",
        })
        cs = Store(root).contexts()
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual((c.id, c.env, c.repo, c.memories, c.date), ("2026-10-28-c", "work", "app", ["a"], "2026-10-28"))

    def test_memory_lookup_and_scope_for(self):
        root = make_store(self.tmp, {"_global/g.md": _mem(), "work/app/a.md": _mem(), "home/app/a.md": _mem()})
        s = Store(root)
        self.assertEqual(s.memory("a", "work").path, root / "work/app/a.md")
        self.assertEqual(s.memory("g", "work").slug, "g")
        self.assertIsNone(s.memory("nope", "work"))
        self.assertEqual(s.scope_for("work", "app"), [root / "_global", root / "work/_global", root / "work/app"])
        self.assertEqual(s.folders()[0], root / "_global")

    def test_folder_filter_order_and_robustness(self):
        root = make_store(self.tmp, {
            "work/.obsidian/x.md": _mem(), "work/_x/x.md": _mem(), "work/Zed/a.md": _mem(),
            "work/_global/w.md": _mem(),
            "work/app/bad.md": "x", "work/app/ok.md": "---\nsupersedes:\nname: n\n---\n",
            "work/app/contexts/c.md": "---\nmemories:\ndate:\n---\n",
        })
        (root / "work/.git").mkdir()
        (root / "work/app/bad.md").write_bytes(b"\xff\xfe\x00bad")
        s = Store(root)
        self.assertEqual([p.name for p in s.folders("work")], ["_global", "Zed", "app"])
        self.assertEqual({m.slug for m in s.memories()}, {"w", "a", "ok"})
        ok = s.memory("ok", "work")
        self.assertEqual((ok.supersedes, ok.description, ok.type), ([], "", ""))
        c = s.contexts()[0]
        self.assertEqual((c.memories, c.date, c.id), ([], "", "c"))

    def test_store_root(self):
        old = os.environ.pop("LRDMEM_STORE", None)
        self.addCleanup(lambda: os.environ.__setitem__("LRDMEM_STORE", old) if old else os.environ.pop("LRDMEM_STORE", None))
        with self.assertRaises(RuntimeError):
            store_root()
        os.environ["LRDMEM_STORE"] = "/x/y"
        self.assertEqual(store_root(), Path("/x/y"))

    def test_repo_key_and_resolve(self):
        repo = self.tmp / "app"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(repo_key(repo), "app")
        self.assertIsNone(repo_key(plain))
        root = make_store(self.tmp, {"work/app/a.md": _mem()})
        s = Store(root)
        self.assertEqual(resolve(s, repo), ("work", "app"))
        self.assertIsNone(resolve(s, plain))
        other = self.tmp / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertIsNone(resolve(s, other))
        (root / "home/app").mkdir(parents=True)
        with self.assertRaises(AmbiguousRepo):
            resolve(s, repo)
        self.assertEqual(resolve(s, repo, "home"), ("home", "app"))
        self.assertIsNone(resolve(s, repo, "nope"))


def _v2(extra_meta="", top="", body="b\n"):
    return f"---\nname: n\ndescription: d\nmetadata:\n  type: feedback\n{extra_meta}{top}---\n{body}"


class V2StoreTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def store(self, files):
        return Store(make_store(self.tmp, files))

    def test_memory_defaults_and_mapping(self):
        s = self.store({"work/app/a.md": _mem(), "work/app/o.md": _mem(extra="  kind: obligation\n"),
                        "work/app/p.md": _mem(extra="  kind: pattern\n"),
                        "work/app/v.md": _mem(extra="  kind: fact\n  memory_type: bogus\n  state: nope\n")})
        m = {x.slug: x for x in s.memories()}
        self.assertEqual((m["a"].memory_type, m["a"].state), ("semantic", "active"))
        self.assertEqual(m["o"].memory_type, "procedural")
        self.assertEqual(m["p"].memory_type, "procedural")
        self.assertEqual((m["v"].memory_type, m["v"].state), ("semantic", "active"))
        self.assertEqual(m["a"].ref, "work/app/a")

    def test_memory_v2_fields_and_evidence(self):
        meta = ("  memory_type: hypothesis\n  state: candidate\n  valid_from: 2026-01-01\n"
                "  valid_until: 2027-01-01\n  created: 2026-01-01\n")
        top = "evidence: [{ref: work/app/experiences/e1, kind: supports, weight: 0.5}, {ref: x, kind: bad, weight: 1}]\n"
        m = self.store({"work/app/a.md": _v2(meta, top)}).memories()[0]
        self.assertEqual((m.memory_type, m.state, m.valid_from, m.valid_until, m.created),
                         ("hypothesis", "candidate", "2026-01-01", "2027-01-01", "2026-01-01"))
        self.assertEqual([(e.ref, e.kind, e.weight) for e in m.evidence], [("work/app/experiences/e1", "supports", 0.5)])

    def test_new_folders_are_not_memories(self):
        files = {"work/app/a.md": _mem()}
        for sub in ("experiences", "edges", "revisions", "queue"):
            files[f"work/app/{sub}/x.md"] = "---\nid: x\n---\n"
        files["work/app/experiences/2026-10-29-x.md"] = "---\nid: 2026-10-29-x\n---\n"
        s = self.store(files)
        self.assertEqual([m.slug for m in s.memories()], ["a"])
        self.assertEqual(s.environments(), ["work"])
        self.assertEqual([p.name for p in s.folders()], ["app"])

    def test_entities(self):
        s = self.store({
            "work/app/experiences/2026-10-29-x.md": ("---\nid: 2026-10-29-x\nkind: error\ndate: 2026-10-29T14:03:00Z\n"
                "agent: codex\ndevice: nb\nfiles: [a.php, b.php]\nsource: run\nrequest_id: r1\n---\nbody\n"),
            "work/app/edges/2026-10-29-e1.md": ("---\nid: 2026-10-29-e1\nsource: work/app/a\ntarget: work/app/b\ntype: supports\n"
                "state: proposed\nweight: 0.5\nevidence: [{ref: work/app/a, kind: supports, weight: 1.0}]\nsuperseded_by: e2\n---\n"),
            "work/app/edges/2026-10-29-e2.md": "---\nid: 2026-10-29-e2\nweight: nan\n---\n",
            "work/app/revisions/2026-10-29-r1.md": ("---\nid: 2026-10-29-r1\ntarget: work/app/a\naction: state_change\n"
                "before:\n  state: candidate\nafter:\n  state: active\nreason: two\nby: consolidate\ndate: 2026-10-29\n---\n"),
            "work/app/queue/q1.md": "---\nid: q1\nkind: merge\nsubjects: [work/app/a, work/app/b]\nstatus: resolved\nresolution: keep\n---\n",
            "work/app/queue/q2.md": "---\nid: q2\n---\n",
        })
        e = s.experiences()[0]
        self.assertIsInstance(e, Experience)
        self.assertEqual((e.id, e.kind, e.date, e.agent, e.device, e.files, e.source, e.request_id),
                         ("2026-10-29-x", "error", "2026-10-29T14:03:00Z", "codex", "nb", ["a.php", "b.php"], "run", "r1"))
        self.assertEqual(e.ref, "work/app/experiences/2026-10-29-x")
        e1, e2 = s.edges()
        self.assertIsInstance(e1, Edge)
        self.assertEqual((e1.source, e1.target, e1.type, e1.state, e1.weight, e1.superseded_by),
                         ("work/app/a", "work/app/b", "supports", "proposed", 0.5, "e2"))
        self.assertEqual(len(e1.evidence), 1)
        self.assertEqual((e2.weight, e2.evidence), (1.0, []))
        r = s.revisions()[0]
        self.assertIsInstance(r, Revision)
        self.assertEqual((r.target, r.action, r.before, r.after, r.reason, r.by, r.date),
                         ("work/app/a", "state_change", {"state": "candidate"}, {"state": "active"}, "two", "consolidate", "2026-10-29"))
        q1, q2 = s.queue()
        self.assertIsInstance(q1, QueueItem)
        self.assertEqual((q1.kind, q1.subjects, q1.status, q1.resolution),
                         ("merge", ["work/app/a", "work/app/b"], "resolved", "keep"))
        self.assertEqual((q2.status, q2.subjects, q2.resolution), ("open", [], ""))

    def test_context_is_correction_experience(self):
        s = self.store({"work/app/contexts/c1.md": "---\nid: c1\ndate: 2026-01-02\nsource: chat\nmemories: [a]\n---\nb\n"})
        self.assertIs(Context, Experience)
        (c,) = s.experiences()
        self.assertEqual((c.kind, c.memories, c.ref), ("correction", ["a"], "work/app/contexts/c1"))
        self.assertEqual(s.contexts(), [c])

    def test_wikilink_is_not_an_edge(self):
        s = self.store({"work/app/a.md": _mem(body="see [[x]]\n")})
        self.assertEqual(s.edges(), [])
        self.assertEqual(s.memories()[0].links, ["x"])

    def test_node(self):
        s = self.store({"_global/g.md": _mem(), "work/_global/w.md": _mem(), "work/app/a.md": _mem(),
                        "work/app/experiences/x.md": "---\nid: x\n---\n", "work/app/edges/e.md": "---\nid: e\n---\n",
                        "work/app/contexts/c.md": "---\nid: c\n---\n", "_global/experiences/gx.md": "---\nid: gx\n---\n",
                        "work/app/MEMORY.md": "x"})
        for ref, cls in (("_global/g", Memory), ("work/_global/w", Memory), ("work/app/a", Memory),
                         ("work/app/experiences/x", Experience), ("work/app/edges/e", Edge),
                         ("work/app/contexts/c", Experience), ("_global/experiences/gx", Experience)):
            self.assertIsInstance(s.node(ref), cls, ref)
        for ref in ("work/app/nope", "work/app/MEMORY", "../a", "work/../work/app/a", "", "a", "work/app/a.md",
                    "work/app/queue/x/y", "bin/app/a", "work/app/experiences"):
            self.assertIsNone(s.node(ref), ref)

    def test_format_marker(self):
        s = self.store({"work/app/a.md": _mem()})
        self.assertEqual(s.format(), 1)
        s.ensure_format()
        self.assertEqual((s.root / ".lrdmem-format").read_text().strip(), "2")
        self.assertEqual(s.format(), SUPPORTED_FORMAT)
        s.ensure_format()
        (s.root / ".lrdmem-format").write_text("3\n")
        with self.assertRaises(StoreFormatError):
            s.ensure_format()
        self.assertEqual(s.format(), 3)
        (s.root / ".lrdmem-format").write_text("junk")
        with self.assertRaises(StoreFormatError):
            s.format()

    def test_kind_defaults_to_correction_only_for_contexts(self):
        s = self.store({"work/app/experiences/x.md": "---\nid: x\n---\n", "work/app/contexts/c.md": "---\nid: c\n---\n"})
        self.assertEqual([e.kind for e in s.experiences()], ["", "correction"])

    def test_stored_confidence_is_ignored(self):
        s = self.store({"work/app/a.md": _v2(top="confidence: 0.9\npriority: 3\n"),
                        "work/app/edges/e.md": "---\nid: e\nconfidence: 0.2\n---\n"})
        self.assertFalse(hasattr(s.memories()[0], "confidence"))
        self.assertFalse(hasattr(s.edges()[0], "confidence"))

    def test_format_marker_garbage_and_fallbacks(self):
        s = self.store({"work/app/a.md": _mem()})
        for bad in ("0", "-1", "2_0", "+2", "", "1.5"):
            (s.root / ".lrdmem-format").write_text(bad)
            with self.assertRaises(StoreFormatError, msg=bad):
                s.format()
        (s.root / ".lrdmem-format").unlink()
        (s.root / f".lrdmem-format.{os.getpid()}.tmp").write_text("stale")
        s.ensure_format()
        self.assertEqual(s.format(), 2)
        (s.root / ".lrdmem-format").unlink()
        with mock.patch("os.link", side_effect=OSError("no hardlinks")):
            s.ensure_format()
        self.assertEqual(s.format(), 2)
        self.assertEqual([p.name for p in s.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_format_marker_does_not_follow_symlink(self):
        s = self.store({"work/app/a.md": _mem()})
        target = self.tmp / "victim"
        (s.root / ".lrdmem-format").symlink_to(target)
        with self.assertRaises(StoreFormatError):
            s.ensure_format()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
