"""B6 (plan K2, D15): a catalogue over the publication cap is never published, activated or bound to a turn, and the
knowledge base previews the model view the answer reads instead of a search ranking.

Invented sources and cards only; these checks prove the size rule and what the preview shows, not that a rule is
correct or current.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.catalog import MAX_CATALOGUE_CARDS, MAX_MODEL_VIEW_CHARS, model_view, model_view_chars
from support_agent.kb_store import KBValidationError, _encode
from support_agent.service import _knowledge_base
from tests.test_graph_product_api import ALICE, ProductServerCase
from tests.test_kb_store import kb_fixture, proposal_for, synthetic_card

HAS_GRAPH = importlib.util.find_spec("pydantic") is not None


def numbered(item, count, *, start=0, conditions=None):
    """``count`` invented cards citing the same source passage."""
    cards = []
    for number in range(start, start + count):
        card = synthetic_card(f"numbered_{number:03d}", f"合成编号卡片{number}", item, "合成运费表")
        if conditions is not None:
            card["conditions"] = list(conditions)
        cards.append(card)
    return cards


class CatalogueCapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalogue-cap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.knowledge, baseline, self.items = kb_fixture(self.root)
        self.store = _knowledge_base(self.root / "kb", self.config, self.knowledge, baseline)

    def draft_with(self, cards):
        draft = self.store.draft()
        draft["catalog"]["cards"] = cards
        self.store._save_draft(draft)

    def test_publishing_a_draft_over_either_limit_is_refused_and_nothing_changes(self):
        active = self.store.active_pointer()
        base = self.store.active_catalog()["cards"]
        long_conditions = ["很长的合成条件文字" * 65] * 12   # within the editor's field limits, over the view cap together
        for label, cards in (("cards", base + numbered(self.items["table"], MAX_CATALOGUE_CARDS - len(base) + 1)),
                             ("characters", base + numbered(self.items["table"], 4, conditions=long_conditions))):
            with self.subTest(over=label):
                self.assertTrue(len(cards) > MAX_CATALOGUE_CARDS or model_view_chars(cards) > MAX_MODEL_VIEW_CHARS)
                self.draft_with(cards)
                report = self.store.validate_draft()
                self.assertFalse(report["ok"])
                self.assertIn("GRAPH_CARD_CATALOG_OVER_CAP", report["runtime_check"])
                with self.assertRaises(KBValidationError) as caught:
                    self.store.publish(label="超过上限的版本", author="kb-admin")
                self.assertIn("GRAPH_CARD_CATALOG_OVER_CAP", str(caught.exception))
                self.assertEqual(self.store.active_pointer(), active)
                self.assertEqual(len(self.store.versions()), 1)

    def test_a_draft_at_the_cap_publishes_and_an_edit_past_it_is_refused(self):
        base = self.store.active_catalog()["cards"]
        self.draft_with(base + numbered(self.items["table"], MAX_CATALOGUE_CARDS - len(base)))
        published = self.store.publish(label="正好到上限", author="kb-admin")
        self.assertEqual(published["version"]["card_count"], MAX_CATALOGUE_CARDS)
        extra = proposal_for(synthetic_card("one_more", "合成多出的一张", self.items["table"], "合成运费表"))
        with self.assertRaises(KBValidationError) as caught:
            self.store.upsert_card(extra, author="kb-admin")
        self.assertIn("GRAPH_CARD_CATALOG_OVER_CAP", json.dumps(caught.exception.errors, ensure_ascii=False))
        self.assertEqual(len(self.store.draft()["catalog"]["cards"]), MAX_CATALOGUE_CARDS)

    def test_the_loader_a_turn_and_an_activation_use_refuses_an_over_cap_catalogue(self):
        catalogue = deepcopy(self.store.active_catalog())
        catalogue["cards"] += numbered(self.items["table"], MAX_CATALOGUE_CARDS)
        with self.assertRaisesRegex(ValueError, "GRAPH_CARD_CATALOG_OVER_CAP"):
            self.store.runtime_check_bytes(_encode(catalogue))


@unittest.skipUnless(HAS_GRAPH, "optional graph runtime not installed")
class ModelViewPreviewHTTPTests(ProductServerCase):
    def setUp(self):
        super().setUp()
        from support_agent.demo_model import KeywordDemoModel
        self.config, self.knowledge, baseline, self.items = kb_fixture(self.root)
        self.serve(demo_login=True, public_policy_config=self.config,
                   model_factory=KeywordDemoModel, kb_store=self.root / "kb", kb_baseline_catalog=baseline)
        self.admin = self.login("kb-admin")

    def test_the_preview_is_the_model_view_with_its_size_against_the_cap(self):
        status, preview = self.request("GET", "/kb/model-view?scope=active", token=self.admin)
        self.assertEqual(status, 200, preview)
        cards = self.server.kb.active_catalog()["cards"]
        self.assertEqual(preview["view"], model_view(cards))
        self.assertEqual((preview["card_count"], preview["chars"], preview["over_cap"]),
                         (len(cards), model_view_chars(cards), ""))
        self.assertEqual(preview["limits"], {"cards": MAX_CATALOGUE_CARDS, "chars": MAX_MODEL_VIEW_CHARS})
        self.assertFalse(any("references" in card or "evidence_ids" in card for card in preview["view"]))
        self.assertTrue(preview["note"])
        card = proposal_for(synthetic_card("fixture_extra", "合成附加规则", self.items["table"], "合成运费表"))
        self.assertEqual(self.request("POST", "/kb/draft/cards", {"card": card}, token=self.admin)[0], 200)
        draft = self.request("GET", "/kb/model-view?scope=draft", token=self.admin)[1]
        self.assertEqual([row["id"] for row in draft["view"]], [row["id"] for row in preview["view"]] + ["fixture_extra"])
        self.assertGreater(draft["chars"], preview["chars"])

    def test_the_preview_is_for_the_administrator_only_and_the_search_preview_is_gone(self):
        self.assertEqual(self.request("GET", "/kb/model-view?scope=active", token=ALICE)[0], 403)
        self.assertEqual(self.request("GET", "/kb/model-view?scope=everything", token=self.admin)[0], 400)
        self.assertEqual(self.request("POST", "/kb/retrieval-preview", {"query": "合成", "scope": "draft"},
                                      token=self.admin)[0], 404)


if __name__ == "__main__":
    unittest.main()
