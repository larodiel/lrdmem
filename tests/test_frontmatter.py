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
        d = parse("---\ne: [{a: {b: c}, d: [x, y]}]\n---\n")
        self.assertEqual(d["e"], [{"a": "{b: c}", "d": "[x, y]"}])
        self.assertEqual(parse(dump(d) + "\n"), d)

    def test_split_returns_body_and_quotes_stripped(self):
        data, body = split("---\nname: 'a'\nother: \"b\"\n---\nhello\n")
        self.assertEqual(data, {"name": "a", "other": "b"})
        self.assertEqual(body, "hello\n")
        self.assertEqual(split("plain"), (None, "plain"))


if __name__ == "__main__":
    unittest.main()
