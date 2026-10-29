import unittest

from lrdmem import model


class ModelTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(model.MEMORY_TYPES, ("episodic", "semantic", "procedural", "hypothesis", "prospective", "metacognitive"))
        self.assertEqual(model.STATES, ("candidate", "active", "contested", "superseded", "obsolete"))
        self.assertEqual(len(model.EDGE_TYPES), 9)
        self.assertEqual(model.EDGE_STATES, ("proposed", "validated", "invalidated", "superseded"))
        self.assertEqual(len(model.EXPERIENCE_KINDS), 9)
        self.assertEqual(model.SOURCES, ("chat", "pr", "run", "check"))
        self.assertEqual(model.EVIDENCE_KINDS, ("supports", "refutes", "observed_in"))
        self.assertEqual(len(model.REVISION_ACTIONS), 8)

    def test_evidence_roundtrip(self):
        e = model.Evidence.from_dict({"ref": "a/b", "kind": "supports", "weight": "1.0"})
        self.assertEqual(e, model.Evidence("a/b", "supports", 1.0))
        self.assertEqual(model.Evidence.from_dict({k: str(v) for k, v in e.to_dict().items()}), e)

    def test_evidence_from_dict_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            model.Evidence.from_dict({"ref": "a", "kind": "bogus", "weight": "1"})

    def test_evidence_rejects_bad_weight(self):
        for w in ("heavy", "nan", "inf"):
            with self.assertRaises(ValueError):
                model.Evidence.from_dict({"ref": "a", "kind": "supports", "weight": w})

    def test_evidence_rejects_empty_ref_and_out_of_range_weight(self):
        for d in ({"ref": "", "kind": "supports", "weight": "1"}, {"ref": "a", "kind": "supports", "weight": "1.5"},
                  {"ref": "a", "kind": "supports", "weight": "-0.1"}):
            with self.assertRaises(ValueError):
                model.Evidence.from_dict(d)

    def test_v01_kind_mapping(self):
        for k, t in (("obligation", "procedural"), ("pattern", "procedural"), ("fact", "semantic"), ("zzz", "semantic")):
            self.assertEqual(model.v01_memory_type(k), t)


if __name__ == "__main__":
    unittest.main()
