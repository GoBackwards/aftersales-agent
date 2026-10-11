"""Versioned card store on invented sources: drafts, checks, publishing, rollback.

Checks here prove source binding and verbatim quotes through the same loader a
chat turn uses. They say nothing about whether a rule is correct or current.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.policy import PolicyAdapter, PublicPolicyConfig, load_policy_knowledge
from support_agent.kb_store import KBValidationError, KnowledgeBaseStore, diff_catalogs, normalize_card
from support_agent.service import _knowledge_base
from tests.test_public_policy_bundles import bundle_fixture

UPLOAD = """一、配件退换
合成店铺的表带、充电线等配件，签收后七天内未使用且包装完整的，可以申请退换。

二、运费
因质量问题退换的，往返运费由合成店铺承担。"""


def synthetic_card(card_id, title, item, quote, *, domain="return_items"):
    return {"id": card_id, "effect_domain": domain, "title": title, "support_scope_ids": ["synthetic"],
            "scope": "仅限合成工程测试", "conditions": ["合成测试条件"], "outcomes": [title + "时适用合成结果"],
            "outcome_premises": [["合成测试前提"]], "exceptions": [], "evidence_ids": [item["id"]],
            "references": [{"evidence_id": item["id"], "parent_sha256": item["sha256"], "quotes": [quote]}]}


def kb_fixture(root):
    """Invented corpus plus a two-card baseline bound to it (never real policy)."""
    root = Path(root)
    config = PublicPolicyConfig(*bundle_fixture(root))
    knowledge = load_policy_knowledge(config)
    parents = {key: item for key, item in knowledge.evidence.items() if key.startswith("parent:")}
    table = next(item for key, item in parents.items() if key.endswith("table01"))
    section = next(item for key, item in parents.items() if key.endswith("section-01"))
    catalog = {"schema_version": "graph-policy-cards-v1", "human_reviewed": False,
               "knowledge_release_id": "c" * 64, "corpus_sha256": knowledge.metadata["corpus_sha256"],
               "cards": [synthetic_card("fixture_gift", "合成赠品条件", table, "注：赠品完整且符合范围时适用，其他约定除外。"),
                         synthetic_card("fixture_notice", "合成售后说明", section, "这是合成工程测试的售后说明文字",
                                        domain="general_guidance")]}
    baseline = root / "baseline-cards.json"
    baseline.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return config, knowledge, baseline, {"table": table, "section": section}


def proposal_for(card, **changes):
    value = {key: card[key] for key in ("id", "title", "scope", "effect_domain", "conditions", "outcomes",
                                         "outcome_premises", "exceptions")}
    value["references"] = [{"evidence_id": ref["evidence_id"], "quotes": list(ref["quotes"])} for ref in card["references"]]
    value.update(changes)
    return value


class KnowledgeBaseStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kb-store-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.knowledge, self.baseline, self.items = kb_fixture(self.root)
        self.store = self.open()

    def open(self):
        return _knowledge_base(self.root / "kb", self.config, self.knowledge, self.baseline)

    def adapter(self, path):
        return PolicyAdapter(self.config, knowledge=self.knowledge, card_catalog=path)

    def new_card(self, **changes):
        base = synthetic_card("fixture_extra", "合成附加规则", self.items["table"], "合成运费表")
        return proposal_for(base, **changes)

    def test_first_open_seeds_one_immutable_active_version_and_reopening_keeps_it(self):
        versions = self.store.versions()
        self.assertEqual([row["number"] for row in versions], [1])
        path = self.store.active_catalog_path()
        self.assertEqual(path.stem, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual({card["kb"]["origin"] for card in self.store.active_catalog()["cards"]}, {"baseline"})
        self.assertEqual(self.store.history()[0]["event"], "seed")
        self.assertFalse(self.store.draft_status()["dirty"])
        self.assertEqual(self.adapter(path)._metadata["policy_card_catalog"]["status"], "enabled")
        again = self.open()
        self.assertEqual(again.active_pointer()["version_id"], self.store.active_pointer()["version_id"])
        self.assertEqual(len(again.versions()), 1)

    def test_draft_edits_are_checked_against_source_text_before_they_are_saved(self):
        with self.assertRaises(KBValidationError) as caught:
            self.store.upsert_card(self.new_card(references=[{"evidence_id": self.items["table"]["id"],
                                                               "quotes": ["原文里并没有这句话"]}]), author="kb-admin")
        self.assertEqual(caught.exception.errors[0]["field"], "references[0].quotes[0]")
        with self.assertRaises(KBValidationError) as caught:
            self.store.upsert_card(self.new_card(outcome_premises=[]), author="kb-admin")
        self.assertIn("outcome_premises", [error["field"] for error in caught.exception.errors])
        with self.assertRaises(KBValidationError):
            self.store.upsert_card(self.new_card(references=[{"evidence_id": "parent:missing", "quotes": ["x"]}]),
                                   author="kb-admin")
        self.assertFalse(self.store.draft_status()["dirty"])
        self.assertFalse((self.root / "kb" / "draft.json").exists())
        card = self.store.upsert_card(self.new_card(), author="kb-admin")
        self.assertEqual(card["kb"]["origin"], "created")
        self.assertEqual(card["references"][0]["parent_sha256"], self.items["table"]["sha256"])
        status = self.store.draft_status()
        self.assertTrue(status["dirty"])
        self.assertEqual([row["id"] for row in status["diff"]["added"]], ["fixture_extra"])

    def test_ids_are_unique_and_existing_cards_are_edited_only_in_place(self):
        with self.assertRaises(KBValidationError) as caught:
            self.store.upsert_card(self.new_card(id="fixture_gift"), author="kb-admin")
        self.assertEqual(caught.exception.errors[0]["field"], "id")
        gift = next(card for card in self.store.draft()["catalog"]["cards"] if card["id"] == "fixture_gift")
        with self.assertRaises(KBValidationError):
            self.store.upsert_card(proposal_for(gift, id="renamed_gift", replaces="fixture_gift"), author="kb-admin")
        unchanged = self.store.upsert_card(proposal_for(gift, replaces="fixture_gift"), author="kb-admin")
        self.assertEqual(unchanged["kb"]["origin"], "baseline")
        self.assertFalse(self.store.draft_status()["dirty"])
        edited = self.store.upsert_card(proposal_for(gift, title="合成赠品条件（修订）", replaces="fixture_gift"),
                                        author="kb-admin")
        self.assertEqual(edited["kb"]["origin"], "edited")
        self.assertEqual(self.store.draft_status()["diff"]["changed"],
                         [{"id": "fixture_gift", "title": "合成赠品条件（修订）", "fields": ["title"]}])

    def test_check_card_is_a_dry_run(self):
        checked = self.store.check_card(self.new_card())
        self.assertEqual(checked["id"], "fixture_extra")
        self.assertFalse((self.root / "kb" / "draft.json").exists())
        with self.assertRaises(KBValidationError):
            self.store.check_card(self.new_card(title=""))

    def test_publish_creates_a_new_active_version_that_a_turn_can_load_and_rollback_restores_the_old_one(self):
        first = self.store.active_pointer()["version_id"]
        self.store.upsert_card(self.new_card(), author="kb-admin")
        with self.assertRaises(KBValidationError):
            self.store.publish(label="  ", author="kb-admin")
        result = self.store.publish(label="新增合成规则", author="kb-admin")
        second = result["version"]["version_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(result["version"]["number"], 2)
        self.assertEqual(result["version"]["parent_version_id"], first)
        self.assertFalse((self.root / "kb" / "draft.json").exists())
        path = self.store.active_catalog_path()
        self.assertEqual(path.stem, second)
        adapter = self.adapter(path)
        self.assertEqual(adapter._metadata["policy_card_catalog"]["card_count"], 3)
        # B6: a turn bound to the new version reads its whole catalogue, the new card included.
        self.assertEqual([card["id"] for card in adapter.catalogue_cards()],
                         ["fixture_gift", "fixture_notice", "fixture_extra"])
        diff = self.store.diff(second)
        self.assertEqual([row["id"] for row in diff["diff"]["added"]], ["fixture_extra"])
        with self.assertRaises(KBValidationError):
            self.store.activate(first, reason="", author="kb-admin")
        self.store.activate(first, reason="演示回滚", author="kb-admin")
        self.assertEqual(self.store.active_pointer()["version_id"], first)
        self.assertEqual([row["event"] for row in self.store.history()], ["rollback", "publish", "seed"])
        self.assertEqual(json.loads((self.root / "kb" / "versions" / f"{second}.json").read_text(encoding="utf-8"))
                         ["cards"][-1]["id"], "fixture_extra")
        with self.assertRaises(KeyError):
            self.store.activate("0" * 64, reason="不存在", author="kb-admin")

    def test_uploads_travel_inside_the_catalog_and_cannot_be_removed_while_cited(self):
        document = self.store.add_upload(title="合成店铺说明", text=UPLOAD, url="", author="kb-admin")
        self.assertEqual(len(document["sections"]), 2)
        identifier = f"upload:{document['document_id']}#s01"
        quote = "签收后七天内未使用且包装完整的，可以申请退换。"
        self.store.upsert_card(self.new_card(references=[{"evidence_id": identifier, "quotes": [quote]}]), author="kb-admin")
        with self.assertRaises(KBValidationError):
            self.store.delete_upload(document["document_id"])
        result = self.store.publish(label="上传合成文档", author="kb-admin")
        self.assertEqual(result["version"]["supplementary_source_count"], 1)
        adapter = self.adapter(self.store.active_catalog_path())
        self.assertIn(identifier, adapter._evidence)
        self.assertNotIn(identifier, self.knowledge.evidence)
        self.assertEqual(adapter._evidence[identifier]["coverage"], "merchant_uploaded_text")
        self.store.delete_card("fixture_extra")
        self.assertEqual(self.store.delete_upload(document["document_id"]), {"deleted": document["document_id"]})
        self.assertNotIn("supplementary_sources", self.store.draft()["catalog"])
        with self.assertRaises(KeyError):
            self.store.delete_upload(document["document_id"])

    def test_runtime_rejection_blocks_saving_and_the_last_card_cannot_be_deleted(self):
        def reject(_path):
            raise ValueError("GRAPH_CARD_CATALOG_INVALID")
        store = KnowledgeBaseStore(self.root / "kb", baseline_catalog=self.baseline,
                                   official_evidence=self.store.official_evidence, runtime_check=reject)
        with self.assertRaises(KBValidationError) as caught:
            store.upsert_card(self.new_card(), author="kb-admin")
        self.assertEqual(caught.exception.errors[0]["field"], "catalog")
        self.assertFalse((self.root / "kb" / "draft.json").exists())
        self.store.delete_card("fixture_notice")
        with self.assertRaises(KBValidationError):
            self.store.delete_card("fixture_gift")
        with self.assertRaises(KeyError):
            self.store.delete_card("fixture_notice")

    def test_validate_reports_tampered_draft_cards_and_discard_restores_the_active_version(self):
        self.store.upsert_card(self.new_card(), author="kb-admin")
        path = self.root / "kb" / "draft.json"
        draft = json.loads(path.read_text(encoding="utf-8"))
        draft["catalog"]["cards"][-1]["references"][0]["quotes"] = ["被手工改坏的引用"]
        path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        report = self.store.validate_draft()
        self.assertFalse(report["ok"])
        self.assertEqual(report["card_errors"][0]["card_id"], "fixture_extra")
        self.assertNotEqual(report["runtime_check"], "passed")
        with self.assertRaises(KBValidationError):
            self.store.publish(label="不应发布", author="kb-admin")
        self.assertEqual(len(self.store.versions()), 1)
        self.assertFalse(self.store.discard_draft()["dirty"])
        self.assertTrue(self.store.validate_draft()["ok"])


class CatalogHelpersTests(unittest.TestCase):
    def test_normalize_card_computes_hashes_and_reports_each_bad_field(self):
        evidence = {"parent:a": {"text": "甲乙丙丁戊己庚辛", "sha256": "a" * 64}}
        card = normalize_card({"id": "demo_card", "title": "标题", "scope": "范围", "effect_domain": "general_guidance",
                               "conditions": ["条件"], "outcomes": ["结论"], "outcome_premises": [["前提"]],
                               "references": [{"evidence_id": "parent:a", "quotes": ["乙丙丁"]}]}, evidence=evidence)
        self.assertEqual(card["references"][0]["parent_sha256"], "a" * 64)
        self.assertEqual(card["evidence_ids"], ["parent:a"])
        self.assertEqual(card["kb"]["origin"], "created")
        with self.assertRaises(KBValidationError) as caught:
            normalize_card({"id": "Bad-ID", "title": "", "scope": "范围", "effect_domain": "unknown",
                            "conditions": [], "outcomes": ["结论"], "outcome_premises": [[]],
                            "references": [{"evidence_id": "parent:a", "quotes": ["不在原文"]}]}, evidence=evidence)
        fields = {error["field"] for error in caught.exception.errors}
        self.assertTrue({"id", "title", "effect_domain", "conditions", "outcome_premises[0]",
                         "references[0].quotes[0]"} <= fields)

    def test_diff_reports_cards_fields_and_uploaded_documents(self):
        before = {"cards": [{"id": "a", "title": "甲", "scope": "旧"}, {"id": "b", "title": "乙"}]}
        after = {"cards": [{"id": "a", "title": "甲", "scope": "新"}, {"id": "c", "title": "丙"}],
                 "supplementary_sources": [{"document_id": "upload-x", "title": "上传"}]}
        diff = diff_catalogs(before, after)
        self.assertEqual(diff["added"], [{"id": "c", "title": "丙"}])
        self.assertEqual(diff["removed"], [{"id": "b", "title": "乙"}])
        self.assertEqual(diff["changed"], [{"id": "a", "title": "甲", "fields": ["scope"]}])
        self.assertEqual(diff["sources_added"], [{"id": "upload-x", "title": "上传"}])
        self.assertEqual(diff["unchanged_count"], 0)


if __name__ == "__main__":
    unittest.main()
