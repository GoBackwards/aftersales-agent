"""Build-time writers for local policy corpus artifacts.

``build_image_corpus`` turns a v1 HTML snapshot plus AI-reviewed image text into
a new self-contained v2 corpus; ``build_structure`` writes the route-table span
sidecar for a v2 corpus. Neither runs at product startup or during a turn: the
runtime modules (``public_policy_images``, ``public_policy_structure``) only
validate what these functions wrote. Outputs are always new files or
directories, outside the source tree except ``var/`` and ``reports/``.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .public_policy import (
    PublicPolicyIndex, _fields, _identifier, _local_file, _ordinary_path, _read_file, _sha256,
)
from .public_policy_images import (
    _IMAGE_FIELDS, AI_SCOPE_NOTE, CORPUS_ID, CORPUS_SCHEMA, DERIVATION_PROTOCOL, _json, derive_document,
    image_coverage, replay_html, validate_image,
)
from .public_policy_structure import _MAX_STRUCTURE_BYTES, _derive, _source


def _write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _new_output_directory(value):
    """Reject traversal and existing links before normalizing the destination."""
    candidate = Path(value).absolute()
    if ".." in candidate.parts:
        raise ValueError("IMAGE_CORPUS_OUTPUT_PARENT_TRAVERSAL_FORBIDDEN")
    for component in reversed((candidate, *candidate.parents)):
        try:
            component.lstat()
        except FileNotFoundError:
            continue
        if component == candidate:
            raise ValueError("IMAGE_CORPUS_OUTPUT_ALREADY_EXISTS")
        _ordinary_path(component, file=False)
    output = candidate.resolve()
    project_root = Path(__file__).resolve().parents[1]
    if output.is_relative_to(project_root):
        relative = output.relative_to(project_root)
        if not relative.parts or relative.parts[0].lower() not in {"var", "reports"}:
            raise ValueError("IMAGE_CORPUS_OUTPUT_IN_SOURCE_TREE_FORBIDDEN")
    return output


def build_image_corpus(corpus_path, image_packet_path, output_dir):
    """Validate inputs first, then create a new self-contained v2 snapshot."""
    output = _new_output_directory(output_dir)
    source_path, packet_path = Path(corpus_path).absolute(), Path(image_packet_path).absolute()
    index = PublicPolicyIndex(source_path)
    source_raw = _read_file(source_path)
    source = _json(source_raw)
    if source["schema_version"] != "official-policy-corpus-v1" or _sha256(source_raw) != index.corpus_sha256:
        raise ValueError("IMAGE_CORPUS_REQUIRES_UNCHANGED_V1_SOURCE")
    packet_raw = _read_file(packet_path)
    packet = _json(packet_raw)
    _fields(packet, {"schema_version", "source_corpus_sha256", "images"})
    if (packet["schema_version"] != "policy-image-packet-v1"
            or packet["source_corpus_sha256"] != index.corpus_sha256):
        raise ValueError("IMAGE_PACKET_SOURCE_BINDING_MISMATCH")
    if type(packet["images"]) is not list or len(packet["images"]) > 10000:
        raise ValueError("IMAGE_PACKET_IMAGES_INVALID")
    documents, files, source_by_id, selected = [], {}, {}, {}
    for document in index.documents:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", document["document_id"]):
            raise ValueError("IMAGE_CORPUS_DOCUMENT_ID_UNSAFE_FOR_ARTIFACT_PATH")
        raw = _read_file(_local_file(source_path.parent, document["raw_path"]))
        old_text = _read_file(_local_file(source_path.parent, document["text_path"]))
        html_text, inventory = replay_html(document, raw, source=True)
        if (_sha256(raw) != document["raw_sha256"] or _sha256(old_text) != document["text_sha256"]
                or html_text.encode("utf-8") != old_text or document["image_count"] != len(inventory)
                or document.get("omitted_images") != inventory):
            raise ValueError("IMAGE_CORPUS_SOURCE_HTML_REPLAY_MISMATCH")
        source_by_id[document["document_id"]] = (document, raw, html_text, inventory)
    for supplied in packet["images"]:
        _fields(supplied, _IMAGE_FIELDS | {"document_id"})
        document_id = _identifier(supplied["document_id"])
        if document_id not in source_by_id:
            raise ValueError("IMAGE_PACKET_UNKNOWN_DOCUMENT")
        item = {key: supplied[key] for key in _IMAGE_FIELDS}
        transcript, assets = validate_image(packet_path.parent, document_id, item, source_by_id[document_id][3])
        key = document_id, item["sequence"]
        if key in selected:
            raise ValueError("IMAGE_PACKET_DUPLICATE_IMAGE")
        # Keep a truthful raster extension so reviewers can open the copied
        # original directly; never trust the supplied filename's extension.
        raster = assets["asset"]
        extension = (".png" if raster.startswith(b"\x89PNG") else ".jpg" if raster.startswith(b"\xff\xd8")
                     else ".gif" if raster.startswith(b"GIF") else ".webp")
        for name, suffix in (("asset", extension), ("transcription", ".json"), ("review", ".json")):
            relative = f"image_evidence/{document_id}/{item['sequence']:02d}/{name}{suffix}"
            files[relative] = assets[name]
            item[name + "_path"] = relative
        selected[key] = (item, transcript)
    for document_id, (original, raw, html_text, inventory) in source_by_id.items():
        document = deepcopy(original)
        transcriptions = [selected[key] for key in sorted(selected) if key[0] == document_id]
        full_text, chunks, locations = derive_document(html_text, document_id, transcriptions)
        entries = [{**item, "blocks": location} for (item, _transcript), location in zip(transcriptions, locations)]
        sequences = {item["sequence"] for item in entries}
        document.update(raw_path=f"raw/{document_id}.html", text_path=f"text/{document_id}.txt",
                        extraction_protocol=DERIVATION_PROTOCOL, html_text_sha256=_sha256(html_text.encode("utf-8")),
                        text_sha256=_sha256(full_text.encode("utf-8")), chunks=chunks,
                        image_transcriptions=entries, omitted_images=[item for item in inventory if item["sequence"] not in sequences],
                        coverage=image_coverage(len(inventory), len(entries)))
        if entries:
            document["scope_note"] = (document.get("scope_note", "").rstrip() + " " + AI_SCOPE_NOTE).strip()
        files[document["raw_path"]], files[document["text_path"]] = raw, full_text.encode("utf-8")
        documents.append(document)
    derived = {**source, "schema_version": CORPUS_SCHEMA, "corpus_id": CORPUS_ID, "documents": documents,
               "derivation": {"protocol": DERIVATION_PROTOCOL, "source_corpus_sha256": index.corpus_sha256,
                              "image_packet_sha256": _sha256(packet_raw), "human_review": False}}
    # Recheck after input validation in case an existing ancestor changed.
    output = _new_output_directory(output)
    output.mkdir(parents=True, exist_ok=False)
    for relative, data in files.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(data)
    _write_json(output / "corpus.json", derived)
    validated = PublicPolicyIndex(output / "corpus.json")
    audit = {"schema_version": "policy-image-corpus-audit-v1", "status": "validated",
             "corpus_sha256": validated.corpus_sha256, "source_corpus_sha256": index.corpus_sha256,
             "image_packet_sha256": _sha256(packet_raw), "html_replay": "matched",
             "derived_text_replay": "matched", "document_count": len(documents),
             "parent_chunk_count": sum(len(document["chunks"]) for document in documents),
             "image_count": sum(document["image_count"] for document in documents),
             "ai_reviewed_image_count": len(selected), "human_reviewed_image_count": 0,
             "image_coverage": "selected_blocks_only_not_full_image_coverage",
             "omitted_image_count": sum(len(document["omitted_images"]) for document in documents),
             "model_api_calls": 0, "effective_dates": "unverified", "authenticity": "not_independently_verified",
             "derivation_binding": "build_time_digests_not_revalidated_source_chain",
             "limitations": ["AI visual transcription and independent AI review are not human acceptance.",
                             "Hashes establish local consistency, not independent source authenticity.",
                             "Root source-corpus and image-packet hashes record build inputs; loading v2 does not revalidate those original input manifests.",
                             "Raster dimensions are checked from headers; semantic fidelity needs human review."],
             "documents": [{"document_id": document["document_id"], "raw_sha256": document["raw_sha256"],
                            "html_text_sha256": document["html_text_sha256"], "text_sha256": document["text_sha256"],
                            "coverage": document["coverage"], "image_count": document["image_count"],
                            "ai_reviewed_image_count": len(document["image_transcriptions"]),
                            "omitted_image_count": len(document["omitted_images"])} for document in documents]}
    _write_json(output / "image-corpus-audit.json", audit)
    return audit


def _output_path(value):
    candidate = Path(value).absolute()
    if ".." in candidate.parts:
        raise ValueError("STRUCTURE_OUTPUT_PARENT_TRAVERSAL_FORBIDDEN")
    for component in reversed((candidate, *candidate.parents)):
        try:
            component.lstat()
        except FileNotFoundError:
            continue
        if component == candidate:
            raise ValueError("STRUCTURE_OUTPUT_ALREADY_EXISTS")
        _ordinary_path(component, file=False)
    output = candidate.resolve()
    project = Path(__file__).resolve().parents[1]
    if output.is_relative_to(project):
        relative = output.relative_to(project)
        if not relative.parts or relative.parts[0].lower() not in {"var", "reports"}:
            raise ValueError("STRUCTURE_OUTPUT_MUST_USE_VAR_OR_REPORTS")
    return output


def build_structure(corpus_path, output_path):
    """Create a new deterministic JSON sidecar without changing the source."""
    output = _output_path(output_path)
    source = _source(corpus_path)
    payload = _derive(source)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > _MAX_STRUCTURE_BYTES:
        raise ValueError("STRUCTURE_OUTPUT_TOO_LARGE")
    _output_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _output_path(output)
    with output.open("xb") as handle:
        handle.write(raw)
    return {"schema_version": "public-policy-structure-build-v1", "status": "completed",
            "structure_sha256": _sha256(raw), "corpus_sha256": source.corpus_sha256,
            "counts": deepcopy(payload["counts"]), "network_requests": 0, "model_api_calls": 0,
            "validation_scope": "deterministic_source_span_derivation_not_semantic_or_external_authentication"}
