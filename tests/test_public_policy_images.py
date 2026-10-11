"""Invented image/text fixtures verify provenance; no real-policy quality scores."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from support_agent.public_policy import PUBLIC_SCOPE, PublicPolicyIndex
from support_agent.public_policy_collection import EXTRACTION_PROTOCOL, extract_policy_text, make_chunks
from support_agent.knowledge_build import build_image_corpus
from support_agent.public_policy_images import AI_SCOPE_NOTE
from support_agent import knowledge_build as build_module


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sha(path.read_bytes())


def synthetic_png(width=100, height=80):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\xff\xff" * width) * height))
            + chunk(b"IEND", b""))


def image_fixture(root, image_count=2):
    source, packet_dir = root / "source", root / "packet"
    source.mkdir()
    packet_dir.mkdir()
    title = "合成图片工程测试"
    html = ('<html><div id="pdfContainer"><div class="contxt">'
            f'<h1 class="help-tit1">{title}</h1>'
            '<p>这是合成工程测试的售后说明文字，用于验证图文追溯而不是真实业务政策。</p>'
            + ''.join(f'<img src="//img.example.invalid/policy-{number}.png" alt="测试表{number}">'
                      for number in range(1, image_count + 1)) + '</div></div></html>')
    text, inventory = extract_policy_text(html, title)
    raw = html.encode("utf-8")
    (source / "source.html").write_bytes(raw)
    (source / "source.txt").write_bytes(text.encode("utf-8"))
    document = {"document_id": "fixture-policy", "title": title,
                "url": "https://help.jd.com/user/issue/327-988.html", "fetched_at": "2026-09-19T01:00:00Z",
                "http_status": 200, "raw_path": "source.html", "text_path": "source.txt", "raw_sha256": sha(raw),
                "text_sha256": sha(text.encode("utf-8")), "effective_from": None, "effective_to": None,
                "coverage": "text_only_images_omitted" if image_count else "text_only", "image_count": image_count,
                "omitted_images": inventory, "content_type": "text/html", "encoding": "utf-8",
                "extraction_protocol": EXTRACTION_PROTOCOL, "scope_note": "合成工程测试。",
                "chunks": make_chunks(text, "fixture-policy")}
    corpus = {"schema_version": "official-policy-corpus-v1", "corpus_id": "jd-self-operated-after-sales-cn-v1",
              "synthetic": False, "source_kind": "official_public_web", "scope": deepcopy(PUBLIC_SCOPE),
              "captured_at": "2026-09-19T01:01:00Z", "documents": [document]}
    corpus_path = source / "corpus.json"
    corpus_hash = save(corpus_path, corpus)
    png = synthetic_png()
    (packet_dir / "original.png").write_bytes(png)
    transcript = {"schema_version": "policy-image-transcription-v1", "document_id": "fixture-policy",
                  "sequence": 1, "source_url": inventory[0]["source_url"] if inventory else "",
                  "asset_sha256": sha(png), "width": 100, "height": 80,
                  "transcriber_kind": "ai", "transcriber_id": "synthetic-transcriber",
                  "blocks": [{"block_id": "table01", "kind": "table", "bbox": [0, 0, 100, 80],
                              "text": "合成运费表：品类 | 普通权益 | 特殊权益\n测试品类 | 用户承担 | 满足条件免费\n注：赠品完整且符合范围时适用，其他约定除外。"}]}
    transcript_hash = save(packet_dir / "transcription.json", transcript)
    review = {"schema_version": "policy-image-review-v1", "document_id": "fixture-policy", "sequence": 1,
              "asset_sha256": sha(png), "transcription_sha256": transcript_hash, "reviewer_kind": "ai",
              "reviewer_id": "synthetic-independent-reviewer", "method": "independent_visual_check",
              "status": "approved", "human_review": False, "reviewed_blocks": ["table01"],
              "notes": ["合成工程测试，审核记录也是测试替身，不声称白色图片含实际条款。"]}
    review_hash = save(packet_dir / "review.json", review)
    image = {"document_id": "fixture-policy", "sequence": 1, "source_url": transcript["source_url"],
             "asset_path": "original.png", "asset_sha256": sha(png), "width": 100, "height": 80,
             "transcription_path": "transcription.json", "transcription_sha256": transcript_hash,
             "review_path": "review.json", "review_sha256": review_hash}
    packet = {"schema_version": "policy-image-packet-v1", "source_corpus_sha256": corpus_hash,
              "images": [image] if image_count else []}
    packet_path = packet_dir / "packet.json"
    save(packet_path, packet)
    return corpus_path, packet_path, corpus, packet, transcript, review


class PublicPolicyImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus, self.packet_path, self.source, self.packet, self.transcript, self.review = image_fixture(self.root)
        self.output = self.root / "derived"

    def build(self):
        return build_image_corpus(self.corpus, self.packet_path, self.output)

    def update_review(self):
        self.packet["images"][0]["review_sha256"] = save(self.packet_path.parent / "review.json", self.review)
        save(self.packet_path, self.packet)

    def update_transcript(self):
        digest = save(self.packet_path.parent / "transcription.json", self.transcript)
        self.packet["images"][0]["transcription_sha256"] = digest
        self.review["transcription_sha256"] = digest
        self.update_review()

    def manifest(self):
        return json.loads((self.output / "corpus.json").read_text(encoding="utf-8"))

    def assert_load_rejected(self, payload):
        save(self.output / "corpus.json", payload)
        with self.assertRaises(ValueError):
            PublicPolicyIndex(self.output / "corpus.json")

    def test_build_preserves_v1_bytes_and_retrieves_complete_table_with_conditions(self):
        original_files = {path: path.read_bytes() for path in self.corpus.parent.iterdir()}
        original_search = PublicPolicyIndex(self.corpus).search("合成工程测试")
        report = self.build()
        self.assertEqual(report["image_count"], 2)
        self.assertEqual(report["ai_reviewed_image_count"], 1)
        self.assertEqual(report["human_reviewed_image_count"], 0)
        self.assertEqual(report["omitted_image_count"], 1)
        self.assertEqual(report["model_api_calls"], 0)
        self.assertEqual(report["image_coverage"], "selected_blocks_only_not_full_image_coverage")
        self.assertEqual(original_files, {path: path.read_bytes() for path in self.corpus.parent.iterdir()})
        self.assertEqual(original_search, PublicPolicyIndex(self.corpus).search("合成工程测试"))
        index = PublicPolicyIndex(self.output / "corpus.json")
        result = index.search("普通权益赠品", max_context_chars=10000)
        evidence = result["evidence"][0]
        self.assertEqual(evidence["content"], self.transcript["blocks"][0]["text"])
        self.assertEqual(evidence["url"], self.source["documents"][0]["url"])
        self.assertEqual(evidence["coverage"], "text_with_ai_reviewed_image_blocks_images_omitted")
        self.assertTrue(evidence["scope_note"].endswith(AI_SCOPE_NOTE))
        self.assertEqual(index.evidence_by_id(evidence["citation_id"])["content"], evidence["content"])
        self.assertIn("source_images_not_indexed_or_verified", result["warnings"])
        self.assertIn("image_transcriptions_ai_reviewed_not_human", result["warnings"])
        self.assertEqual(index.documents[0]["omitted_images"], [self.source["documents"][0]["omitted_images"][1]])

    def test_transcribing_every_image_does_not_claim_human_or_full_image_coverage(self):
        # Second image gets its own bound transcription and independent review.
        second = deepcopy(self.packet["images"][0])
        transcript, review = deepcopy(self.transcript), deepcopy(self.review)
        second["sequence"] = transcript["sequence"] = review["sequence"] = 2
        second["source_url"] = transcript["source_url"] = self.source["documents"][0]["omitted_images"][1]["source_url"]
        second["transcription_path"], second["review_path"] = "second-transcription.json", "second-review.json"
        second["transcription_sha256"] = review["transcription_sha256"] = save(self.packet_path.parent / second["transcription_path"], transcript)
        second["review_sha256"] = save(self.packet_path.parent / second["review_path"], review)
        self.packet["images"].append(second)
        save(self.packet_path, self.packet)
        self.build()
        index = PublicPolicyIndex(self.output / "corpus.json")
        self.assertEqual(index.documents[0]["omitted_images"], [])
        self.assertEqual(index.documents[0]["coverage"], "text_with_ai_reviewed_image_blocks")
        result = index.search("普通权益", max_context_chars=10000)
        self.assertNotIn("source_images_not_indexed_or_verified", result["warnings"])
        self.assertIn("image_transcriptions_ai_reviewed_not_human", result["warnings"])

    def test_empty_packet_retains_omitted_images_and_never_injects_unreviewed_text(self):
        self.packet["images"] = []
        save(self.packet_path, self.packet)
        self.build()
        index = PublicPolicyIndex(self.output / "corpus.json")
        self.assertEqual(index.documents[0]["coverage"], "text_only_images_omitted")
        self.assertEqual(index.documents[0]["image_transcriptions"], [])
        self.assertEqual((self.output / index.documents[0]["text_path"]).read_bytes(), (self.corpus.parent / "source.txt").read_bytes())
        self.assertNotIn("image_transcriptions_ai_reviewed_not_human", index.search("合成")["warnings"])

    def test_image_source_must_be_bound_to_exact_html_inventory(self):
        self.packet["images"][0]["source_url"] = "//img.example.invalid/unrelated.png"
        save(self.packet_path, self.packet)
        with self.assertRaisesRegex(ValueError, "HTML_SOURCE_MISMATCH"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_changed_source_corpus_is_not_silently_accepted(self):
        self.packet["source_corpus_sha256"] = "0" * 64
        save(self.packet_path, self.packet)
        with self.assertRaisesRegex(ValueError, "SOURCE_BINDING_MISMATCH"):
            self.build()

    def test_independent_approved_review_bound_to_current_transcription_required(self):
        original = deepcopy(self.review)
        for key, value in (("status", "pending"), ("human_review", True), ("reviewer_kind", "human"),
                           ("reviewer_id", self.transcript["transcriber_id"]),
                           ("transcription_sha256", "0" * 64), ("reviewed_blocks", []),
                           ("asset_sha256", "0" * 64), ("sequence", 2)):
            with self.subTest(field=key):
                self.review = {**original, key: value}
                self.update_review()
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(self.output.exists())

    def test_review_hash_tampering_is_rejected(self):
        self.review["notes"].append("unbound mutation")
        save(self.packet_path.parent / "review.json", self.review)
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            self.build()

    def test_transcription_hash_tampering_is_rejected(self):
        self.transcript["blocks"][0]["text"] += "篡改条件。"
        save(self.packet_path.parent / "transcription.json", self.transcript)
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            self.build()

    def test_transcription_bounds_cannot_exceed_original_image(self):
        self.transcript["blocks"][0]["bbox"] = [0, 0, 101, 80]
        self.update_transcript()
        with self.assertRaisesRegex(ValueError, "BBOX_INVALID"):
            self.build()

    def test_declared_image_size_must_match_original_file_header(self):
        self.packet["images"][0]["width"] = 101
        save(self.packet_path, self.packet)
        with self.assertRaisesRegex(ValueError, "DIMENSIONS_MISMATCH"):
            self.build()

    def test_duplicate_image_packet_entry_is_rejected(self):
        self.packet["images"].append(deepcopy(self.packet["images"][0]))
        save(self.packet_path, self.packet)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_IMAGE"):
            self.build()

    def test_packet_paths_cannot_escape_root(self):
        self.packet["images"][0]["asset_path"] = "../packet/original.png"
        save(self.packet_path, self.packet)
        with self.assertRaises(ValueError):
            self.build()

    def test_windows_reparse_points_are_rejected_before_artifact_read(self):
        real_lstat = Path.lstat
        target = self.packet_path.parent / "original.png"
        def lstat(path):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400)
            return real_lstat(path)
        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(ValueError, "重解析点"):
                self.build()

    def test_v2_loader_rechecks_image_and_review_hashes(self):
        self.build()
        entry = self.manifest()["documents"][0]["image_transcriptions"][0]
        for field in ("asset_path", "transcription_path", "review_path"):
            with self.subTest(field=field):
                target = self.output / entry[field]
                original = target.read_bytes()
                target.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    PublicPolicyIndex(self.output / "corpus.json")
                target.write_bytes(original)

    def test_v2_loader_rejects_forged_text_even_when_text_and_chunk_hashes_match(self):
        self.build()
        payload = self.manifest()
        document = payload["documents"][0]
        target = self.output / document["text_path"]
        full_text = target.read_text(encoding="utf-8").replace("其他约定除外", "其他约定无效")
        target.write_bytes(full_text.encode("utf-8"))
        document["text_sha256"] = sha(target.read_bytes())
        for chunk in document["chunks"]:
            chunk["content"] = full_text[chunk["start"]:chunk["end"]]
            chunk["sha256"] = sha(chunk["content"].encode("utf-8"))
        self.assert_load_rejected(payload)

    def test_v2_loader_reconstructs_transcription_instead_of_trusting_manifest(self):
        self.build()
        payload = self.manifest()
        entry = payload["documents"][0]["image_transcriptions"][0]
        transcription_path, review_path = self.output / entry["transcription_path"], self.output / entry["review_path"]
        transcript = json.loads(transcription_path.read_text(encoding="utf-8"))
        transcript["blocks"][0]["text"] += "新增未绑定主张。"
        entry["transcription_sha256"] = save(transcription_path, transcript)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["transcription_sha256"] = entry["transcription_sha256"]
        entry["review_sha256"] = save(review_path, review)
        self.assert_load_rejected(payload)

    def test_v2_loader_rejects_false_coverage_and_missing_ai_disclosure(self):
        self.build()
        original = self.manifest()
        for field, value in (("omitted_images", []), ("coverage", "text_with_ai_reviewed_image_blocks"),
                             ("scope_note", "全部人工核验"), ("html_text_sha256", "0" * 64)):
            with self.subTest(field=field):
                payload = deepcopy(original)
                payload["documents"][0][field] = value
                self.assert_load_rejected(payload)

    def test_v2_loader_rejects_block_location_or_citation_metadata_mutation(self):
        self.build()
        payload = self.manifest()
        payload["documents"][0]["image_transcriptions"][0]["blocks"][0]["bbox"] = [1, 1, 99, 79]
        self.assert_load_rejected(payload)

    def test_no_overwrite_and_no_mutation_of_existing_output(self):
        self.build()
        original = (self.output / "corpus.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "OUTPUT_ALREADY_EXISTS"):
            self.build()
        self.assertEqual((self.output / "corpus.json").read_bytes(), original)

    def test_output_dotdot_cannot_bypass_source_tree_or_escape_project(self):
        project = self.root / "project"
        (project / "var").mkdir(parents=True)
        (project / "knowledge").mkdir()
        fake_module = project / "support_agent" / "knowledge_build.py"
        attempts = (project / "var" / ".." / "knowledge" / "new-snapshot",
                    project / "var" / ".." / ".." / "escaped-snapshot",
                    project / "var" / "unused" / ".." / "new-snapshot")
        with patch.object(build_module, "__file__", str(fake_module)):
            for output in attempts:
                with self.subTest(output=str(output)), patch.object(build_module, "PublicPolicyIndex", side_effect=AssertionError("must reject before reading inputs")):
                    with self.assertRaisesRegex(ValueError, "PARENT_TRAVERSAL_FORBIDDEN"):
                        build_image_corpus(self.corpus, self.packet_path, output)
        self.assertEqual(list((project / "var").iterdir()), [])
        self.assertEqual(list((project / "knowledge").iterdir()), [])
        self.assertFalse((self.root / "escaped-snapshot").exists())

    def test_normalized_output_allows_var_but_rejects_other_project_directories(self):
        project = self.root / "project"
        project.mkdir()
        fake_module = project / "support_agent" / "knowledge_build.py"
        with patch.object(build_module, "__file__", str(fake_module)):
            with self.assertRaisesRegex(ValueError, "IN_SOURCE_TREE_FORBIDDEN"):
                build_image_corpus(self.corpus, self.packet_path, project / "knowledge" / "snapshot")
            output = project / "var" / "snapshot"
            build_image_corpus(self.corpus, self.packet_path, output)
        self.assertTrue((output / "corpus.json").is_file())
        self.assertFalse((project / "knowledge").exists())

    def test_output_reparse_ancestor_is_rejected_before_normalization(self):
        parent = self.root / "output-parent"
        parent.mkdir()
        real_lstat = Path.lstat
        def lstat(path):
            info = real_lstat(path)
            if path == parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(ValueError, "重解析点"):
                build_image_corpus(self.corpus, self.packet_path, parent / "snapshot")
        self.assertEqual(list(parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
