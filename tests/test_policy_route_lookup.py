"""Typed-route correctness tests; no natural-language or model-quality claims."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_agent.public_policy import evidence_chars
from support_agent.knowledge_build import build_structure
from support_agent.public_policy_route_lookup import PolicyRouteLookup
from tests.test_public_policy_images import save
from tests.test_public_policy_structure import structure_fixture


class PolicyRouteLookupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus, *_rest = structure_fixture(self.root, route_rows=[
            ["北京", "上海", "09.00", "上海", "北京", "17", "北京", "上海", "09.00"],
            ["广东", "浙江", "12"],
        ])
        self.structure = self.root / "structure.json"
        build_structure(self.corpus, self.structure)
        self.lookup = PolicyRouteLookup(self.corpus, self.structure)

    def test_forward_reverse_amounts_are_distinct_and_raw_spelling_is_retained(self):
        forward = self.lookup.lookup("北京", "上海")
        reverse = self.lookup.lookup("上海", "北京")
        self.assertEqual(forward["status"], "evidence_found")
        self.assertEqual(forward["source_amount_text"], "09.00")
        self.assertEqual(reverse["source_amount_text"], "17")
        self.assertEqual(forward["requested_route"], {"origin": "北京", "destination": "上海"})
        self.assertFalse(forward["eligibility_evaluated"])
        self.assertEqual(forward["amount_semantics"], "source_table_value_not_order_payout")
        self.assertEqual(forward["human_review"], "pending")
        self.assertFalse(forward["effective_dates_verified"])
        self.assertIn("effective_dates_unverified", forward["warnings"])
        self.assertIn("source_policy_human_review_pending", forward["warnings"])
        self.assertEqual(forward["model_api_calls"], 0)
        self.assertEqual(forward["network_requests"], 0)

    def test_same_value_duplicates_keep_all_ids_but_return_one_complete_first_source_unit(self):
        before = self.lookup.index.units
        result = self.lookup.lookup("北京", "上海")
        matches = [unit for unit in before if unit["route"] == {"origin": "北京", "destination": "上海", "amount_text": "09.00"}]
        self.assertEqual(result["matching_record_count"], 2)
        self.assertEqual(result["duplicate_record_count"], 1)
        self.assertEqual(result["matched_unit_ids"], [unit["unit_id"] for unit in matches])
        self.assertEqual(result["evidence"], [{**matches[0], "score": 0}])
        self.lookup.index.validate_evidence({key: value for key, value in result["evidence"][0].items() if key != "score"})
        self.assertEqual(before, self.lookup.index.units)

    def test_unlisted_directed_pair_never_uses_reverse_amount_or_zero(self):
        self.assertEqual(self.lookup.lookup("广东", "浙江")["source_amount_text"], "12")
        result = self.lookup.lookup("浙江", "广东")
        self.assertEqual(result["status"], "route_not_listed")
        self.assertEqual(result["evidence"], [])
        self.assertIsNone(result["source_amount_text"])
        self.assertEqual(result["matching_record_count"], 0)
        self.assertIn("unlisted_route_is_not_ineligibility_or_zero_compensation", result["warnings"])

    def test_unknown_city_or_natural_language_endpoint_is_not_automatically_mapped(self):
        for origin in ("广州市", "Atlantis", "从北京寄到上海", " 北京 "):
            with self.subTest(origin=origin):
                result = self.lookup.lookup(origin, "上海")
                self.assertEqual(result["status"], "unsupported_route_endpoint")
                self.assertEqual(result["unsupported_endpoints"], ["origin"])
                self.assertEqual(result["requested_route"]["origin"], origin)
                self.assertIsNone(result["source_amount_text"])
                self.assertEqual(result["evidence"], [])
        result = self.lookup.lookup("Atlantis", "不存在")
        self.assertEqual(result["unsupported_endpoints"], ["origin", "destination"])

    def test_explicit_merchant_or_hk_region_and_source_scope_markers_are_out_of_scope(self):
        for kwargs in ({"merchant": "third_party"}, {"region": "HK"}):
            with self.subTest(kwargs=kwargs):
                result = self.lookup.lookup("北京", "上海", **kwargs)
                self.assertEqual(result["status"], "out_of_scope")
                self.assertEqual(result["scope_guard_status"], "scope_mismatch")
                self.assertIsNone(result["source_amount_text"])
                self.assertEqual(result["evidence"], [])
        result = self.lookup.lookup("第三方", "上海")
        self.assertEqual(result["status"], "out_of_scope")
        self.assertEqual(result["scope_guard_status"], "scope_warning")

    def test_full_serialized_unit_budget_is_exact_and_no_scalar_leaks_when_omitted(self):
        full = self.lookup.lookup("北京", "上海")
        budget = evidence_chars(full["evidence"])
        self.assertEqual(full["context_chars"], budget)
        self.assertGreater(budget, sum(len(span["text"]) for span in full["evidence"][0]["spans"]))
        self.assertEqual(self.lookup.lookup("北京", "上海", max_context_chars=budget)["evidence"], full["evidence"])
        for limit in (2, budget - 1):
            with self.subTest(limit=limit):
                limited = self.lookup.lookup("北京", "上海", max_context_chars=limit)
                self.assertEqual(limited["status"], "budget_exhausted")
                self.assertEqual(limited["context_chars"], 2)
                self.assertEqual(limited["evidence"], [])
                self.assertIsNone(limited["source_amount_text"])
                self.assertEqual(limited["matched_unit_ids"], full["matched_unit_ids"])

    def test_conflicting_amount_records_have_no_selected_evidence_or_scalar(self):
        temporary = self.root / "conflict"
        temporary.mkdir()
        corpus, *_rest = structure_fixture(temporary, route_rows=[
            ["北京", "上海", "9", "北京", "上海", "10", "上海", "北京", "17"],
        ])
        structure = temporary / "structure.json"
        build_structure(corpus, structure)
        result = PolicyRouteLookup(corpus, structure).lookup("北京", "上海")
        self.assertEqual(result["status"], "conflicting_route_records")
        self.assertEqual(result["matching_record_count"], 2)
        self.assertEqual(result["duplicate_record_count"], 0)
        self.assertEqual(len(result["matched_unit_ids"]), 2)
        self.assertEqual(result["evidence"], [])
        self.assertIsNone(result["source_amount_text"])

    def test_equal_decimal_values_with_different_source_spellings_keep_first_original_text(self):
        temporary = self.root / "equal-values"
        temporary.mkdir()
        corpus, *_rest = structure_fixture(temporary, route_rows=[
            ["北京", "上海", "09.00", "北京", "上海", "9", "上海", "北京", "17"],
        ])
        structure = temporary / "structure.json"
        build_structure(corpus, structure)
        result = PolicyRouteLookup(corpus, structure).lookup("北京", "上海")
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["source_amount_text"], "09.00")
        self.assertEqual(result["duplicate_record_count"], 1)

    def test_parameter_types_and_budget_use_existing_strict_validation(self):
        for origin, destination, kwargs in ((None, "上海", {}), ("北京", [], {}), (" ", "上海", {}),
                                           ("北京", "上海", {"max_context_chars": True}),
                                           ("北京", "上海", {"max_context_chars": 1}),
                                           ("北京", "上海", {"merchant": None})):
            with self.subTest(origin=origin, destination=destination, kwargs=kwargs), self.assertRaises(ValueError):
                self.lookup.lookup(origin, destination, **kwargs)

    def test_tampered_sidecar_or_source_is_rejected_before_lookup(self):
        original = self.structure.read_bytes()
        payload = json.loads(original)
        route = next(unit for unit in payload["units"] if unit["kind"] == "route_record")
        route["route"]["amount_text"] = "999"
        save(self.structure, payload)
        with self.assertRaises(ValueError):
            PolicyRouteLookup(self.corpus, self.structure).lookup("北京", "上海")
        self.structure.write_bytes(original)
        corpus = json.loads(self.corpus.read_text(encoding="utf-8"))
        raw = self.corpus.parent / corpus["documents"][0]["raw_path"]
        raw.write_bytes(raw.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            PolicyRouteLookup(self.corpus, self.structure)

    def test_instance_reuses_validated_snapshot_and_returned_evidence_cannot_mutate_it(self):
        returned = self.lookup.lookup("北京", "上海")
        original = deepcopy(returned)
        returned["evidence"][0]["spans"][-1]["text"] = "changed"
        with patch("support_agent.public_policy_route_lookup.StructureIndex", side_effect=AssertionError("must not reload")):
            repeated = self.lookup.lookup("北京", "上海")
            reverse = self.lookup.lookup("上海", "北京")
        self.assertEqual(original, repeated)
        self.assertEqual(reverse["source_amount_text"], "17")


if __name__ == "__main__":
    unittest.main()
