"""Uploaded policy text: stable sections, exact hashes and safe attribution links."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from support_agent.kb_sources import (MAX_SECTION_CHARS, TARGET_SECTION_CHARS, UPLOAD_COVERAGE, UPLOAD_KIND,
                                      build_uploaded_document, evidence_id, normalize_upload_text,
                                      split_sections, supplementary_evidence, validate_public_url)

DOCUMENT = """一、配件退换
合成店铺的表带、充电线等配件，签收后七天内未使用且包装完整的，可以申请退换。

二、不支持退换的情形
配件已经使用、刻字定制或包装缺失的，不支持七天无理由退换。

第三条 运费
因质量问题退换的，往返运费由店铺承担。"""


class UploadTextTests(unittest.TestCase):
    def test_headings_start_sections_and_every_section_is_verbatim_and_hashed(self):
        text = normalize_upload_text(DOCUMENT)
        sections = split_sections(text, title="合成说明")
        self.assertEqual([section["section_id"] for section in sections], ["s01", "s02", "s03"])
        self.assertEqual([section["title"] for section in sections], ["一、配件退换", "二、不支持退换的情形", "第三条 运费"])
        for section in sections:
            self.assertIn(section["text"], text)
            self.assertEqual(len(section["sha256"]), 64)

    def test_plain_paragraphs_are_grouped_to_a_bounded_size_with_numbered_titles(self):
        paragraph = "合成段落用于测试分段上限，内容没有标题。" * 12
        text = normalize_upload_text("\n\n".join([paragraph] * 6))
        sections = split_sections(text, title="无标题文档")
        self.assertGreater(len(sections), 1)
        self.assertTrue(all(section["title"].startswith("无标题文档 · 第 ") for section in sections))
        self.assertTrue(all(len(section["text"]) <= TARGET_SECTION_CHARS + len(paragraph) for section in sections))
        for long in (normalize_upload_text("很长的合成句子。" * (MAX_SECTION_CHARS // 6)),
                     normalize_upload_text("没有句末标点的超长合成文字" * 2500)):
            with self.subTest(chars=len(long)):
                pieces = split_sections(long, title="长段落")
                self.assertGreater(len(pieces), 1)
                self.assertTrue(all(len(piece["text"]) <= MAX_SECTION_CHARS for piece in pieces))
                self.assertTrue(all(piece["text"] in long for piece in pieces))
                self.assertEqual("".join(piece["text"] for piece in pieces), long)

    def test_text_is_normalized_before_hashing_and_empty_or_binary_input_is_rejected(self):
        self.assertEqual(normalize_upload_text("甲  \r\n\r\n\r\n\r\n乙\r"), "甲\n\n乙")
        for bad in ("", "   \n\n ", "含有\x00的文本", 3, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_upload_text(bad)

    def test_attribution_links_must_be_plain_public_https(self):
        self.assertEqual(validate_public_url(""), "")
        self.assertEqual(validate_public_url(" https://help.example.com/a?b=1 "), "https://help.example.com/a?b=1")
        for bad in ("http://help.example.com/", "https://user:secret@help.example.com/", "https://127.0.0.1/",
                    "https://localhost/", "https://printer.local/", "https://[::1]/", "javascript:alert(1)",
                    "https://" + "a" * 2050 + ".com", 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_public_url(bad)

    def test_document_ids_are_namespaced_and_unique(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        first = build_uploaded_document("合成说明", DOCUMENT, now=now)
        second = build_uploaded_document("合成说明", DOCUMENT, existing_ids=[first["document_id"]], now=now)
        self.assertTrue(first["document_id"].startswith("upload-"))
        self.assertNotEqual(first["document_id"], second["document_id"])
        self.assertEqual(first["kind"], UPLOAD_KIND)
        self.assertEqual(first["added_at"], "2026-09-20T00:00:00Z")
        self.assertEqual(evidence_id(first["document_id"], "s02"), f"upload:{first['document_id']}#s02")
        with self.assertRaises(ValueError):
            build_uploaded_document("  ", DOCUMENT)


class SupplementaryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.document = build_uploaded_document("合成说明", DOCUMENT, url="https://help.example.com/p")

    def test_sections_become_evidence_items_labelled_as_uploads(self):
        evidence = supplementary_evidence([self.document])
        self.assertEqual(len(evidence), 3)
        identifier = evidence_id(self.document["document_id"], "s01")
        item, source = evidence[identifier]
        self.assertEqual(item["id"], identifier)
        self.assertEqual(item["text"], self.document["sections"][0]["text"])
        self.assertEqual(item["coverage"], UPLOAD_COVERAGE)
        self.assertEqual(source["source_kind"], UPLOAD_KIND)
        self.assertEqual(source["url"], "https://help.example.com/p")
        # B5d1: production voice; the note still says nobody checked the text against the platform rules.
        self.assertIn("没有和平台公开规则逐条核对过", item["scope_note"])
        self.assertEqual(supplementary_evidence(None), {})

    def test_tampered_or_malformed_documents_are_rejected(self):
        variants = []
        edited = deepcopy(self.document)
        edited["sections"][0]["text"] += "（被改动）"
        variants.append([edited])
        variants.append([self.document, deepcopy(self.document)])
        bad_id = deepcopy(self.document)
        bad_id["document_id"] = "parent:official"
        variants.append([bad_id])
        bad_kind = deepcopy(self.document)
        bad_kind["kind"] = "official_snapshot"
        variants.append([bad_kind])
        bad_section = deepcopy(self.document)
        bad_section["sections"][1]["section_id"] = bad_section["sections"][0]["section_id"]
        variants.append([bad_section])
        bad_url = deepcopy(self.document)
        bad_url["url"] = "http://help.example.com/"
        variants.append([bad_url])
        variants.append({"not": "a list"})
        for value in variants:
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                supplementary_evidence(value)


if __name__ == "__main__":
    unittest.main()
