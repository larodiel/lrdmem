import unittest

from lrdmem.frontmatter import dump, parse, split


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

    def test_split_returns_body_and_quotes_stripped(self):
        data, body = split("---\nname: 'a'\nother: \"b\"\n---\nhello\n")
        self.assertEqual(data, {"name": "a", "other": "b"})
        self.assertEqual(body, "hello\n")
        self.assertEqual(split("plain"), (None, "plain"))


if __name__ == "__main__":
    unittest.main()
