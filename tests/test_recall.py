import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lrdmem import config
from lrdmem.cli import main
from lrdmem.recall import WEIGHTS, recall, render, to_json
from lrdmem.store import Store
from lrdmem.triggers import CAVEAT
from tests.fixtures import make_store

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
PHP = "  paths: ['**/*.php']\n"


def _mem(name, body="body", trig=PHP, desc="d", **meta):
    extra = "".join(f"  {k}: {v}\n" for k, v in meta.items())
    return (f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: feedback\n{extra}"
            f"{'triggers:' + chr(10) + trig if trig else ''}---\n{body}\n")


def _edge(i, src, tgt, typ, state="validated"):
    return f"---\nid: {i}\nsource: {src}\ntarget: {tgt}\ntype: {typ}\nstate: {state}\n---\n"


class RecallTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)

    def store(self, files):
        self.root = make_store(self.tmp, files)
        return Store(self.root)

    def refs(self, hits):
        return [h.node.ref for h in hits]

    def test_hypothesis_absent_from_hook_present_and_labelled_explicitly(self):
        s = self.store({"work/app/h.md": _mem("h", "order caused it", memory_type="hypothesis", state="candidate"),
                        "work/app/r.md": _mem("r", "use tabs")})
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], for_hook=True, now=NOW)), ["work/app/r"])
        hits = recall(s, "work", "app", files=["a.php"], now=NOW)
        self.assertEqual(set(self.refs(hits)), {"work/app/h", "work/app/r"})
        text = render(hits, "a.php")
        self.assertIn("## Hypotheses — not confirmed\n\n### work/app/h — hypothesis, candidate, confidence 0.00\n"
                      "order caused it\nWhy recalled: file trigger **/*.php", text)
        self.assertLess(text.index("## Consolidated knowledge"), text.index("## Hypotheses"))

    def test_candidate_labelled_and_contested_injected(self):
        s = self.store({"work/app/c.md": _mem("c", "maybe", state="candidate"),
                        "work/app/x.md": _mem("x", "disputed", state="contested")})
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], for_hook=True, now=NOW)), ["work/app/x"])
        text = render(recall(s, "work", "app", files=["a.php"], now=NOW))
        self.assertIn("## Contested\n\n### work/app/x — semantic, contested", text)
        self.assertIn("## Candidates — not confirmed\n\n### work/app/c — semantic, candidate", text)

    def test_superseded_only_with_states_all(self):
        s = self.store({"work/app/old.md": _mem("old", state="superseded"),
                        "work/app/gone.md": _mem("gone", state="obsolete", memory_type="episodic")})
        self.assertEqual(recall(s, "work", "app", files=["a.php"], now=NOW), [])
        hits = recall(s, "work", "app", files=["a.php"], states="all", now=NOW)
        self.assertEqual(set(self.refs(hits)), {"work/app/old", "work/app/gone"})
        self.assertIn("## Historical experiences", render(hits))
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], states=["obsolete"], now=NOW)),
                         ["work/app/gone"])

    def test_validity_window(self):
        s = self.store({"work/app/exp.md": _mem("exp", valid_until="2026-10-28"),
                        "work/app/fut.md": _mem("fut", valid_from="2026-10-30"),
                        "work/app/ok.md": _mem("ok", valid_from="2026-10-29", valid_until="2026-10-29"),
                        "work/app/bad.md": _mem("bad", valid_from="'2026-11-31'", valid_until="soon"),
                        "work/app/lex.md": _mem("lex", valid_until="2026-1-5"),
                        "work/app/dt.md": _mem("dt", valid_until="2026-10-28T00:00:00Z"),
                        "work/app/sp.md": _mem("sp", valid_until="'2026-10-28 '"),
                        "work/app/dtf.md": _mem("dtf", valid_from="2026-10-30T09:00:00+02:00")})
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], now=NOW)),
                         ["work/app/bad", "work/app/lex", "work/app/ok"])
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], states="all", now=NOW)),
                         ["work/app/bad", "work/app/lex", "work/app/ok"])

    def test_two_hop_neighbour_and_relations(self):
        s = self.store({"work/app/a.md": _mem("a", "alpha migration", trig=""),
                        "work/app/b.md": _mem("b", "beta", trig=""),
                        "work/app/c.md": _mem("c", "gamma", trig=""),
                        "work/app/edges/e1.md": _edge("e1", "work/app/a", "work/app/b", "supports"),
                        "work/app/edges/e2.md": _edge("e2", "work/app/b", "work/app/c", "caused_by")})
        hits = recall(s, "work", "app", query="migration", now=NOW)
        self.assertEqual(self.refs(hits), ["work/app/a", "work/app/b", "work/app/c"])
        self.assertEqual(hits[2].signals, ["2 hops via caused_by"])
        self.assertEqual(self.refs(recall(s, "work", "app", query="migration", hops=0, now=NOW)), ["work/app/a"])
        text = render(hits)
        self.assertIn("Why recalled: 2 hops via caused_by\nRelations: caused_by → work/app/b (validated, active)", text)
        self.assertIn("Relations: supports → work/app/a (validated, active); caused_by → work/app/c (validated, active)",
                      text)

    def test_relations_validated_first_max_five(self):
        files = {"work/app/a.md": _mem("a", "alpha", trig="")}
        for i in range(7):
            files[f"work/app/n{i}.md"] = _mem(f"n{i}", "n", trig="")
            files[f"work/app/edges/e{i}.md"] = _edge(f"e{i}", "work/app/a", f"work/app/n{i}", "related_to",
                                                     "proposed" if i < 3 else "validated")
        files["work/app/edges/bad.md"] = _edge("bad", "work/app/a", "work/app/n0", "contradicts", "invalidated")
        hit = recall(self.store(files), "work", "app", query="alpha", hops=0, now=NOW)
        rel = to_json(hit)[0]["relations"]
        self.assertEqual(len(rel), 5)
        self.assertEqual([r["state"] for r in rel], ["validated"] * 4 + ["proposed"])
        self.assertNotIn("contradicts", [r["type"] for r in rel])

    def test_relation_targets_in_hook_and_explicit(self):
        s = self.store({"work/app/a.md": _mem("a", "alpha [[c]] [[h]] [[o]] [[x]]"),
                        "work/app/c.md": _mem("c", trig="", state="candidate"),
                        "work/app/h.md": _mem("h", trig="", memory_type="hypothesis", state="candidate"),
                        "work/app/o.md": _mem("o", trig="", state="obsolete"),
                        "work/app/x.md": _mem("x", trig="", state="contested"),
                        "work/app/experiences/e1.md": "---\nid: e1\ndate: 2026-10-01\nkind: error\nagent: a\n---\nx\n",
                        "work/app/edges/oc.md": _edge("oc", "work/app/a", "work/app/experiences/e1", "occurred_in")})
        hook = to_json(recall(s, "work", "app", files=["a.php"], for_hook=True, now=NOW))[0]["relations"]
        self.assertEqual(sorted((r["target"], r["target_state"]) for r in hook),
                         [("work/app/experiences/e1", "experience"), ("work/app/x", "contested")])
        explicit = recall(s, "work", "app", files=["a.php"], hops=0, now=NOW)
        self.assertEqual(len(to_json(explicit)[0]["relations"]), 5)
        self.assertIn("related_to → work/app/h (validated, candidate)", render(explicit))

    def test_scope_isolation(self):
        s = self.store({"_global/g.md": _mem("g", "global [[secret]]"),
                        "work/other/secret.md": _mem("secret", "OTHER REPO"),
                        "work/app/mine.md": _mem("mine", "mine"),
                        "work2/app/x.md": _mem("x", "OTHER ENV"),
                        "work/other/edges/e1.md": _edge("e1", "_global/g", "work/other/secret", "caused_by"),
                        "work/app/edges/e2.md": _edge("e2", "work/app/mine", "work/other/secret", "caused_by"),
                        "work/app/edges/e3.md": _edge("e3", "work/app/mine", "work2/app/x", "related_to")})
        both = ["_global/g", "work/app/mine"]
        for kw, want in ((dict(files=["a.php"], for_hook=True), both),
                         (dict(query="other repo env secret", states="all", hops=5), ["_global/g"]),
                         (dict(files=["a.php"], states="all", hops=5), both)):
            hits = recall(s, "work", "app", now=NOW, **kw)
            self.assertEqual(sorted(self.refs(hits)), want)
            text = render(hits) + json.dumps(to_json(hits))
            for leak in ("work/other", "work2", "OTHER"):
                self.assertNotIn(leak, text)

    def test_strong_evidence_beats_text_beyond_any_cut(self):
        files = {f"work/app/a{i}.md": _mem(f"a{i}", "alpha alpha alpha alpha beta " + "filler " * i, trig="")
                 for i in range(40)}
        ev = ", ".join(f"{{ref: work/app/experiences/e{k}, kind: supports, weight: 1.0}}" for k in range(3))
        files["work/app/strong.md"] = _mem("strong", "alpha " + "filler " * 60, trig="").replace(
            "metadata:", f"evidence: [{ev}]\nmetadata:", 1)
        files.update({f"work/app/experiences/e{k}.md":
                      f"---\nid: e{k}\ndate: 2026-10-0{k + 1}\nkind: outcome\nagent: ag{k}\n---\nx\n" for k in range(3)})
        hits = recall(self.store(files), "work", "app", query="alpha", budget=99999, now=NOW)
        self.assertEqual(len(hits), 10)
        self.assertIn("work/app/strong", self.refs(hits))

    def test_store_reads_constant_and_top_k_scored(self):
        def build(n):
            tmp = self.tmp / f"s{n}"
            tmp.mkdir()
            files = {f"work/app/m{i}.md": _mem(f"m{i}", f"rule {i} [[m{(i + 1) % n}]]") for i in range(n)}
            files.update({f"work/app/contexts/2026-01-01-c{i}.md":
                          f"---\nid: c{i}\ndate: 2026-01-01\nsource: chat\nagent: a{i}\nmemories: [m{i}]\n---\nx\n"
                          for i in range(n)})
            return Store(make_store(tmp, files))

        def reads(store, **kw):
            calls = {}
            for name in ("memories", "contexts", "node"):
                real = getattr(Store, name)
                calls[name] = mock.patch.object(Store, name, autospec=True, side_effect=real)
            with calls["memories"] as m, calls["contexts"] as c, calls["node"] as nd, \
                    mock.patch("lrdmem.recall.confidence", wraps=__import__("lrdmem.evidence").evidence.confidence) as conf:
                hits = recall(store, "work", "app", files=["a.php"], now=NOW, **kw)
            return len(hits), m.call_count, c.call_count, nd.call_count, conf.call_count

        small, big = build(3), build(60)
        for kw, most in ((dict(for_hook=True), 2), (dict(budget=99999), 3)):
            a, b = reads(small, **kw), reads(big, **kw)
            self.assertEqual(a[1:4], b[1:4], kw)
            self.assertLessEqual(b[1], most)
            self.assertLessEqual(b[2], 2)
            self.assertEqual(b[0], 10)

    def test_empty_no_match_and_cycle(self):
        self.assertEqual(recall(self.store({}), "work", "app", query="x", files=["a.php"], now=NOW), [])
        s = Store(self.root)
        self.assertEqual(recall(s, "work", "app", now=NOW), [])
        tmp = self.tmp / "cyc"
        tmp.mkdir()
        files = {f"work/app/{n}.md": _mem(n, f"{n} loop", trig="") for n in "abc"}
        files.update({f"work/app/edges/{a}{b}.md": _edge(f"{a}{b}", f"work/app/{a}", f"work/app/{b}", "related_to")
                      for a, b in ("ab", "bc", "ca")})
        s = Store(make_store(tmp, files))
        self.assertEqual(recall(s, "work", "app", query="zzz", now=NOW), [])
        hits = recall(s, "work", "app", query="a", hops=50, now=NOW)
        self.assertEqual(sorted(self.refs(hits)), ["work/app/a", "work/app/b", "work/app/c"])

    def test_to_json_keys(self):
        s = self.store({"work/app/r.md": _mem("r", "use tabs"),
                        "work/app/contexts/2026-10-01-x.md":
                            "---\nid: x\ndate: 2026-10-01\nsource: chat\nagent: a\nmemories: [r]\n---\nx\n"})
        [d] = to_json(recall(s, "work", "app", files=["a.php"], now=NOW))
        self.assertEqual(list(d), ["id", "content", "type", "state", "confidence", "provenance", "relations",
                                   "signals"])
        self.assertEqual((d["id"], d["content"], d["type"], d["state"]), ("work/app/r", "use tabs", "semantic",
                                                                          "active"))
        self.assertEqual(d["provenance"], [{"ref": "work/app/contexts/2026-10-01-x", "kind": "supports",
                                            "weight": 1.0}])
        self.assertGreater(d["confidence"], 0.6)  # legacy context counts as a support
        self.assertEqual((d["relations"], d["signals"]), ([], ["file trigger **/*.php"]))

    def test_hook_rules(self):
        s = self.store({"work/_global/wide.md": _mem("wide", "wide rule"),
                        "work/_global/named.md": _mem("named", "named rule", trig=PHP + "  repos: [app]\n"),
                        "work/app/text.md": _mem("text", "php migration", trig=""),
                        "work/app/ent.md": _mem("ent", "never touch src/a.php by hand", trig="")})
        hook = recall(s, "work", "app", query="php migration", files=["src/a.php"], for_hook=True, now=NOW)
        self.assertEqual(sorted(self.refs(hook)), ["work/_global/named", "work/app/ent"])
        self.assertIn("work/_global/wide", self.refs(recall(s, "work", "app", files=["src/a.php"], now=NOW)))
        self.assertEqual(recall(s, "work", "app", query="migration", for_hook=True, now=NOW), [])

    def test_context_scoring_and_weights(self):
        s = self.store({"work/app/r.md": _mem("r", "x"), "work/_global/e.md": _mem("e", "x", trig=PHP + "  repos: [app]\n"),
                        "_global/g.md": _mem("g", "x")})
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], now=NOW)),
                         ["work/app/r", "work/_global/e", "_global/g"])
        self.assertEqual(set(WEIGHTS), {"text", "context", "confidence", "priority", "hops"})
        with mock.patch.object(config, "load", return_value={"recall_weights": {"context": -1.0, "bogus": "x"}}):
            got = recall(s, "work", "app", files=["a.php"], now=NOW)
        self.assertEqual(self.refs(got), ["_global/g", "work/_global/e", "work/app/r"])
        self.assertAlmostEqual(got[0].score, 1.0 - 0.25 + 0.6)

    def test_budget_limit_order_and_exclude(self):
        files = {f"work/app/m{i}.md": _mem(f"m{i}", "x" * 2000) for i in range(10)}
        files["work/app/ob.md"] = _mem("ob", "y", kind="obligation")
        s = self.store(files)
        got = recall(s, "work", "app", files=["a.php"], for_hook=True, now=NOW)
        self.assertEqual(self.refs(got)[0], "work/app/ob")  # equal score: obligations first, like v0.1
        self.assertEqual(len(got), 3)
        self.assertEqual(recall(s, "work", "app", files=["a.php"], budget=100, exclude={"work/app/ob"}, now=NOW), [])
        self.assertEqual(len(recall(s, "work", "app", files=["a.php"], budget=99999, limit=4, now=NOW)), 4)

    def test_tiebreak_confidence_then_kind(self):
        ctx = lambda i, ms: (f"work/app/contexts/2026-01-0{i}-c.md",
                             f"---\nid: c{i}\ndate: 2026-01-0{i}\nsource: s\nagent: a\nmemories: [{ms}]\n---\nx\n")
        files = {f"work/app/{n}.md": _mem(n, kind="pattern") for n in ("b", "a", "c")}
        files["work/app/o.md"] = _mem("o", kind="fact")
        files.update(dict([ctx(1, "c"), ctx(2, "c"), ctx(3, "b")]))
        got = recall(self.store(files), "work", "app", files=["a.php"], for_hook=True, now=NOW)
        self.assertEqual([h.node.slug for h in got], ["c", "b", "a", "o"])

    def test_render_header_and_printable(self):
        s = self.store({"work/app/r.md": _mem("r", "bad\x1b[31m body")})
        text = render(recall(s, "work", "app", files=["a.php"], now=NOW), "a.php")
        self.assertTrue(text.startswith("## lrdmem: memories for a.php\n" + CAVEAT + "\n\n## Consolidated knowledge\n\n"
                                        "### work/app/r — semantic, active, confidence 0.00\nbad?[31m body\n"
                                        "Why recalled: file trigger **/*.php\n"), text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("Relations:", text)
        self.assertEqual(render([]), "")

    def test_types_filter(self):
        s = self.store({"work/app/p.md": _mem("p", kind="obligation"), "work/app/f.md": _mem("f")})
        self.assertEqual(self.refs(recall(s, "work", "app", files=["a.php"], types=["procedural"], now=NOW)),
                         ["work/app/p"])


class RecallCliTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        root = make_store(self.tmp, {"work/app/r.md": _mem("r", "never drop a column in a migration"),
                                     "work/app/h.md": _mem("h", "migration order", memory_type="hypothesis",
                                                           state="candidate", trig="")})
        repo = self.tmp / "app"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        env = {"LRDMEM_STORE": str(root), "TMPDIR": str(self.tmp), "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_CONFIG_NOSYSTEM": "1"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, "tempdir", None)
        cwd = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, cwd)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_recall_text_and_json(self):
        code, out = self.run_cli("recall", "migration")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("## lrdmem: memories for migration\n"), out)
        self.assertIn("## Hypotheses — not confirmed", out)
        code, out = self.run_cli("recall", "migration", "--json", "--types", "hypothesis")
        self.assertEqual([d["id"] for d in json.loads(out)], ["work/app/h"])
        code, out = self.run_cli("recall", "--file", "x.php", "--states", "candidate", "--json")
        self.assertEqual(json.loads(out), [])
        code, out = self.run_cli("recall", "--file", "x.php", "--states", "all", "--limit", "1", "--json")
        self.assertEqual([d["id"] for d in json.loads(out)], ["work/app/r"])

    def test_negative_hops_refused(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["recall", "x", "--hops", "-1"])
        self.assertEqual(e.exception.code, 2)

    def test_for_is_hook_recall_with_session(self):
        code, out = self.run_cli("for", "migration.php", "--session", "s1")
        self.assertIn("## lrdmem: memories for migration.php", out)
        self.assertIn("### work/app/r", out)
        self.assertNotIn("work/app/h", out)
        self.assertEqual(self.run_cli("for", "migration.php", "--session", "s1")[1], "")


if __name__ == "__main__":
    unittest.main()
