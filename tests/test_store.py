import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lrdmem.store import AmbiguousRepo, Store, repo_key, resolve, store_root
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


if __name__ == "__main__":
    unittest.main()
