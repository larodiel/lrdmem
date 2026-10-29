import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from lrdmem import cli, evidence, experience, propose, write
from lrdmem.frontmatter import parse
from lrdmem.lint import lint
from lrdmem.store import Memory, Store
from tests.fixtures import make_store

MEM = ("---\nname: {0}\ndescription: {1}\nmetadata:\n  type: feedback\n  author: me\n  updated: 2026-01-01\n"
       "---\n\nbody\n\n**Why:** w\n**How to apply:** h\n")


def cand(slug, memory_type="semantic", classification="new", **kw):
    c = {"slug": slug, "memory_type": memory_type, "description": f"About {slug}", "body": f"{slug} body.",
         "why": "because.", "how": "do it.", "scope": "repo", "classification": classification}
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


class _Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, {"_global/MEMORY.md": "", "acme/_global/MEMORY.md": "",
                                          "acme/web/MEMORY.md": "", "other/_global/MEMORY.md": "- [far](far.md) — Far away\n",
                                          "other/_global/far.md": MEM.format("far", "Far away")})
        self.repo = self.tmp / "web"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.store = Store(self.root)

    def exp(self, kind="error", text="migration failed because column removed before dependency", **kw):
        return experience.add(self.store, self.repo, kind=kind, agent=kw.pop("agent", "claude"), text=text, **kw)[0]

    def apply(self, exp, *cands, agent="claude"):
        return propose.apply(self.store, self.repo, {"experience": exp.ref, "candidates": list(cands)}, agent=agent)

    def mem(self, slug, folder="acme/web"):
        node = self.store.node(f"{folder}/{slug}")
        return node if isinstance(node, Memory) else None

    def put(self, slug, description):
        (self.root / f"acme/web/{slug}.md").write_text(MEM.format(slug, description))
        with open(self.root / "acme/web/MEMORY.md", "a") as f:
            f.write(f"- [{slug}]({slug}.md) — {description}\n")

    def snapshot(self):
        return {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}


class ProposeTest(_Base):
    def test_spec_example_agent_extracted(self):
        e = self.exp()
        r = self.apply(e, cand("procedure-deps", "procedural", description="Check deps before dropping columns"),
                       cand("migration-failed", "episodic", why=None, how=None),
                       cand("column-removed-first", "semantic"),
                       cand("order-caused-failure", "hypothesis"))
        self.assertEqual(r.refused, [])
        self.assertEqual(sorted(r.created), ["acme/web/column-removed-first", "acme/web/migration-failed",
                                             "acme/web/order-caused-failure", "acme/web/procedure-deps"])
        for slug, mtype in (("migration-failed", "episodic"), ("column-removed-first", "semantic"),
                            ("order-caused-failure", "hypothesis"), ("procedure-deps", "procedural")):
            m = self.mem(slug)
            self.assertEqual((m.memory_type, m.state), (mtype, "candidate"), slug)
            self.assertEqual([(x.ref, x.kind, x.weight) for x in m.evidence], [(e.ref, "observed_in", 1.0)])
            self.assertEqual(m.created, date.today().isoformat())
            self.assertEqual(m.data["metadata"]["author"], "claude")
        self.assertNotIn("**Why:**", self.mem("migration-failed").body)
        self.assertIn("**Why:** because.\n**How to apply:** do it.", self.mem("procedure-deps").body)
        derived = {(x.source, x.target, x.state) for x in self.store.edges() if x.type == "derived_from"}
        self.assertEqual(derived, {(f"acme/web/{s}", "acme/web/migration-failed", "proposed")
                                   for s in ("procedure-deps", "column-removed-first", "order-caused-failure")})
        self.assertEqual(sorted(r.edges), sorted(x.ref for x in self.store.edges()))
        idx = (self.root / "acme/web/MEMORY.md").read_text()
        self.assertIn("- [procedure-deps](procedure-deps.md) — Check deps before dropping columns\n", idx)
        self.assertEqual(lint(self.store), [])

    def test_correction_semantic_active_hypothesis_never(self):
        e = self.exp(kind="correction", text="no, spaces not tabs")
        r = self.apply(e, cand("spaces"), cand("tabs-hurt", "hypothesis"), cand("unsure", classification="insufficient"))
        self.assertEqual(r.refused, [])
        self.assertEqual(self.mem("spaces").state, "active")
        self.assertEqual([(x.kind, x.weight) for x in self.mem("spaces").evidence], [("supports", 1.0)])
        self.assertEqual((self.mem("tabs-hurt").memory_type, self.mem("tabs-hurt").state), ("hypothesis", "candidate"))
        self.assertEqual(self.mem("unsure").state, "candidate")

    def test_review_is_active(self):
        e = self.exp(kind="review", text="reviewer: use spaces", source="pr", ref="https://github.com/o/web/pull/1#r1")
        self.apply(e, cand("spaces", "procedural"))
        self.assertEqual(self.mem("spaces").state, "active")

    def test_equivalent_adds_evidence_not_a_file(self):
        self.put("spaces", "Indent with spaces")
        e = self.exp(kind="discovery", text="spaces again")
        before = sorted(p.name for p in (self.root / "acme/web").glob("*.md"))
        r = self.apply(e, {"classification": "equivalent", "relates_to": "acme/web/spaces"})
        self.assertEqual((r.created, r.refused, r.evidence_added), ([], [], ["acme/web/spaces"]))
        self.assertEqual(sorted(p.name for p in (self.root / "acme/web").glob("*.md")), before)
        self.assertEqual([(x.ref, x.kind) for x in self.mem("spaces").evidence], [(e.ref, "observed_in")])
        revs = [v for v in self.store.revisions() if v.target == "acme/web/spaces"]
        self.assertEqual([(v.action, v.before, v.after) for v in revs], [("merge", {"evidence": ""}, {"evidence": e.ref})])
        again = self.apply(e, {"classification": "equivalent", "relates_to": "acme/web/spaces"})
        self.assertEqual((again.evidence_added, again.refused), ([], []))  # same evidence twice: no-op
        self.assertEqual(len(self.mem("spaces").evidence), 1)

    def test_contradicts_marks_the_weaker_side_contested(self):
        self.put("strong", "Strong rule")
        refs = [self.exp(kind="correction", text=f"proof {i}", agent=f"a{i}").ref for i in range(2)]
        for ref in refs:
            write.add_evidence(self.root / "acme/web/strong.md", evidence.Evidence(ref, "supports", 1.0))
        e = self.exp(kind="discovery", text="maybe the opposite")
        r = self.apply(e, cand("weak", classification="contradicts", relates_to="acme/web/strong"))
        self.assertEqual(r.refused, [])
        self.assertEqual((self.mem("weak").state, self.mem("strong").state), ("contested", "active"))
        [edge] = [x for x in self.store.edges() if x.type == "contradicts"]
        self.assertEqual((edge.source, edge.target, edge.state), ("acme/web/weak", "acme/web/strong", "proposed"))
        revs = [v for v in self.store.revisions() if v.target == "acme/web/weak"]
        self.assertEqual([(v.action, v.before, v.after) for v in revs],
                         [("state_change", {"state": "candidate"}, {"state": "contested"})])
        self.assertEqual(lint(self.store), [])

    def test_contradicts_existing_weaker_and_tie(self):
        self.put("old", "Old rule")
        e = self.exp(kind="correction", text="the old rule is wrong")
        self.apply(e, cand("new-rule", classification="contradicts", relates_to="acme/web/old"))
        self.assertEqual((self.mem("new-rule").state, self.mem("old").state), ("active", "contested"))
        self.assertEqual([(v.action, v.before, v.after) for v in self.store.revisions() if v.target == "acme/web/old"],
                         [("state_change", {"state": ""}, {"state": "contested"})])
        self.put("bare", "Bare rule")
        with mock.patch.object(evidence, "confidence", return_value=0.5):
            self.apply(e, cand("tie", classification="contradicts", relates_to="acme/web/bare"))
        self.assertEqual((self.mem("tie").state, self.mem("bare").state), ("contested", "active"))
        self.assertEqual(lint(self.store), [])

    def test_complementary_edge_type(self):
        self.put("base", "Base rule")
        e = self.exp(kind="discovery", text="related idea")
        r = self.apply(e, cand("near", classification="complementary", relates_to="acme/web/base"),
                       cand("wide", classification="complementary", relates_to="acme/web/base", edge_type="generalizes"))
        self.assertEqual(r.refused, [])
        got = {(x.source, x.type, x.state) for x in self.store.edges()}
        self.assertEqual(got, {("acme/web/near", "related_to", "proposed"), ("acme/web/wide", "generalizes", "proposed")})

    def test_malformed_candidates_refused_others_proceed(self):
        self.put("sem", "A semantic rule")
        (self.root / "_global/shadow.md").write_text(MEM.format("shadow", "Global shadow"))
        e = self.exp(kind="correction", text="a correction")
        bad = [
            "not a dict",
            dict(cand("x1"), bogus="y"),
            cand("x2", "belief"),
            cand("x3", classification="maybe"),
            {"classification": "equivalent", "relates_to": "acme/web/sem", "memory_type": "hypothesis"},
            cand("x5", why=None),
            cand("x6", how=" "),
            cand("x7", body="token = abcdef123456"),
            cand("Bad Slug"),
            cand("memory"),
            cand("x10", classification="complementary", relates_to="other/_global/far"),
            cand("x11", classification="complementary", relates_to="acme/web/missing"),
            cand("x12", classification="complementary"),
            {"classification": "equivalent"},
            cand("x14", triggers={"paths": "a"}),
            cand("x15", triggers={"bogus": ["a"]}),
            cand("shadow"),
            cand("sem"),
            cand("x18", description="two\nlines"),
            cand("x19", scope="planet"),
            cand("x20", edge_type="bogus", classification="complementary", relates_to="acme/web/sem"),
            cand("x21", body=f"see {Path.home()}/secret-notes"),
            cand("x22", body="see [[nowhere]]"),
            cand("x23", classification="contradicts", relates_to="acme/web/sem", edge_type="related_to"),
            cand("x24", description=7),
        ]
        snap = self.snapshot()
        r = self.apply(e, *bad)
        self.assertEqual([i for i, _ in r.refused], list(range(len(bad))))
        self.assertEqual((r.created, r.edges, r.evidence_added), ([], [], []))
        self.assertEqual(self.snapshot(), snap)
        why = ["JSON object", "unknown keys", "memory_type", "classification", "hypothesis cannot merge", "missing why",
               "missing how", "credential", "slug must", "slug must", "outside the candidate", "not a memory",
               "required for complementary", "required for equivalent", "triggers", "triggers", "_global/shadow.md",
               "classify it as equivalent", "single-line", "scope must", "edge_type", "home directory",
               "[[nowhere]]", "always contradicts", "must be strings"]
        for (i, reason), want in zip(r.refused, why):
            self.assertIn(want, reason, i)
        r = self.apply(e, cand("x2", "belief"), cand("good"))
        self.assertEqual(([i for i, _ in r.refused], r.created), ([0], ["acme/web/good"]))

    def test_refused_candidate_writes_nothing_even_late(self):
        e = self.exp(kind="discovery", text="something")
        with mock.patch.object(propose.edges, "create", side_effect=write.Refused("boom")):
            r = self.apply(e, cand("ep", "episodic", why=None, how=None), cand("later"))
        self.assertEqual([i for i, _ in r.refused], [1])
        self.assertIsNone(self.mem("later"))
        self.assertNotIn("later.md", (self.root / "acme/web/MEMORY.md").read_text())

    def test_payload_level_errors(self):
        e = self.exp()
        for payload in ([], {"experience": e.ref}, {"experience": "acme/web/nope", "candidates": [cand("a")]},
                        {"experience": e.ref, "candidates": [], }, {"experience": e.ref, "candidates": [cand("a")], "x": 1},
                        {"experience": e.ref, "candidates": "a"}):
            with self.subTest(payload), self.assertRaises(write.Refused):
                propose.apply(self.store, self.repo, payload, agent="claude")

    def test_experience_must_be_visible_from_scope(self):
        far = self.store.root / "other/_global/experiences"
        far.mkdir(parents=True)
        (far / "2026-10-29-x.md").write_text("---\nid: 2026-10-29-x\ndate: 2026-10-29\nkind: error\nagent: a\n"
                                             "source: chat\n---\n\nx\n")
        r = propose.apply(self.store, self.repo, {"experience": "other/_global/experiences/2026-10-29-x",
                                                  "candidates": [cand("a")]}, agent="claude")
        self.assertEqual([i for i, _ in r.refused], [0])
        self.assertIsNone(self.mem("a"))

    def test_episodic_later_in_payload_still_linked(self):
        e = self.exp()
        r = self.apply(e, cand("first"), cand("ep", "episodic", why=None, how=None))
        self.assertEqual(r.refused, [])
        self.assertEqual([(x.source, x.target, x.type) for x in self.store.edges()],
                         [("acme/web/first", "acme/web/ep", "derived_from")])


class ProposeCliTest(_Base):
    def run_cli(self, stdin, *argv):
        out, err = io.StringIO(), io.StringIO()
        sin = io.StringIO(stdin)
        env = {"LRDMEM_STORE": str(self.root), "LRDMEM_CONFIG": str(self.tmp / "cfg.json")}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", sin), \
                mock.patch.object(sin, "isatty", return_value=False, create=True), \
                mock.patch("pathlib.Path.cwd", return_value=self.repo), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["memory", "propose", "--agent", "claude", *argv])
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_prints_and_exit_codes(self):
        e = self.exp()
        code, out, _ = self.run_cli(json.dumps({"experience": e.ref, "candidates": [
            cand("ep", "episodic", why=None, how=None), cand("bad", "belief")]}))
        self.assertEqual(code, 0)
        self.assertEqual(out[0], "created acme/web/ep")
        self.assertEqual(out[1], "refused 1: memory_type must be one of ['episodic', 'semantic', 'procedural', "
                                 "'hypothesis', 'prospective', 'metacognitive']")
        code, out, _ = self.run_cli(json.dumps({"experience": e.ref, "candidates": [cand("bad", "belief")]}))
        self.assertEqual(code, 1)
        code, _, err = self.run_cli("not json")
        self.assertEqual(code, 1)
        self.assertIn("JSON", err)
        code, out, _ = self.run_cli(json.dumps({"experience": e.ref, "candidates": [
            cand("ok"), cand("ep2", "episodic", why=None, how=None)]}))
        self.assertEqual((code, out[:2]), (0, ["created acme/web/ep2", "created acme/web/ok"]))
        self.assertTrue(any(ln.startswith("edge acme/web/edges/") for ln in out))


if __name__ == "__main__":
    unittest.main()
