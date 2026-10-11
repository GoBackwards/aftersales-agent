"""The authored card catalogue bound to a release (B6: whole, under the publication cap), separate from answer quality."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.catalog import (CATALOG_PATH, MAX_CATALOGUE_CARDS, MAX_MODEL_VIEW_CHARS, cap_problem,
                                       model_view, model_view_chars)
from support_agent.policy import (PolicyAdapter, PublicPolicyConfig, load_policy_knowledge,
                                        resolve_policy_config)
from tests.test_public_policy_bundles import bundle_fixture


APP = Path(__file__).resolve().parents[1]
STORE = APP / "var/knowledge-store"


class GraphCardCatalogValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="graph-policy-card-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = PublicPolicyConfig(*bundle_fixture(self.root))
        self.knowledge = load_policy_knowledge(self.config)
        sources = [item for key, item in self.knowledge.evidence.items() if key.startswith("parent:")][:2]
        self.sources = sources
        self.catalog = {"schema_version": "graph-policy-cards-v1", "human_reviewed": False,
            "knowledge_release_id": "c" * 64, "corpus_sha256": self.knowledge.metadata["corpus_sha256"],
            "cards": [self.card("independent_example", "测试两段来源的完整条件")]}
        self.path = self.root / "cards.json"
        self.write_catalog(self.catalog)

    def card(self, identifier, title, **changes):
        card = {"id": identifier, "title": title, "scope": "仅限测试",
                "conditions": ["需要同时核实两份条件"], "outcomes": ["符合条件后适用测试结果"],
                "exceptions": ["例外另行核对"], "support_scope_ids": ["synthetic"],
                "evidence_ids": [item["id"] for item in self.sources],
                "references": [{"evidence_id": item["id"], "parent_sha256": item["sha256"],
                                "quotes": [item["text"][:16]]} for item in self.sources]}
        card.update(changes)
        return card

    def write_catalog(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def adapter(self):
        return PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=self.path)

    def test_invalid_binding_missing_evidence_bad_quotes_or_premises_fail_initialization(self):
        variants = []
        missing_release = deepcopy(self.catalog)
        missing_release.pop("knowledge_release_id")
        variants.append(missing_release)
        bad_parent = deepcopy(self.catalog)
        bad_parent["cards"][0]["references"][0]["parent_sha256"] = "0" * 64
        variants.append(bad_parent)
        bad_quote = deepcopy(self.catalog)
        bad_quote["cards"][0]["references"][0]["quotes"] = ["并不存在于已校验来源中的条件"]
        variants.append(bad_quote)
        no_quote = deepcopy(self.catalog)
        no_quote["cards"][0]["references"][0].pop("quotes")
        variants.append(no_quote)
        missing_source = deepcopy(self.catalog)
        missing_source["cards"][0]["evidence_ids"][0] = "parent:missing"
        missing_source["cards"][0]["references"][0]["evidence_id"] = "parent:missing"
        variants.append(missing_source)
        bad_refs = deepcopy(self.catalog)
        bad_refs["cards"][0]["references"].pop()
        variants.append(bad_refs)
        duplicate = deepcopy(self.catalog)
        duplicate["cards"].append(deepcopy(duplicate["cards"][0]))
        variants.append(duplicate)
        for field, value in (("schema_version", "unknown"), ("human_reviewed", True)):
            identity = deepcopy(self.catalog)
            identity[field] = value
            variants.append(identity)
        missing_condition = deepcopy(self.catalog)
        missing_condition["cards"][0]["conditions"] = []
        variants.append(missing_condition)
        # B6: the premises check moved from the per-turn card filter into the loader.
        for premises in ([], [["前提"], ["多出的一组"]], [[""]], [[]]):
            bad_premises = deepcopy(self.catalog)
            bad_premises["cards"][0]["outcome_premises"] = premises
            variants.append(bad_premises)
        for value in variants:
            with self.subTest(value=value):
                self.write_catalog(value)
                with self.assertRaises(ValueError):
                    self.adapter()

    def test_the_bound_catalogue_is_whole_in_order_and_detached(self):
        self.catalog["cards"].append(self.card("second_example", "测试第二张卡片"))
        self.write_catalog(self.catalog)
        adapter = self.adapter()
        self.assertEqual(adapter.catalogue_cards(), self.catalog["cards"])
        self.assertEqual(adapter._metadata["card_catalog_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        adapter.catalogue_cards()[0]["outcomes"][0] = "被改过的结论"
        self.assertEqual(adapter.catalogue_cards()[0]["outcomes"], ["符合条件后适用测试结果"])
        # The shared raw knowledge never holds catalogue data, and an adapter without a catalogue has no cards.
        self.assertEqual(PolicyAdapter(self.config, knowledge=self.knowledge).catalogue_cards(), [])
        self.assertFalse(any(key.startswith("card:") for key in self.knowledge.evidence))
        sources = adapter.source_cards(self.catalog["cards"][0]["evidence_ids"])
        self.assertEqual([card["citation_id"] for card in sources], self.catalog["cards"][0]["evidence_ids"])

    def test_different_unpinned_fixture_corpus_explicitly_falls_back_to_no_cards(self):
        self.catalog["corpus_sha256"] = "d" * 64
        self.write_catalog(self.catalog)
        adapter = self.adapter()
        self.assertEqual(adapter.catalogue_cards(), [])
        self.assertEqual(adapter._metadata["policy_card_catalog"]["status"], "disabled_fixture_corpus_mismatch")

    def test_a_catalogue_over_the_publication_cap_is_refused_and_one_at_the_cap_is_bound(self):
        # D15: at most 60 cards and 24,000 characters of model view, so the whole view fits the answer context.
        at_cap = deepcopy(self.catalog)
        at_cap["cards"] = [self.card(f"card_{index:02d}", f"测试卡片{index}") for index in range(MAX_CATALOGUE_CARDS)]
        self.assertLessEqual(model_view_chars(at_cap["cards"]), MAX_MODEL_VIEW_CHARS)
        self.write_catalog(at_cap)
        self.assertEqual(len(self.adapter().catalogue_cards()), MAX_CATALOGUE_CARDS)
        too_many = deepcopy(at_cap)
        too_many["cards"].append(self.card("card_extra", "测试多出的卡片"))
        long_text = "很长的测试条件" * 1300
        too_long = deepcopy(self.catalog)
        too_long["cards"] = [self.card(f"long_{index}", f"测试长卡片{index}", conditions=[long_text]) for index in range(3)]
        self.assertGreater(model_view_chars(too_long["cards"]), MAX_MODEL_VIEW_CHARS)
        for label, value in (("cards", too_many), ("characters", too_long)):
            with self.subTest(over=label):
                self.assertTrue(cap_problem(value["cards"]))
                self.write_catalog(value)
                with self.assertRaisesRegex(ValueError, "GRAPH_CARD_CATALOG_OVER_CAP"):
                    self.adapter()

    def test_the_model_view_holds_the_cards_words_and_premises_but_no_sources(self):
        card = self.card("viewed", "测试视图卡片", outcomes=["结果一", "结果二"], outcome_premises=[["前提一"], ["前提二"]],
                         effect_domain="return_items")
        [view] = model_view([card])
        self.assertEqual(set(view), {"id", "title", "scope", "conditions", "outcomes", "exceptions", "effect_domain",
                                     "outcome_premises"})
        self.assertEqual(view["outcome_premises"], [["前提一"], ["前提二"]])
        unmapped = model_view([self.card("unmapped", "测试未映射分支", outcomes=["结果一", "结果二"])])[0]
        self.assertEqual((unmapped["outcome_premises"], unmapped["effect_domain"]), ([None, None], "unclassified"))
        self.assertEqual(model_view_chars([card]), len(json.dumps([view], ensure_ascii=False)))


@unittest.skipUnless(STORE.exists(), "existing local frozen release is not distributed with source")
class GraphRealPolicyCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = resolve_policy_config(STORE)
        cls.knowledge = load_policy_knowledge(cls.config)
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    def adapter(self):
        return PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=CATALOG_PATH)

    def test_the_whole_repository_catalogue_is_bound_and_fits_the_answer_context(self):
        cards = self.adapter().catalogue_cards()
        self.assertEqual([card["id"] for card in cards], [card["id"] for card in self.catalog["cards"]])
        self.assertEqual(len(cards), 24)
        self.assertFalse(cap_problem(cards))
        self.assertLess(model_view_chars(cards), MAX_MODEL_VIEW_CHARS)

    def test_an_exception_list_and_its_base_rule_are_both_always_citable(self):
        # The companion cards of the retrieval era (an exception card pulled in its base rule) are not needed: every
        # card is in the view every turn.
        ids = [card["id"] for card in model_view(self.adapter().catalogue_cards())]
        self.assertTrue({"seven_day_return", "seven_day_excluded_categories", "digital_activation"} <= set(ids))

    def test_a_multi_document_card_shows_both_sources_in_the_drawer(self):
        adapter = self.adapter()
        card = next(card for card in adapter.catalogue_cards() if card["id"] == "seven_day_return")
        sources = adapter.source_cards(card["evidence_ids"])
        self.assertEqual({source["citation_id"] for source in sources}, set(card["evidence_ids"]))
        self.assertEqual(len({source["parent_citation_id"].split("@")[0] for source in sources}), 2)
        self.assertTrue(any("退货或换货" in outcome for outcome in card["outcomes"]))
        self.assertEqual(len(card["outcome_premises"]), len(card["outcomes"]))
        self.assertIn("个人原因导致的退货或换货", card["outcome_premises"][0][0])
        self.assertNotIn("详情页带有", card["outcome_premises"][0][0])
        self.assertIn("详情页带有", card["outcome_premises"][1][0])

    def test_every_card_source_reaches_the_drawer_as_the_release_has_it(self):
        adapter = self.adapter()
        for card in adapter.catalogue_cards():
            with self.subTest(card=card["id"]):
                for source in adapter.source_cards(card["evidence_ids"]):
                    canonical = self.knowledge.evidence[source["citation_id"]]
                    self.assertEqual((source["content"], source["sha256"]), (canonical["text"], canonical["sha256"]))

    def test_gift_and_shipping_card_branches_keep_their_source_conditions(self):
        cards = {card["id"]: card for card in self.catalog["cards"]}
        general = cards["gift_general"]
        lost = cards["gift_loss_damage"]
        shipping = cards["return_shipping_responsibility"]
        self.assertTrue(any("无法全额退款" in quote for ref in general["references"]
                            for quote in ref["quotes"]))
        self.assertFalse(any("赠品价值" in text for text in general["outcomes"]))
        self.assertTrue(any("未退回" in text and "遗失或破损" in text for text in general["exceptions"]))
        self.assertTrue(any("仅仅未退回" in text for text in lost["conditions"]))
        self.assertTrue(any("确认属于这一情形" in text for text in lost["outcomes"]))
        self.assertEqual(len(lost["outcome_premises"]), len(lost["outcomes"]))
        self.assertIn("赠品遗失或破损", lost["outcome_premises"][0][0])
        self.assertIn("发票遗失", lost["outcome_premises"][1][0])
        self.assertTrue(any("适用范围：自营商品，由京东发货" in quote
                            for ref in shipping["references"] for quote in ref["quotes"]))
        self.assertTrue(any("京东发货" in text and "自营商品" in text for text in shipping["outcomes"]))

    def test_each_multi_outcome_card_has_branch_specific_premises(self):
        cards = {card["id"]: card for card in self.catalog["cards"]}
        self.assertEqual(len(cards), 24)
        self.assertTrue(all(type(card.get("effect_domain")) is str and card["effect_domain"]
                            for card in cards.values()))
        self.assertEqual(cards["free_pickup_benefit"]["effect_domain"], "return_shipping_benefit")
        self.assertEqual(cards["seven_day_return"]["effect_domain"], "return_eligibility")
        self.assertEqual(cards["pickup_prices"]["effect_domain"], "return_pickup_fee")
        self.assertNotEqual(cards["free_pickup_benefit"]["effect_domain"],
                            cards["seven_day_return"]["effect_domain"])
        for card in cards.values():
            for premises in card.get("outcome_premises", []):
                self.assertFalse(any("已核实" in text for text in premises), card["id"])
        multi = [card for card in cards.values() if len(card["outcomes"]) > 1]
        self.assertEqual(len(multi), 14)
        for card in multi:
            with self.subTest(card=card["id"]):
                self.assertEqual(len(card["outcome_premises"]), len(card["outcomes"]))
                self.assertTrue(all(type(branch) is list and branch and
                                    all(type(item) is str and item.strip() for item in branch)
                                    for branch in card["outcome_premises"]))
        self.assertIn("京东快递", cards["receipt_clock"]["outcome_premises"][0][0])
        self.assertIn("并非京东配送", cards["receipt_clock"]["outcome_premises"][1][0])
        self.assertIn("存储类商品", cards["digital_repair_preparation"]["outcome_premises"][1][0])
        self.assertEqual(len(cards["gift_seven_day_branches"]["outcomes"]), 3)
        self.assertIn("拆分为订单", cards["gift_seven_day_branches"]["outcome_premises"][2][0])
        self.assertIn("同一售后服务单", cards["free_pickup_benefit"]["outcome_premises"][1][0])
        self.assertIn("运费险", cards["free_benefit_self_post"]["outcome_premises"][1][0])
        self.assertIn("实际分配为上门换新", cards["exchange_methods"]["outcome_premises"][0][0])
        self.assertIn("实际分配为返件换新", cards["exchange_methods"]["outcome_premises"][1][0])
        quality = cards["quality_return_exchange"]
        self.assertEqual(len(quality["outcomes"]), 3)
        self.assertIn("七天（含）", quality["outcome_premises"][0][0])
        self.assertIn("十五天（含）", quality["outcome_premises"][1][0])
        self.assertIn("自营小时购", quality["outcome_premises"][1][0])
        shipping = cards["return_shipping_responsibility"]
        self.assertEqual(len(shipping["outcomes"]), 3)
        self.assertIn("非 PLUS", shipping["outcome_premises"][0][0])
        self.assertIn("为 PLUS", shipping["outcome_premises"][1][0])
        self.assertIn("非客户原因", shipping["outcome_premises"][2][0])
        repair = cards["repair_shipping_responsibility"]
        self.assertEqual(len(repair["outcomes"]), 3)
        self.assertIn("人为损坏", repair["outcome_premises"][0][0])
        self.assertIn("保修期内", repair["outcome_premises"][1][0])
        self.assertIn("超过保修期", repair["outcome_premises"][2][0])

    def test_two_parents_from_same_document_preserve_carrier_scope_and_exception(self):
        adapter = self.adapter()
        card = next(card for card in adapter.catalogue_cards() if card["id"] == "pickup_carrier")
        sources = adapter.source_cards(card["evidence_ids"])
        self.assertEqual(len(sources), 2)
        self.assertEqual(len({source["parent_citation_id"].split("#")[0] for source in sources}), 1)
        text = "\n".join(source["content"] for source in sources)
        self.assertIn("带有免费上门退换服务标识", text)
        self.assertIn("实际分配承运商为准", text)

    def test_wrong_release_or_corpus_never_falls_back_for_a_pinned_config(self):
        with tempfile.TemporaryDirectory(prefix="graph-wrong-card-") as temporary:
            path = Path(temporary) / "cards.json"
            for field in ("knowledge_release_id", "corpus_sha256"):
                value = deepcopy(self.catalog)
                value[field] = "0" * 64
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
                    PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=path)


if __name__ == "__main__":
    unittest.main()
