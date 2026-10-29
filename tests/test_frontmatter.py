import unittest

from lrdmem.frontmatter import dump, parse, split, unsupported


class FrontmatterTest(unittest.TestCase):
    def test_parses_existing_shape(self):
        d = parse("---\nname: a\ndescription: TEC repos: x\nmetadata:\n  type: feedback\n  updated: 2026-10-28\n---\nbody\n")
        self.assertEqual(d["description"], "TEC repos: x")
        self.assertEqual(d["metadata"], {"type": "feedback", "updated": "2026-10-28"})

    def test_inline_lists_and_quotes(self):
        d = parse('---\ntriggers:\n  paths: ["**/*.php", src/a b.py]\nsupersedes: [old]\n---\n')
        self.assertEqual(d["triggers"]["paths"], ["**/*.php", "src/a b.py"])
        self.assertEqual(d["supersedes"], ["old"])

    def test_crlf_and_missing(self):
        self.assertEqual(parse("---\r\nname: a\r\n---\r\n")["name"], "a")
        self.assertIsNone(parse("no frontmatter"))

    def test_roundtrip(self):
        d = {"name": "a", "triggers": {"paths": ["**/*.php"]}, "memories": ["x", "y"]}
        self.assertEqual(parse(dump(d) + "\n"), d)

    def test_roundtrip_quoted_items(self):
        d = {"paths": ["a b", "c,d", "e:f", "g]"]}
        self.assertEqual(parse(dump(d) + "\n"), d)

    def test_roundtrip_quotes_and_empty(self):
        d = {"files": ["it's.py", 'say "hi".py', "", "plain"], "note": "", "q": '"x"', "ml": "a\nb", "l": "[x]"}
        self.assertEqual(parse(dump(d) + "\n"), d)

    def test_inline_map_list_roundtrip(self):
        d = {"evidence": [{"ref": "a/b", "kind": "supports", "weight": "1.0"},
                          {"ref": "x: y, z", "kind": "a b", "weight": "0.5"}], "plain": ["{x}", "k"]}
        text = dump(d) + "\n"
        self.assertEqual(parse(text), d)
        self.assertEqual(dump(parse(text)), dump(d))
        self.assertIn("evidence: [{ref: a/b, kind: supports, weight: 1.0}, ", text)
        self.assertEqual(unsupported(text), [])
        self.assertEqual(parse("---\ne: [{ref: a/b, kind: supports}, {ref: c}]\n---\n")["e"],
                         [{"ref": "a/b", "kind": "supports"}, {"ref": "c"}])

    def test_inline_map_nested_kept_as_raw_string(self):
        d = parse("---\ne: [{a: {b: c}, d: z}]\n---\n")
        self.assertEqual(d["e"], [{"a": "{b: c}", "d": "z"}])
        self.assertEqual(parse(dump(d) + "\n"), d)

    def test_bracket_items_and_values_roundtrip(self):
        d = {"p": ["a[b", "c"], "e": [{"k": "[", "j": "x"}]}
        self.assertEqual(parse(dump(d) + "\n"), d)
        self.assertEqual(parse("---\np: [a[b, c]\n---\n")["p"], ["a[b", "c"])

    def test_v01_brace_glob_and_map_like_item(self):
        self.assertEqual(parse("---\np: [src/{a,b}.py, c]\n---\n")["p"], ["src/{a,b}.py", "c"])
        self.assertEqual(parse("---\np: [{a:b}]\n---\n")["p"], [{"a": "b"}])

    def test_bad_map_key_rejected(self):
        for k in ("a b", "a:b", "a,b", "a}", "{a", ""):
            with self.assertRaises(ValueError):
                dump({"e": [{k: "v"}]})

    def test_v01_dump_bytes_unchanged(self):
        d = {"name": "a", "description": "x y: z", "metadata": {"type": "feedback", "updated": "2026-10-28"},
             "triggers": {"paths": ["**/*.php", "src/a b.py"], "commands": ["git push"]}, "supersedes": ["old"]}
        self.assertEqual(dump(d), '---\nname: a\ndescription: x y: z\nmetadata:\n  type: feedback\n  updated: 2026-10-28\n'
                         'triggers:\n  paths: [**/*.php, "src/a b.py"]\n  commands: ["git push"]\nsupersedes: [old]\n---')

    def test_split_returns_body_and_quotes_stripped(self):
        data, body = split("---\nname: 'a'\nother: \"b\"\n---\nhello\n")
        self.assertEqual(data, {"name": "a", "other": "b"})
        self.assertEqual(body, "hello\n")
        self.assertEqual(split("plain"), (None, "plain"))


if __name__ == "__main__":
    unittest.main()
