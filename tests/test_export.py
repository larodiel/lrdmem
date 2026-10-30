import json
import tempfile
import unittest
from pathlib import Path

from lrdmem.export import to_dot, to_html, to_json
from lrdmem.graph import Graph
from lrdmem.store import Store
from tests.fixtures import make_store
from tests.test_graph import _ctx, _files, _mem


class ExportTest(unittest.TestCase):
    def _graph(self, files):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return Graph(Store(make_store(Path(t.name), files)))

    def test_json_edges(self):
        d = to_json(self._graph(_files()))
        rels = [e["rel"] for e in d["edges"]]
        self.assertEqual(rels.count("context"), 4)  # c0 -> phpcs, other; c1, c2 -> phpcs
        self.assertEqual(rels.count("related_to"), 1)
        self.assertIn({"source": "work/app/phpcs", "target": "_global/style", "rel": "related_to",
                       "state": "validated"}, d["edges"])
        self.assertIn({"source": "work/app/contexts/2026-03-01-c0", "target": "work/app/phpcs",
                       "rel": "context", "state": "validated"}, d["edges"])
        ids = [n["id"] for n in d["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))
        phpcs = next(n for n in d["nodes"] if n["id"] == "work/app/phpcs")
        self.assertEqual((phpcs["type"], phpcs["strength"], phpcs["env"]), ("memory", 3, "work"))
        self.assertEqual((phpcs["memory_type"], phpcs["state"]), ("semantic", "active"))  # no explicit kind: semantic (spec §8)
        self.assertGreater(phpcs["confidence"], 0)  # legacy contexts count as supports

    def test_supersedes_edge(self):
        g = self._graph({"work/app/a.md": _mem("a", extra="supersedes: [b]\n"), "work/app/b.md": _mem("b")})
        self.assertEqual(to_json(g)["edges"], [{"source": "work/app/a", "target": "work/app/b", "rel": "supersedes",
                                                "state": "validated"}])

    def test_file_edges_carry_type_and_state(self):
        edge = ("---\nid: e1\nsource: work/app/a\ntarget: work/app/b\ntype: contradicts\nstate: proposed\n---\n")
        g = self._graph({"work/app/a.md": _mem("a"), "work/app/b.md": _mem("b"), "work/app/edges/e1.md": edge})
        self.assertIn({"source": "work/app/a", "target": "work/app/b", "rel": "contradicts", "state": "proposed"},
                      to_json(g)["edges"])

    def test_dot(self):
        g = self._graph({"work/app/a.md": _mem("a"), "work/app/b.md": _mem("b", "see [[a]]")})
        dot = to_dot(g)
        self.assertTrue(dot.startswith("digraph"))
        self.assertIn('"work/app/b" -> "work/app/a"', dot)

    def test_dot_escapes_labels(self):
        c = _ctx("c", "2026-01-01", ["a"]).replace("first line of c", 'say "hi" \\ x')
        g = self._graph({"work/app/a.md": _mem("a"), "work/app/contexts/2026-01-01-c.md": c})
        self.assertIn('label="say \\"hi\\" \\\\ x"', to_dot(g))

    def test_html_self_contained_and_safe(self):
        evil = "</script><img src=x onerror=alert(1)>"
        ctx = _ctx("c", "2026-01-01", ["a"]).replace("first line of c", evil)
        g = self._graph({"work/app/a.md": _mem("a").replace("about a", evil),
                         "work/app/contexts/2026-01-01-c.md": ctx})
        html = to_html(g)
        self.assertNotIn("http", html)
        self.assertNotIn("</script><img", html)
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("innerHTML", html)
        blob = html.split("const data = ", 1)[1].split(";\nconst cv", 1)[0]
        self.assertEqual(json.loads(blob), to_json(g))


if __name__ == "__main__":
    unittest.main()
