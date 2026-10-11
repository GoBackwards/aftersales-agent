"""Synthetic bundle boundary tests; no real-model or policy-accuracy claims."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.public_policy import evidence_chars
from support_agent.public_policy_bundles import PolicyEvidenceBundles
from support_agent.knowledge_build import build_structure
from support_agent.public_policy_structure import StructureIndex
from tests.test_public_policy_images import save, sha
from tests.test_public_policy_structure import structure_fixture


def bundle_fixture(root, *, route_rows=None):
    """Return corpus, structure, relations using invented local source material."""
    root = Path(root)
    corpus, _old, _image = structure_fixture(root, route_rows=route_rows)
    structure = root / "structure.json"
    build_structure(corpus, structure)
    index = StructureIndex(corpus, structure)
    unit = next(unit for unit in index.units if unit["kind"] == "whole_parent")
    source = index.source_index.evidence_by_id(unit["parent_citation_id"])
    text = source["content"]
    common = {"document_id": unit["document_id"], "chunk_id": unit["parent_chunk_id"],
              "parent_sha256": unit["parent_sha256"]}
    gift_start = text.index("注：")
    def rule(rule_id, start, end):
        return {"rule_id": rule_id, **common, "purpose": "Invented test condition with its exception.",
                "spans": [{"start": start, "end": end, "sha256": sha(text[start:end].encode())}]}
    payload = {"schema_version": "public-policy-relations-v1", "corpus_sha256": index.corpus_sha256,
               "authored_by": "synthetic-test", "human_reviewed": False,
               "selection_method": "invented source selection, not real policy", "limitations": ["synthetic"],
               "topics": {"shipping_responsibility": ["responsibility"],
                          "shipping_compensation": ["responsibility", "conditions"],
                          "return_gifts": ["gifts"]},
               "rules": [rule("responsibility", 0, gift_start), rule("conditions", gift_start, len(text)),
                         rule("gifts", gift_start, len(text))]}
    relations = root / "relations.json"
    save(relations, payload)
    return corpus, structure, relations


class PublicPolicyBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus, self.structure, self.relations = bundle_fixture(self.root)
        self.builder = PolicyEvidenceBundles(self.corpus, self.structure, self.relations)

    def test_all_selected_spans_map_to_full_source_and_model_view_with_same_id(self):
        result = self.builder.build(["shipping_compensation", "return_gifts"])
        self.assertEqual(result["status"], "evidence_ready")
        self.assertTrue(result["complete"])
        backend = {item["evidence_id"]: item for item in result["backend_evidence"]}
        for item in result["model_view"]:
            full = backend[item["id"]]
            for compact, span in zip(item["spans"], full["spans"], strict=True):
                self.assertEqual(compact["text"], full["source"]["content"][compact["start"]:compact["end"]])
                self.assertEqual(span["sha256"], sha(compact["text"].encode()))
                self.assertEqual(span["document_start"], full["parent_start"] + span["start"])
                self.assertEqual(span["document_end"], full["parent_start"] + span["end"])
        self.assertIn("其他约定除外", json.dumps(result["model_view"], ensure_ascii=False))
        self.assertFalse(result["eligibility_evaluated"])
        self.assertEqual(result["model_api_calls"], 0)

    def test_gift_only_does_not_add_shipping_conditions(self):
        result = self.builder.build(["return_gifts"])
        self.assertEqual([item["rule_id"] for item in result["backend_evidence"]], ["gifts"])
        self.assertIsNone(result["source_amount_text"])

    def test_overlapping_topics_deduplicate_shared_requirements(self):
        result = self.builder.build(["shipping_responsibility", "shipping_compensation"])
        self.assertEqual([item["rule_id"] for item in result["backend_evidence"]], ["responsibility", "conditions"])

    def test_route_requires_compensation_branch_even_without_requested_topic(self):
        route = self.builder.route_lookup.lookup("甲", "乙")
        result = self.builder.build([], route_result=route)
        self.assertEqual(result["requested_topics"], ["shipping_compensation"])
        self.assertEqual(result["source_amount_text"], "09.00")
        self.assertEqual(result["route_verification_calls"], 1)
        self.assertEqual(result["model_view"][-1]["id"], "R1")
        self.assertEqual([span["text"] for span in result["model_view"][-1]["spans"]],
                         [span["text"] for span in route["evidence"][0]["spans"]])
        self.assertEqual(result["amount_semantics"], "source_table_value_not_order_payout")
        self.assertFalse(result["eligibility_evaluated"])

    def test_full_model_json_exact_budget_boundary_and_atomic_omission(self):
        route = self.builder.route_lookup.lookup("甲", "乙")
        first = self.builder.build(["return_gifts"], route_result=route)
        size = evidence_chars(first["model_view"])
        self.assertEqual(first["context_chars"], size)
        fits = self.builder.build(["return_gifts"], route_result=route, max_context_chars=size)
        self.assertEqual(fits["status"], "evidence_ready")
        too_small = self.builder.build(["return_gifts"], route_result=route, max_context_chars=size - 1)
        self.assertEqual(too_small["status"], "budget_exhausted")
        self.assertEqual(too_small["model_view"], [])
        self.assertIsNone(too_small["source_amount_text"])
        self.assertFalse(too_small["complete"])
        self.assertTrue(too_small["backend_evidence"])
        self.assertIn("explicit_directed_route", too_small["missing_requirements"])

    def test_full_backend_provenance_is_not_charged_as_compact_view(self):
        result = self.builder.build(["shipping_compensation"])
        self.assertGreater(evidence_chars(result["backend_evidence"]), result["context_chars"])
        encoded = json.dumps(result["model_view"])
        self.assertNotIn("sha256", encoded)
        self.assertNotIn("raw_path", encoded)
        self.assertEqual(result["context_budget_unit"], "compact_model_view_json_characters_v1")

    def test_image_common_clause_retains_transcription_review_warning(self):
        result = self.builder.build(["return_gifts"])
        self.assertIn("image_transcriptions_ai_reviewed_not_human", result["warnings"])
        source = result["backend_evidence"][0]["source"]
        self.assertIn("image", source["coverage"])
        self.assertEqual(result["human_review"], "pending")
        self.assertIn("赠品", result["model_view"][0]["spans"][0]["text"])

    def test_route_missing_does_not_use_reverse_or_make_zero(self):
        missing = self.builder.route_lookup.lookup("乙", "甲")
        result = self.builder.build(["return_gifts"], route_result=missing)
        self.assertEqual(result["status"], "route_unavailable")
        self.assertEqual(result["route_status"], "route_not_listed")
        self.assertIsNone(result["source_amount_text"])
        self.assertFalse(result["complete"])
        self.assertNotIn("R1", [item["id"] for item in result["model_view"]])

    def test_tampered_route_amount_direction_provenance_and_success_flag_are_rejected(self):
        route = self.builder.route_lookup.lookup("甲", "乙")
        for mutation in ("amount", "route", "offset", "hash", "status", "extra"):
            with self.subTest(mutation=mutation):
                altered = deepcopy(route)
                if mutation == "amount": altered["source_amount_text"] = "99"
                if mutation == "route": altered["requested_route"] = {"origin": "乙", "destination": "甲"}
                if mutation == "offset": altered["evidence"][0]["spans"][-1]["start"] += 1
                if mutation == "hash": altered["corpus_sha256"] = "0" * 64
                if mutation == "status": altered["status"] = "budget_exhausted"
                if mutation == "extra": altered["qualification"] = "guaranteed"
                with self.assertRaises(ValueError):
                    self.builder.build([], route_result=altered)

    def test_budget_limited_original_lookup_cannot_be_promoted_to_an_amount(self):
        route = self.builder.route_lookup.lookup("甲", "乙", max_context_chars=2)
        result = self.builder.build([], route_result=route)
        self.assertEqual(result["route_status"], "budget_exhausted")
        self.assertIsNone(result["source_amount_text"])

    def test_non_mainland_and_other_merchant_return_no_visible_evidence(self):
        for scope in ({"region": "HK"}, {"merchant": "third_party"}):
            with self.subTest(scope=scope):
                result = self.builder.build(["shipping_compensation"], **scope)
                self.assertEqual(result["status"], "out_of_scope")
                self.assertEqual(result["model_view"], [])
                self.assertEqual(result["backend_evidence"], [])

    def test_returned_evidence_mutation_cannot_change_later_builds(self):
        result = self.builder.build(["return_gifts"])
        expected = deepcopy(result)
        result["model_view"][0]["spans"][0]["text"] = "wrong"
        result["backend_evidence"][0]["source"]["content"] = "wrong"
        self.assertEqual(self.builder.build(["return_gifts"]), expected)

    def test_topics_and_budget_require_strict_types(self):
        for topics in ("return_gifts", [], ["unknown"], ["return_gifts", "return_gifts"], [False]):
            with self.subTest(topics=topics), self.assertRaises(ValueError):
                self.builder.build(topics)
        for budget in (True, 1, 20001, "3000", 3.0):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.builder.build(["return_gifts"], max_context_chars=budget)

    def test_manifest_wrong_corpus_parent_offsets_and_sha_fail_closed(self):
        original = json.loads(self.relations.read_text(encoding="utf-8"))
        for mutation in ("corpus", "parent", "bool", "offset", "hash", "missing", "dependency"):
            with self.subTest(mutation=mutation):
                altered = deepcopy(original)
                if mutation == "corpus": altered["corpus_sha256"] = "0" * 64
                if mutation == "parent": altered["rules"][0]["parent_sha256"] = "0" * 64
                if mutation == "bool": altered["rules"][0]["spans"][0]["start"] = False
                if mutation == "offset": altered["rules"][0]["spans"][0]["end"] += 1
                if mutation == "hash": altered["rules"][0]["spans"][0]["sha256"] = "0" * 64
                if mutation == "missing": altered["topics"]["return_gifts"] = ["missing"]
                if mutation == "dependency": altered["topics"]["shipping_compensation"] = ["conditions"]
                save(self.relations, altered)
                with self.assertRaises(ValueError):
                    PolicyEvidenceBundles(self.corpus, self.structure, self.relations)


if __name__ == "__main__":
    unittest.main()
