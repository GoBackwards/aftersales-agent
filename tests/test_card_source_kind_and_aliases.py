"""Card source kind and aliases (B5d1, plan §8.6 B5d): the catalogue fields, the /kb release flow and the loader.

source_kind feeds guard G5 (decision D16: no card is a law source); aliases with the titles make guard G3's
rule-name lexicon. Neither field reaches the answer model. Invented sources only, except the repository
catalogue itself; these checks prove field handling and source binding, not that a rule is right.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.catalog import CATALOG_PATH, SOURCE_KINDS, model_view
from support_agent.policy import PolicyAdapter
from support_agent.guards import rule_names
from support_agent.kb_sources import UPLOAD_SCOPE_NOTE
from support_agent.kb_store import KBValidationError, _encode, card_changed, diff_catalogs, normalize_card
from support_agent.service import _knowledge_base
from tests.test_graph_product_api import ProductServerCase
from tests.test_kb_store import kb_fixture, proposal_for, synthetic_card

HAS_GRAPH = importlib.util.find_spec("pydantic") is not None
CATALOGUE = json.loads(Path(CATALOG_PATH).read_text(encoding="utf-8"))


def own_text(card):
    return "\n".join([card["title"], card["scope"], *card["conditions"], *card["outcomes"], *card["exceptions"],
                      *(part for rows in card.get("outcome_premises", []) for part in rows),
                      *(quote for ref in card["references"] for quote in ref["quotes"])])


class RepositoryCatalogueTests(unittest.TestCase):
    def test_every_card_is_a_platform_rule_and_none_is_a_law_source(self):
        self.assertEqual({card["source_kind"] for card in CATALOGUE["cards"]}, {"platform"})
        self.assertNotIn("law", SOURCE_KINDS)

    def test_every_alias_is_written_in_its_own_card(self):
        aliased = {card["id"]: card["aliases"] for card in CATALOGUE["cards"] if card["aliases"]}
        self.assertEqual(sorted(aliased), ["free_pickup_benefit", "self_post_without_benefit", "seven_day_return",
                                           "warranty_18m"])
        for card in CATALOGUE["cards"]:
            for alias in card["aliases"]:
                with self.subTest(card=card["id"], alias=alias):
                    self.assertIn(alias, own_text(card))

    def test_the_rule_name_lexicon_is_every_title_and_alias(self):
        names = rule_names(CATALOGUE["cards"])
        self.assertEqual(names, {name for card in CATALOGUE["cards"] for name in [card["title"], *card["aliases"]]})
        self.assertEqual(len(names), 24 + 5)

    def test_the_answer_model_never_sees_the_source_kind_or_the_aliases(self):
        for item in model_view(CATALOGUE["cards"]):
            with self.subTest(card=item["id"]):
                self.assertFalse({"source_kind", "aliases"} & set(item))


class NormalizeCardTests(unittest.TestCase):
    EVIDENCE = {"parent:a": {"text": "甲乙丙丁戊己庚辛，合成换新无忧条款", "sha256": "a" * 64}}

    def proposal(self, **changes):
        value = {"id": "demo_card", "title": "合成规则", "scope": "合成范围", "effect_domain": "general_guidance",
                 "conditions": ["合成条件"], "outcomes": ["合成结论"], "outcome_premises": [["合成前提"]],
                 "references": [{"evidence_id": "parent:a", "quotes": ["合成换新无忧条款"]}]}
        value.update(changes)
        return value

    def test_the_editor_keeps_the_source_kind_and_the_aliases(self):
        card = normalize_card(self.proposal(source_kind="merchant", aliases=["换新无忧"]), evidence=self.EVIDENCE)
        self.assertEqual((card["source_kind"], card["aliases"]), ("merchant", ["换新无忧"]))
        plain = normalize_card(self.proposal(), evidence=self.EVIDENCE)
        self.assertEqual((plain["source_kind"], plain["aliases"]), ("platform", []))

    def test_an_edit_that_leaves_the_fields_out_keeps_the_cards_own(self):
        existing = {"source_kind": "merchant", "aliases": ["换新无忧"], "support_scope_ids": ["kb-editor"]}
        card = normalize_card(self.proposal(), evidence=self.EVIDENCE, existing_card=existing)
        self.assertEqual((card["source_kind"], card["aliases"]), ("merchant", ["换新无忧"]))

    def test_a_law_source_an_invented_alias_and_a_repeated_alias_are_refused(self):
        cases = {"source_kind": self.proposal(source_kind="law"),
                 "aliases[0]": self.proposal(aliases=["来源里没有的名字"]),
                 "aliases": self.proposal(aliases=["换新无忧", "换新无忧"])}
        for field, proposal in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(KBValidationError) as caught:
                    normalize_card(proposal, evidence=self.EVIDENCE)
                self.assertIn(field, {error["field"] for error in caught.exception.errors})

    def test_changes_to_the_fields_show_in_the_card_diff(self):
        before = {"id": "a", "title": "甲"}
        self.assertEqual(card_changed(before, {**before, "source_kind": "platform", "aliases": []}), [])
        self.assertEqual(card_changed(before, {**before, "aliases": ["别名"]}), ["aliases"])
        diff = diff_catalogs({"cards": [before]}, {"cards": [{**before, "source_kind": "merchant"}]})
        self.assertEqual(diff["changed"], [{"id": "a", "title": "甲", "fields": ["source_kind"]}])

    def test_uploaded_sources_carry_a_production_voice_note(self):
        for word in ("演示", "模拟", "教学", "本地", "本项目"):
            with self.subTest(word=word):
                self.assertNotIn(word, UPLOAD_SCOPE_NOTE)


class StoreAndLoaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kb-fields-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.knowledge, self.baseline, self.items = kb_fixture(self.root)

    def write(self, catalog):
        path = self.root / "catalog.json"
        path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        return path

    def test_the_runtime_loader_refuses_a_law_source_and_malformed_aliases(self):
        catalog = json.loads(self.baseline.read_text(encoding="utf-8"))
        PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=self.write(catalog))   # no fields: fine
        named = catalog["cards"][0]["title"][:4]   # written in the card, so a fine alias on its own
        PolicyAdapter(self.config, knowledge=self.knowledge,
                           card_catalog=self.write({**catalog, "cards": [{**catalog["cards"][0], "aliases": [named]},
                                                                         *catalog["cards"][1:]]}))
        for change, code in (({"source_kind": "law"}, "GRAPH_CARD_SOURCE_KIND_INVALID"),
                             ({"aliases": "合成"}, "GRAPH_CARD_CATALOG_INVALID"),
                             ({"aliases": [""]}, None), ({"aliases": ["长" * 21]}, None),
                             # the editor's alias rule holds here too (a card edited on disk, a baseline card)
                             ({"aliases": ["卡里没写的叫法"]}, "GRAPH_CARD_ALIAS_INVALID"),
                             ({"aliases": [named, named]}, "GRAPH_CARD_ALIAS_INVALID")):
            broken = json.loads(json.dumps(catalog))
            broken["cards"][0].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ValueError) as caught:
                    PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=self.write(broken))
                if code:
                    self.assertEqual(str(caught.exception), code)

    def test_draft_validation_refuses_an_invented_alias_on_a_baseline_card(self):
        store = _knowledge_base(self.root / "kb", self.config, self.knowledge, self.baseline)
        draft = store.draft()
        draft["catalog"]["cards"][0]["aliases"] = ["卡里没写的叫法"]
        (self.root / "kb" / "draft.json").write_bytes(_encode(draft))
        report = store.validate_draft()
        self.assertEqual((report["ok"], report["runtime_check"]), (False, "GRAPH_CARD_ALIAS_INVALID"))
        with self.assertRaises(KBValidationError):
            store.publish(label="合成：篡改的别名", author="kb-admin")

    def test_publishing_a_card_with_the_fields_keeps_them_in_the_active_version(self):
        store = _knowledge_base(self.root / "kb", self.config, self.knowledge, self.baseline)
        card = proposal_for(synthetic_card("fixture_extra", "合成附加规则", self.items["table"], "合成运费表"),
                            source_kind="merchant", aliases=["合成运费表"])
        store.upsert_card(card, author="kb-admin")
        published = store.publish(label="合成：来源类型与别名", author="kb-admin")
        active = {card["id"]: card for card in store.active_catalog()["cards"]}
        self.assertEqual((active["fixture_extra"]["source_kind"], active["fixture_extra"]["aliases"]),
                         ("merchant", ["合成运费表"]))
        self.assertEqual(store.diff(published["version"]["version_id"])["diff"]["added"],
                         [{"id": "fixture_extra", "title": "合成附加规则"}])
        refused = proposal_for(synthetic_card("fixture_law", "合成条文", self.items["table"], "合成运费表"),
                               source_kind="law")
        with self.assertRaises(KBValidationError):
            store.upsert_card(refused, author="kb-admin")


@unittest.skipUnless(HAS_GRAPH, "optional graph runtime not installed")
class KnowledgeBaseEditorHTTPTests(ProductServerCase):
    def setUp(self):
        super().setUp()
        from support_agent.demo_model import KeywordDemoModel
        self.config, self.knowledge, baseline, self.items = kb_fixture(self.root)
        self.serve(demo_login=True, public_policy_config=self.config,
                   model_factory=KeywordDemoModel, kb_store=self.root / "kb", kb_baseline_catalog=baseline)
        self.admin = self.login("kb-admin")

    def test_the_kb_routes_offer_the_source_kinds_and_round_trip_the_fields(self):
        overview = self.request("GET", "/kb/overview", token=self.admin)[1]
        self.assertEqual(overview["source_kinds"], SOURCE_KINDS)
        card = {"id": "fixture_extra", "title": "合成附加规则", "scope": "仅限合成工程测试", "effect_domain": "return_items",
                "source_kind": "merchant", "aliases": ["合成运费表"], "conditions": ["合成附加条件"],
                "outcomes": ["合成附加规则时适用合成结果"], "outcome_premises": [["合成附加前提"]], "exceptions": [],
                "references": [{"evidence_id": self.items["table"]["id"], "quotes": ["合成运费表"]}]}
        status, result = self.request("POST", "/kb/draft/cards/check", {"card": {**card, "source_kind": "law"}},
                                      token=self.admin)
        self.assertEqual((status, result["error"]["details"][0]["field"]), (422, "source_kind"))
        self.assertEqual(self.request("POST", "/kb/draft/cards", {"card": card}, token=self.admin)[0], 200)
        self.assertEqual(self.request("POST", "/kb/draft/publish", {"label": "合成：别名"}, token=self.admin)[0], 200)
        status, shown = self.request("GET", "/kb/cards/fixture_extra?scope=active", token=self.admin)
        self.assertEqual((status, shown["card"]["source_kind"], shown["card"]["aliases"]),
                         (200, "merchant", ["合成运费表"]))

    def test_an_offline_draft_from_an_upload_is_a_merchant_rule_without_aliases(self):
        status, upload = self.request("POST", "/kb/uploads", {"title": "合成店铺说明",
            "text": "一、配件退换\n合成店铺的配件签收后七天内未使用且包装完整的，可以申请退换。"}, token=self.admin)
        self.assertEqual(status, 201, upload)
        identifier = f"upload:{upload['document']['document_id']}#s01"
        status, drafted = self.request("POST", "/kb/ai-draft", {"evidence_ids": [identifier]}, token=self.admin)
        self.assertEqual((status, drafted["card"]["source_kind"], drafted["card"]["aliases"]), (200, "merchant", []))
        status, source = self.request("GET", "/kb/sources?scope=draft", token=self.admin)
        self.assertEqual(source["documents"][0]["sections"][0]["scope_note"], UPLOAD_SCOPE_NOTE)


if __name__ == "__main__":
    unittest.main()
