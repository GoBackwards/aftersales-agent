"""Synthetic exact-span tests; invented tables do not measure policy quality."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support_agent.public_policy_collection import extract_policy_text, make_chunks
from support_agent import knowledge_build
from support_agent.knowledge_build import build_image_corpus
from support_agent import public_policy_structure as structure
from tests.test_public_policy_images import image_fixture, save, sha


def structure_fixture(root, *, malformed=False, footer=False, route_rows=None):
    """Return (v2_corpus_path, v1_corpus_path, image_transcription_dict).

    route_rows can supply any 3/6/9-cell synthetic table rows; defaults preserve
    a duplicate and an absent reverse direction. No real policy is asserted.
    """
    corpus, packet_path, payload, packet, transcript, _review = image_fixture(root, image_count=1)
    header = ["发货地", "收件地", "赔付金额"] * 3
    rows = [["甲", "乙", "09.00", "丙", "丁", "12", "戊", "己", "7"],
            ["甲", "乙", "09.00", "庚", "辛", "15"], ["壬", "癸", "0"]]
    if route_rows is not None:
        rows = deepcopy(route_rows)
    if malformed:
        rows[1].pop()
    table = "<table><tr>" + "".join(f"<th>{cell}</th>" for cell in header) + "</tr>"
    table += "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    table += "</table>" + ("<p>注：这项额外条件不能在拆表时丢失。</p>" if footer else "")
    document = payload["documents"][0]
    raw_path = corpus.parent / document["raw_path"]
    raw = raw_path.read_text(encoding="utf-8").replace("</div></div></html>", table + "</div></div></html>")
    text, inventory = extract_policy_text(raw, document["title"])
    raw_path.write_bytes(raw.encode("utf-8"))
    text_path = corpus.parent / document["text_path"]
    text_path.write_bytes(text.encode("utf-8"))
    document.update(raw_sha256=sha(raw_path.read_bytes()), text_sha256=sha(text_path.read_bytes()),
                    chunks=make_chunks(text, document["document_id"]), omitted_images=inventory)
    packet["source_corpus_sha256"] = save(corpus, payload)
    save(packet_path, packet)
    output = root / "image-corpus"
    build_image_corpus(corpus, packet_path, output)
    return output / "corpus.json", corpus, transcript


class PolicyStructureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus, self.v1, self.transcription = structure_fixture(self.root)
        self.path = self.root / "sidecar.json"

    def build(self):
        return knowledge_build.build_structure(self.corpus, self.path)

    def index(self):
        self.build()
        return structure.StructureIndex(self.corpus, self.path)

    def test_three_column_groups_preserve_source_order_duplicate_routes_and_raw_money(self):
        index = self.index()
        self.assertEqual(index.counts, {"source_documents": 1, "source_parents": 2, "route_parents": 1,
                                       "route_record_units": 6, "whole_parent_units": 1, "units": 7})
        routes = [unit for unit in index.units if unit["kind"] == "route_record"]
        self.assertEqual([(u["route"]["origin"], u["route"]["destination"], u["route"]["amount_text"]) for u in routes],
                         [("甲", "乙", "09.00"), ("丙", "丁", "12"), ("戊", "己", "7"),
                          ("甲", "乙", "09.00"), ("庚", "辛", "15"), ("壬", "癸", "0")])
        self.assertEqual([u["table"]["column_group"] for u in routes], [1, 2, 3, 1, 2, 1])
        self.assertEqual([u["table"]["row_index"] for u in routes], [1, 1, 1, 2, 2, 3])
        self.assertNotEqual(routes[0]["unit_id"], routes[3]["unit_id"])
        self.assertNotEqual(routes[0]["spans"][-1]["start"], routes[3]["spans"][-1]["start"])
        self.assertFalse(any(u["route"]["origin"] == "乙" for u in routes))

    def test_every_span_maps_to_parent_and_document_without_fabricating_a_contiguous_quote(self):
        index = self.index()
        documents = {doc["document_id"]: doc for doc in index.source_index.documents}
        for unit in index.units:
            with self.subTest(unit=unit["unit_id"]):
                document = documents[unit["document_id"]]
                parent = index.source_index.evidence_by_id(unit["parent_citation_id"])
                full_text = (self.corpus.parent / document["text_path"]).read_text(encoding="utf-8")
                self.assertNotIn("content", unit)
                self.assertEqual(unit["raw_sha256"], document["raw_sha256"])
                self.assertEqual(unit["text_sha256"], document["text_sha256"])
                self.assertEqual(unit["parent_sha256"], parent["sha256"])
                for span in unit["spans"]:
                    self.assertEqual(span["text"], parent["content"][span["start"]:span["end"]])
                    self.assertEqual(span["text"], full_text[span["document_start"]:span["document_end"]])
                    self.assertEqual(span["sha256"], sha(span["text"].encode("utf-8")))
                self.assertEqual(index.validate_evidence(unit), unit)
                if unit["kind"] == "route_record":
                    header = next(span for span in unit["spans"] if span["role"] == "table_header")
                    self.assertEqual(header["text"], "发货地 | 收件地 | 赔付金额")
                    self.assertLess(sum(len(span["text"]) for span in unit["spans"]), len(parent["content"]))
        route_units = [u for u in index.units if u["kind"] == "route_record"]
        self.assertEqual(len({u["spans"][1]["start"] for u in route_units[:3]}), 3)

    def test_image_parent_remains_whole_with_common_conditions_and_ai_disclosure(self):
        index = self.index()
        unit = next(u for u in index.units if u["kind"] == "whole_parent")
        self.assertEqual(len(unit["spans"]), 1)
        self.assertEqual(unit["spans"][0]["text"], self.transcription["blocks"][0]["text"])
        self.assertIn("其他约定除外", unit["spans"][0]["text"])
        self.assertIn("尚未人工审核", unit["scope_note"])
        self.assertIsNone(unit["route"])
        self.assertIsNone(unit["table"])

    def test_build_is_byte_deterministic_and_never_changes_source_artifacts(self):
        originals = {path: path.read_bytes() for path in self.corpus.parent.rglob("*") if path.is_file()}
        first = self.build()
        second_path = self.root / "second.json"
        second = knowledge_build.build_structure(self.corpus, second_path)
        self.assertEqual(self.path.read_bytes(), second_path.read_bytes())
        self.assertEqual(first["structure_sha256"], second["structure_sha256"])
        self.assertEqual(first["network_requests"], 0)
        self.assertEqual(first["model_api_calls"], 0)
        self.assertEqual(originals, {path: path.read_bytes() for path in self.corpus.parent.rglob("*") if path.is_file()})

    def test_loader_rederives_typed_fields_spans_counts_and_occurrences(self):
        self.build()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for kind in ("route", "amount", "text", "offset", "boolean_offset", "parent", "raw", "header",
                     "dropped_duplicate", "reordered", "counts", "corpus", "protocol"):
            with self.subTest(kind=kind):
                payload = deepcopy(original)
                first = payload["units"][0]
                if kind == "route":
                    first["route"]["origin"] = "伪造地区"
                elif kind == "amount":
                    first["route"]["amount_text"] = "9"
                elif kind == "text":
                    first["spans"][-1]["text"] = "甲 | 乙 | 1"
                    first["spans"][-1]["sha256"] = sha(first["spans"][-1]["text"].encode("utf-8"))
                elif kind == "offset":
                    first["spans"][-1]["document_start"] += 1
                elif kind == "boolean_offset":
                    first["spans"][0]["start"] = False
                elif kind == "parent":
                    first["parent_sha256"] = "0" * 64
                elif kind == "raw":
                    first["raw_sha256"] = "0" * 64
                elif kind == "header":
                    first["spans"][1] = deepcopy(payload["units"][1]["spans"][1])
                elif kind == "dropped_duplicate":
                    del payload["units"][3]
                elif kind == "reordered":
                    payload["units"].reverse()
                elif kind == "counts":
                    payload["counts"]["route_record_units"] -= 1
                elif kind == "corpus":
                    payload["source_corpus_sha256"] = "0" * 64
                else:
                    payload["protocol"]["missing_routes"] = "fill_by_symmetry"
                save(self.path, payload)
                with self.assertRaisesRegex(ValueError, "DERIVATION_MISMATCH"):
                    structure.StructureIndex(self.corpus, self.path)

    def test_runtime_validator_rejects_changed_source_unit_and_returned_data_is_immutable(self):
        index = self.index()
        unit = index.units[0]
        canonical = deepcopy(unit)
        unit["spans"][-1]["text"] = "替换行"
        with self.assertRaisesRegex(ValueError, "EVIDENCE_MISMATCH"):
            index.validate_evidence(unit)
        self.assertEqual(index.evidence_by_id(canonical["unit_id"]), canonical)
        fetched = index.evidence_by_id(canonical["unit_id"])
        fetched["route"]["amount_text"] = "999"
        self.assertEqual(index.evidence_by_id(canonical["unit_id"]), canonical)
        self.assertIsNone(index.evidence_by_id("unknown"))
        self.assertIsNone(index.evidence_by_id({}))

    def test_ambiguous_malformed_or_noted_table_retains_whole_parent(self):
        for option in ("malformed", "footer"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                corpus, *_rest = structure_fixture(root, **{option: True})
                path = root / "structure.json"
                result = knowledge_build.build_structure(corpus, path)
                self.assertEqual(result["counts"]["route_record_units"], 0)
                units = structure.StructureIndex(corpus, path).units
                self.assertTrue(all(unit["kind"] == "whole_parent" for unit in units))
                if option == "footer":
                    self.assertIn("额外条件不能", units[0]["spans"][0]["text"])

    def test_unrelated_numeric_or_single_group_table_is_not_targeted(self):
        for header in ("类别 | 区域 | 数值", "发货地 | 收件地 | 赔付金额"):
            self.assertIsNone(structure._route_rows(header + "\n甲 | 乙 | 10"))

    def test_v1_source_and_tampered_v2_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "REQUIRES_V2"):
            knowledge_build.build_structure(self.v1, self.path)
        self.build()
        payload = json.loads(self.corpus.read_text(encoding="utf-8"))
        text = self.corpus.parent / payload["documents"][0]["text_path"]
        text.write_bytes(text.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            structure.StructureIndex(self.corpus, self.path)

    def test_output_never_overwrites_existing_file(self):
        self.path.write_bytes(b"existing evidence")
        with self.assertRaisesRegex(ValueError, "ALREADY_EXISTS"):
            self.build()
        self.assertEqual(self.path.read_bytes(), b"existing evidence")

    def test_dotdot_and_project_source_tree_outputs_are_rejected_before_loading(self):
        project = self.root / "project"
        (project / "var").mkdir(parents=True)
        fake_module = project / "support_agent" / "knowledge_build.py"
        with patch.object(knowledge_build, "__file__", str(fake_module)), patch.object(knowledge_build, "_source", side_effect=AssertionError("do not load")):
            for target in (project / "var" / ".." / "knowledge" / "sidecar.json",
                           project / "var" / ".." / ".." / "escaped.json",
                           project / "knowledge" / "sidecar.json"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    knowledge_build.build_structure(self.corpus, target)
        self.assertEqual(list((project / "var").iterdir()), [])

    def test_output_and_input_reparse_points_are_rejected(self):
        self.build()
        original_lstat = Path.lstat
        for target, action in ((self.path, lambda: structure.StructureIndex(self.corpus, self.path)),
                               (self.root, lambda: knowledge_build.build_structure(self.corpus, self.root / "nested" / "new.json"))):
            def lstat(path):
                info = original_lstat(path)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(target=target), patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ValueError, "重解析点"):
                action()

    def test_json_duplicate_keys_and_extra_fields_rejected(self):
        self.build()
        original = self.path.read_text(encoding="utf-8")
        self.path.write_text(original.replace('"schema_version":', '"schema_version":"duplicate","schema_version":', 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            structure.StructureIndex(self.corpus, self.path)
        payload = json.loads(original)
        payload["summary"] = "not authorized policy text"
        save(self.path, payload)
        with self.assertRaises(ValueError):
            structure.StructureIndex(self.corpus, self.path)


if __name__ == "__main__":
    unittest.main()
