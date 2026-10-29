import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrdmem.cli import main
from lrdmem.search import bm25, candidates, expand
from lrdmem.store import Store
from tests.fixtures import make_store

GIT = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _mem(name, desc="d", body="body", trig=""):
    return (f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: feedback\n"
            f"{'triggers:' + chr(10) + trig if trig else ''}---\n{body}\n")


def _edge(i, src, tgt, typ, state="validated"):
    return f"---\nid: {i}\nsource: {src}\ntarget: {tgt}\ntype: {typ}\nstate: {state}\n---\n"


FILES = {
    "work/app/mig.md": _mem("mig", "database migration", "Never drop a column in a migration.",
                            "  paths: ['**/*.php']\n  commands: ['wp db']\n"),
    "work/app/style.md": _mem("style", "code style", "Use tabs."),
    "work/app/a.md": _mem("a", "alpha", "alpha thing"),
    "work/app/b.md": _mem("b", "beta", "beta thing"),
    "work/app/c.md": _mem("c", "gamma", "gamma thing"),
    "work/app/d.md": _mem("d", "delta", "delta thing"),
    "work/other/secret.md": _mem("secret", "database migration", "migration migration"),
    "home/app/home.md": _mem("home", "database migration", "migration"),
    "work/_global/wide.md": _mem("wide", "wide thing", "migration"),
    "_global/g.md": _mem("g", "global migration", "migration"),
    "work/app/edges/e1.md": _edge("e1", "work/app/a", "work/app/b", "supports"),
    "work/app/edges/e2.md": _edge("e2", "work/app/b", "work/app/c", "caused_by", "proposed"),
    "work/app/edges/e3.md": _edge("e3", "work/app/c", "work/app/a", "related_to"),
    "work/app/edges/e4.md": _edge("e4", "work/app/c", "work/app/d", "related_to", "invalidated"),
    "work/app/edges/e5.md": _edge("e5", "work/app/b", "work/other/secret", "related_to"),
}


class SearchTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.root = make_store(self.tmp, FILES)
        self.store = Store(self.root)

    def refs(self, hits):
        return [h.node.ref for h in hits]

    def test_bm25(self):
        docs = [("x", "drop column migration"), ("y", "tabs and spaces"), ("z", "migration of src/a.php")]
        r = bm25(docs, "migration drop")
        self.assertEqual([i for i, _, _ in r], ["x", "z"])
        self.assertEqual(r[0][2], ["drop", "migration"])
        self.assertEqual([i for i, _, _ in bm25(docs, "src/a.php")], ["z"])
        self.assertEqual(bm25([], "x"), [])
        self.assertEqual(bm25(docs, "nothing"), [])

    def test_text_ranks_and_scope(self):
        hits = candidates(self.store, "work", "app", query="database migration")
        self.assertEqual(hits[0].node.ref, "work/app/mig")
        self.assertIn("bm25: ", hits[0].signals[0])
        got = self.refs(hits)
        self.assertEqual(set(got), {"work/app/mig", "work/_global/wide", "_global/g"})
        self.assertEqual(got, self.refs(candidates(self.store, "work", "app", query="database migration")))

    def test_file_and_command_signals(self):
        h = candidates(self.store, "work", "app", files=["src/x.php"], command="wp db reset")
        self.assertEqual(self.refs(h), ["work/app/mig"])
        self.assertEqual(h[0].signals, ["file trigger **/*.php", "command trigger wp db"])

    def test_entity_id(self):
        h = candidates(self.store, "work", "app", query="what about style")
        self.assertEqual(self.refs(h), ["work/app/style"])
        self.assertEqual(h[0].signals, ["bm25: style", "entity style"])

    def test_accents(self):
        self.assertEqual([i for i, _, _ in bm25([("x", "Migração de dados")], "migracao")], ["x"])
        self.assertEqual([i for i, _, _ in bm25([("x", "migracao")], "MIGRAÇÃO")], ["x"])

    def test_entity_boundaries(self):
        with tempfile.TemporaryDirectory() as t:
            st = Store(make_store(Path(t), {"work/app/m.md": _mem("m", "d", "edit src/a.php now, also x")}))
            self.assertEqual(len(candidates(st, "work", "app", files=["src/a.php"])), 1)
            self.assertEqual(candidates(st, "work", "app", files=["rc/a.php", "a", "", "x.php"]), [])
            self.assertEqual(candidates(st, "work", "app", files=[""], command=""), [])
            self.assertEqual(len(candidates(st, "work", "app", files=["x"])), 1)

    def test_scope_rules(self):
        files = {
            "work/app/ok.md": _mem("ok", "zeta", "zeta"),
            "work/app/other.md": _mem("other", "zeta", "zeta", "  repos: [elsewhere]\n"),
            "work/_global/w.md": _mem("w", "zeta", "zeta", "  repos: [app]\n"),
            "work/_global/x.md": _mem("x", "zeta", "zeta", "  repos: [elsewhere]\n"),
            "home/app/h.md": _mem("h", "zeta", "zeta"),
            "work/app/edges/e.md": _edge("e", "work/app/ok", "home/app/h", "related_to"),
        }
        with tempfile.TemporaryDirectory() as t:
            st = Store(make_store(Path(t), files))
            hits = candidates(st, "work", "app", query="zeta")
            self.assertEqual(sorted(self.refs(hits)), ["work/_global/w", "work/app/ok"])
            out = expand(st, hits, env="work", repo="app")
            self.assertEqual(sorted(self.refs(out)), ["work/_global/w", "work/app/ok"])
            stray = candidates(st, "home", "app", query="zeta")
            self.assertEqual(self.refs(expand(st, stray, env="work", repo="app")), [])

    def test_empty(self):
        self.assertEqual(candidates(self.store, "work", "app"), [])
        with tempfile.TemporaryDirectory() as t:
            root = make_store(Path(t), {"work/app/x.md": _mem("x")})
            self.assertEqual(candidates(Store(root), "work", "app", query="q"), [])
            self.assertEqual(expand(Store(root), [], env="work", repo="app"), [])

    def test_expand_hops_via_cycle_and_scope(self):
        seed = candidates(self.store, "work", "app", query="alpha")
        self.assertEqual(self.refs(seed), ["work/app/a"])
        out = expand(self.store, seed, hops=2, env="work", repo="app")
        by = {h.node.ref: h for h in out}
        self.assertEqual(set(by), {"work/app/a", "work/app/b", "work/app/c"})  # a-b-c-a cycle, d invalidated
        self.assertEqual((by["work/app/b"].hops, by["work/app/b"].via), (1, "supports"))
        self.assertEqual(by["work/app/c"].hops, 1)  # via e3, nearer than b->c
        self.assertEqual(by["work/app/c"].signals, ["1 hop via related_to"])
        s = seed[0].score
        self.assertAlmostEqual(by["work/app/b"].score, s * 0.5)
        self.assertNotIn("work/other/secret", by)
        two = expand(self.store, [by["work/app/b"]], hops=1, env="work", repo="app")
        self.assertEqual({h.node.ref for h in two}, {"work/app/a", "work/app/b", "work/app/c"})

    def test_expand_two_hops_proposed_and_limit(self):
        files = dict(FILES)
        del files["work/app/edges/e3.md"]
        with tempfile.TemporaryDirectory() as t:
            st = Store(make_store(Path(t), files))
            seed = candidates(st, "work", "app", query="alpha")
            by = {h.node.ref: h for h in expand(st, seed, hops=2, env="work", repo="app")}
            self.assertEqual((by["work/app/c"].hops, by["work/app/c"].via), (2, "caused_by"))
            self.assertEqual(by["work/app/c"].signals, ["2 hops via caused_by"])
            self.assertAlmostEqual(by["work/app/c"].score, by["work/app/b"].score * 0.5 * 0.5)
            self.assertEqual(set(by), {"work/app/a", "work/app/b", "work/app/c"})
            one = expand(st, seed, hops=1, env="work", repo="app")
            self.assertEqual(self.refs(one), ["work/app/a", "work/app/b"])
            self.assertEqual(len(expand(st, seed, hops=2, limit=2, env="work", repo="app")), 2)

    def _run(self, *argv, cwd):
        out, err = io.StringIO(), io.StringIO()
        env = {"LRDMEM_STORE": str(self.root), "HOME": str(self.tmp)}
        with mock.patch.dict(os.environ, env), mock.patch("pathlib.Path.cwd", return_value=cwd), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli(self):
        app, lone = self.tmp / "app", self.tmp / "nope"
        for d in (app, lone):
            d.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=d, check=True, env=GIT)
        code, out, _ = self._run("associate", "alpha", "--env", "work", cwd=app)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^work/app/a  \d+\.\d\d  semantic/active  bm25: alpha$")
        self.assertEqual(len(lines), 3)
        code, out, _ = self._run("associate", "alpha", "--json", "--env", "work", cwd=app)
        data = json.loads(out)
        self.assertEqual(set(data[0]), {"ref", "score", "memory_type", "state", "signals", "hops", "via"})
        self.assertEqual(self._run("associate", "zzzz", "--env", "work", cwd=app)[:2], (0, ""))
        self.assertEqual(self._run("associate", "zzzz", "--json", "--env", "work", cwd=app)[:2], (0, "[]\n"))
        for bad in ("0", "-1", "x"):
            with self.assertRaises(SystemExit) as cm:
                self._run("associate", "alpha", "--limit", bad, cwd=app)
            self.assertEqual(cm.exception.code, 2)
        code, out, err = self._run("associate", "alpha", cwd=lone)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not opted in", err)


if __name__ == "__main__":
    unittest.main()
