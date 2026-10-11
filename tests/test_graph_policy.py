"""Evidence integrity of the policy adapter (B6: no retrieval), with invented fixtures, not answer-quality scores."""
from pathlib import Path
import tempfile
import unittest

from support_agent.policy import PolicyAdapter, PublicPolicyConfig
from tests.test_public_policy_bundles import bundle_fixture


class GraphPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        paths = bundle_fixture(Path(self.temporary.name))
        self.config = PublicPolicyConfig(*paths)
        self.adapter = PolicyAdapter(self.config)

    def test_no_configuration_means_no_cards_no_sources_and_no_calls(self):
        adapter = PolicyAdapter()
        self.assertEqual(adapter.catalogue_cards(), [])
        self.assertEqual(adapter.source_cards([]), [])
        with self.assertRaisesRegex(ValueError, "UNKNOWN_EVIDENCE"):
            adapter.source_cards(["invented"])
        self.assertEqual(adapter.route_reference("甲", "乙"), {"status": "unconfigured", "source_amount_text": None})

    def test_without_a_catalogue_the_release_offers_no_cards(self):
        self.assertEqual(self.adapter.catalogue_cards(), [])

    def test_the_adapter_has_no_search(self):
        # B6 (plan §7.3): the BM25 ranking, the multi-query assembly and the companion cards are gone.
        for name in ("retrieve", "_rank", "_rank_cards", "_attach_companions"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.adapter, name))

    def test_a_whole_parent_and_its_table_header_reach_the_source_drawer_as_saved(self):
        table = next(item for key, item in self.adapter._evidence.items()
                     if key.startswith("parent:") and "发货地" in item["text"])
        [card] = self.adapter.source_cards([table["id"]])
        canonical = self.adapter._index.evidence_by_id(table["parent_citation_id"])
        self.assertEqual(card["content"], canonical["content"])
        self.assertIn("甲 | 乙 | 09.00", card["content"])
        self.assertEqual((card["spans"][0]["start"], card["spans"][0]["end"]), (0, len(canonical["content"])))

    def test_a_rule_reaches_the_drawer_with_its_spans_bound_to_the_source(self):
        cards = self.adapter.source_cards(["rule:conditions", "rule:responsibility", "rule:conditions"])
        self.assertEqual([card["rule_id"] for card in cards], ["conditions", "responsibility"])   # order given, once
        self.assertIn("其他约定除外", "\n".join(card["content"] for card in cards))
        for card in cards:
            source = self.adapter._index.evidence_by_id(card["parent_citation_id"])
            self.assertEqual(card["url"], source["url"])
            for span in card["spans"]:
                self.assertEqual(span["text"], source["content"][span["start"]:span["end"]])

    def test_an_unknown_or_malformed_selection_is_refused(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_EVIDENCE"):
            self.adapter.source_cards(["rule:responsibility", "not-in-the-release"])
        for value in ("rule:responsibility", [1], None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SELECTION_INVALID"):
                self.adapter.source_cards(value)

    def test_returned_sources_are_detached_from_the_shared_snapshot(self):
        first = self.adapter.source_cards(["rule:conditions"])
        first[0]["content"] = "changed"
        first[0]["spans"][0]["text"] = "changed"
        fresh = self.adapter.source_cards(["rule:conditions"])
        self.assertNotEqual(fresh[0]["content"], "changed")
        self.assertNotEqual(fresh[0]["spans"][0]["text"], "changed")


if __name__ == "__main__":
    unittest.main()
