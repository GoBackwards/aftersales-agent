"""Deterministic source-span sidecar for a frozen v2 policy corpus.

Repeated route-table columns become separate records. Other parents remain
complete. Units are lists of exact source spans, never fabricated contiguous
quotes. Validation establishes local consistency, not publisher authenticity.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .public_policy import (
    PublicPolicyIndex, _fields, _object, _read_file, _reject_constant, _sha256,
)


SCHEMA = "public-policy-structure-v1"
PROTOCOL = {
    "name": "repeated_route_table_exact_spans_v1",
    "header": ["发货地", "收件地", "赔付金额"],
    "column_groups": 3,
    "record_order": "source_parent_order_then_row_then_column_group",
    "duplicate_records": "preserve_every_occurrence",
    "missing_routes": "never_infer_or_fill",
    "other_parents": "whole_parent_unchanged",
    "offsets": "python_unicode_characters_end_exclusive",
    "source_truth": "local_hash_and_span_consistency_not_independent_authentication",
    "generation": "not_connected_to_answer_protocols",
}
_ROOT_FIELDS = {"schema_version", "source_corpus_schema", "source_corpus_sha256", "protocol", "counts", "units"}
_MAX_STRUCTURE_BYTES = 16 * 1024 * 1024


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("STRUCTURE_JSON_INVALID") from None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _source(corpus_path):
    path = Path(corpus_path).absolute()
    source = PublicPolicyIndex(path)
    raw = _read_file(path)
    if _sha256(raw) != source.corpus_sha256:
        raise ValueError("STRUCTURE_SOURCE_CHANGED")
    if _json(raw)["schema_version"] != "official-policy-corpus-v2":
        raise ValueError("STRUCTURE_REQUIRES_V2_CORPUS")
    return source


def _cells(line, start):
    """Return exact, whitespace-trimmed cell spans including empty cells."""
    cells, position = [], 0
    for raw in line.split("|"):
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        cells.append((raw.strip(), start + position + left, start + position + right))
        position += len(raw) + 1
    return cells


def _route_rows(content):
    """Recognize the repeated three-group shape, without page IDs or test labels.

    Ambiguous/malformed table-like content stays a complete parent. This phase
    handles tables with only a source prefix, one header, rows and blank lines;
    tables with trailing notes need a later context-preserving protocol.
    """
    lines, position = [], 0
    for raw in content.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        lines.append((line, position, _cells(line, position)))
        position += len(raw)
    headers = [number for number, (_line, _start, cells) in enumerate(lines)
               if [cell[0] for cell in cells] == PROTOCOL["header"] * PROTOCOL["column_groups"]]
    if len(headers) != 1:
        return None
    header_number = headers[0]
    _header_line, header_start, header_cells = lines[header_number]
    if any("|" in line for line, _start, _cells_value in lines[:header_number]):
        return None
    prefix_end = len(content[:header_start].rstrip())
    rows, row_number = [], 0
    for line, _line_start, cells in lines[header_number + 1:]:
        if not line.strip():
            continue
        if (not 3 <= len(cells) <= 9 or len(cells) % 3
                or any(not cell[0] for cell in cells)):
            return None
        row_number += 1
        for column in range(len(cells) // 3):
            triple = cells[column * 3:column * 3 + 3]
            # A money column must be visibly numeric; retain its exact spelling.
            # This recognizes the layout rather than inventing amounts for dashes,
            # merged cells, missing routes or notes that need another protocol.
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", triple[2][0]):
                return None
            header = header_cells[column * 3:column * 3 + 3]
            rows.append({"route": {"origin": triple[0][0], "destination": triple[1][0],
                                   "amount_text": triple[2][0]},
                         "table": {"row_index": row_number, "column_group": column + 1, "header_group_count": 3},
                         "header_span": (header[0][1], header[-1][2]),
                         "record_span": (triple[0][1], triple[-1][2])})
    return (prefix_end, rows) if rows else None


def _span(parent, role, start, end):
    text = parent["content"][start:end]
    return {"role": role, "start": start, "end": end,
            "document_start": parent["start"] + start, "document_end": parent["start"] + end,
            "text": text, "sha256": _sha256(text.encode("utf-8"))}


def _derive(source):
    units, parent_count, route_parents = [], 0, 0
    documents = source.documents
    for document in documents:
        metadata = {key: deepcopy(document.get(key)) for key in (
            "document_id", "title", "url", "fetched_at", "raw_sha256", "text_sha256",
            "coverage", "scope_note", "effective_from", "effective_to",
        )}
        for parent in document["chunks"]:
            parent_count += 1
            citation = f"{document['document_id']}@{document['text_sha256'][:12]}#{parent['chunk_id']}"
            base = {**metadata, "parent_citation_id": citation, "parent_chunk_id": parent["chunk_id"],
                    "parent_sha256": parent["sha256"]}
            parsed = _route_rows(parent["content"])
            if parsed is None:
                units.append({"unit_id": f"U{source.corpus_sha256[:12]}:{len(units) + 1:05d}",
                              "kind": "whole_parent", **base, "route": None, "table": None,
                              "spans": [_span(parent, "whole_parent", 0, len(parent["content"]))]})
                continue
            route_parents += 1
            prefix_end, rows = parsed
            for row in rows:
                spans = [_span(parent, "context", 0, prefix_end)] if prefix_end else []
                spans += [_span(parent, "table_header", *row["header_span"]),
                          _span(parent, "record", *row["record_span"])]
                units.append({"unit_id": f"U{source.corpus_sha256[:12]}:{len(units) + 1:05d}",
                              "kind": "route_record", **base, "route": row["route"], "table": row["table"],
                              "spans": spans})
    route_count = sum(unit["kind"] == "route_record" for unit in units)
    return {"schema_version": SCHEMA, "source_corpus_schema": "official-policy-corpus-v2",
            "source_corpus_sha256": source.corpus_sha256, "protocol": deepcopy(PROTOCOL),
            "counts": {"source_documents": len(documents), "source_parents": parent_count,
                       "route_parents": route_parents, "route_record_units": route_count,
                       "whole_parent_units": len(units) - route_count, "units": len(units)},
            "units": units}


class StructureIndex:
    """Read-only units rebuilt against their unchanged canonical v2 source."""

    def __init__(self, corpus_path, structure_path):
        source = _source(corpus_path)
        raw = _read_file(Path(structure_path).absolute())
        payload = _json(raw)
        _fields(payload, _ROOT_FIELDS)
        if _canonical(payload) != _canonical(_derive(source)):
            raise ValueError("STRUCTURE_SOURCE_DERIVATION_MISMATCH")
        self._source_index = source
        self._structure_sha256 = _sha256(raw)
        self._payload = deepcopy(payload)
        self._units = {unit["unit_id"]: unit for unit in payload["units"]}

    @property
    def source_index(self):
        return self._source_index

    @property
    def structure_sha256(self):
        return self._structure_sha256

    @property
    def corpus_sha256(self):
        return self._source_index.corpus_sha256

    @property
    def units(self):
        return deepcopy(self._payload["units"])

    @property
    def counts(self):
        return deepcopy(self._payload["counts"])

    def evidence_by_id(self, unit_id):
        return deepcopy(self._units.get(unit_id)) if type(unit_id) is str else None

    def validate_evidence(self, unit):
        """Reject altered/recombined units; source authenticity is not established."""
        if type(unit) is not dict or type(unit.get("unit_id")) is not str:
            raise ValueError("STRUCTURE_EVIDENCE_INVALID")
        canonical = self._units.get(unit["unit_id"])
        try:
            matched = canonical is not None and _canonical(unit) == _canonical(canonical)
        except (TypeError, ValueError):
            matched = False
        if not matched:
            raise ValueError("STRUCTURE_EVIDENCE_MISMATCH")
        return deepcopy(canonical)
