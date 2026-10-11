"""Replayable, offline derivation of HTML snapshots plus AI-reviewed image text.

Approval records describe an AI workflow, never human acceptance or source
authentication. No OCR, image downloads, network requests, or paid APIs run here.
"""
from __future__ import annotations

import json
import struct

from .public_policy import (
    _digest, _fields, _identifier, _local_file, _object, _read_file, _reject_constant, _sha256, _string,
)
from .public_policy_collection import EXTRACTION_PROTOCOL, extract_policy_text, make_chunks


CORPUS_SCHEMA = "official-policy-corpus-v2"
CORPUS_ID = "jd-self-operated-after-sales-cn-v2"
DERIVATION_PROTOCOL = "html-with-ai-reviewed-images-v1"
AI_SCOPE_NOTE = "选定图片区域由 AI 目视转录并经独立 AI 复核，尚未人工审核；不保证图片内容全覆盖、解释正确或政策当前有效。"
_IMAGE_FIELDS = {"sequence", "source_url", "asset_path", "asset_sha256", "width", "height",
                 "transcription_path", "transcription_sha256", "review_path", "review_sha256"}
_TRANSCRIPTION_FIELDS = {"schema_version", "document_id", "sequence", "source_url", "asset_sha256",
                         "width", "height", "transcriber_kind", "transcriber_id", "blocks"}
_REVIEW_FIELDS = {"schema_version", "document_id", "sequence", "asset_sha256", "transcription_sha256",
                  "reviewer_kind", "reviewer_id", "method", "status", "human_review", "reviewed_blocks", "notes"}


def _json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("IMAGE_ARTIFACT_JSON_INVALID") from None


def image_dimensions(raw):
    """Read raster dimensions without adding an image-decoding dependency.

    This is a bounded header check, not a guarantee that every pixel decodes.
    Original bytes and dimensions remain available for visual review.
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33 and raw[12:16] == b"IHDR":
        size = struct.unpack(">II", raw[16:24])
    elif raw[:6] in {b"GIF87a", b"GIF89a"} and len(raw) >= 13:
        size = struct.unpack("<HH", raw[6:10])
    elif raw.startswith(b"\xff\xd8"):
        offset, size = 2, None
        while offset < len(raw):
            if raw[offset] != 255:
                break
            while offset < len(raw) and raw[offset] == 255:
                offset += 1
            if offset >= len(raw):
                break
            marker, offset = raw[offset], offset + 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(raw) or marker == 0xDA:
                break
            length = int.from_bytes(raw[offset:offset + 2], "big")
            if length < 2 or offset + length > len(raw):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
                height, width = struct.unpack(">HH", raw[offset + 3:offset + 7])
                size = width, height
                break
            offset += length
        if size is None:
            raise ValueError("IMAGE_DIMENSIONS_UNSUPPORTED")
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        if raw[12:16] == b"VP8X":
            size = (1 + int.from_bytes(raw[24:27], "little"),
                    1 + int.from_bytes(raw[27:30], "little"))
        elif raw[12:16] == b"VP8L" and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            size = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        elif raw[12:16] == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", raw[26:30])
            size = width & 0x3FFF, height & 0x3FFF
        else:
            raise ValueError("IMAGE_DIMENSIONS_UNSUPPORTED")
    else:
        raise ValueError("IMAGE_DIMENSIONS_UNSUPPORTED")
    if any(not 1 <= value <= 100000 for value in size):
        raise ValueError("IMAGE_DIMENSIONS_INVALID")
    return size


def _artifact(root, item, name):
    data = _read_file(_local_file(root, item[name + "_path"]))
    if _sha256(data) != _digest(item[name + "_sha256"]):
        raise ValueError("IMAGE_ARTIFACT_HASH_MISMATCH")
    return data


def validate_image(root, document_id, item, inventory):
    """Validate all bindings before any transcription text can enter retrieval."""
    _fields(item, _IMAGE_FIELDS)
    sequence = item["sequence"]
    if type(sequence) is not int or not 1 <= sequence <= len(inventory):
        raise ValueError("IMAGE_SEQUENCE_INVALID")
    if item["source_url"] != inventory[sequence - 1]["source_url"]:
        raise ValueError("IMAGE_HTML_SOURCE_MISMATCH")
    for name in ("asset", "transcription", "review"):
        _string(item[name + "_path"], limit=1024)
    if len({item[name + "_path"] for name in ("asset", "transcription", "review")}) != 3:
        raise ValueError("IMAGE_ARTIFACT_PATHS_MUST_DIFFER")
    original = _artifact(root, item, "asset")
    width, height = item["width"], item["height"]
    if (type(width) is not int or type(height) is not int
            or (width, height) != image_dimensions(original)):
        raise ValueError("IMAGE_DIMENSIONS_MISMATCH")
    transcript_raw = _artifact(root, item, "transcription")
    transcript = _json(transcript_raw)
    _fields(transcript, _TRANSCRIPTION_FIELDS)
    bindings = {"document_id": document_id, **{key: item[key] for key in
                ("sequence", "source_url", "asset_sha256", "width", "height")}}
    if (transcript["schema_version"] != "policy-image-transcription-v1"
            or transcript["transcriber_kind"] != "ai"
            or any(type(transcript[key]) is not int for key in ("sequence", "width", "height"))
            or any(transcript[key] != value for key, value in bindings.items())):
        raise ValueError("IMAGE_TRANSCRIPTION_BINDING_MISMATCH")
    transcriber = _string(transcript["transcriber_id"], limit=200)
    blocks = transcript["blocks"]
    if type(blocks) is not list or not 1 <= len(blocks) <= 1000:
        raise ValueError("IMAGE_TRANSCRIPTION_BLOCKS_INVALID")
    ids = []
    for block in blocks:
        _fields(block, {"block_id", "kind", "bbox", "text"})
        block_id = _identifier(block["block_id"])
        if (len(block_id) > 32 or block_id in ids or type(block["kind"]) is not str
                or block["kind"] not in {"table", "text"}):
            raise ValueError("IMAGE_TRANSCRIPTION_BLOCK_INVALID")
        ids.append(block_id)
        box = block["bbox"]
        if (type(box) is not list or len(box) != 4 or any(type(value) is not int for value in box)
                or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
            raise ValueError("IMAGE_TRANSCRIPTION_BBOX_INVALID")
        text = _string(block["text"], limit=100000)
        if text != text.strip() or "\r" in text:
            raise ValueError("IMAGE_TRANSCRIPTION_TEXT_MUST_BE_CANONICAL")
    review_raw = _artifact(root, item, "review")
    review = _json(review_raw)
    _fields(review, _REVIEW_FIELDS)
    if (review["schema_version"] != "policy-image-review-v1"
            or review["document_id"] != document_id or type(review["sequence"]) is not int
            or review["sequence"] != sequence
            or review["asset_sha256"] != item["asset_sha256"]
            or review["transcription_sha256"] != item["transcription_sha256"]
            or review["reviewer_kind"] != "ai" or review["human_review"] is not False
            or review["method"] != "independent_visual_check" or review["status"] != "approved"
            or review["reviewed_blocks"] != ids):
        raise ValueError("IMAGE_INDEPENDENT_REVIEW_REQUIRED")
    reviewer = _string(review["reviewer_id"], limit=200)
    if reviewer == transcriber:
        raise ValueError("IMAGE_REVIEWER_MUST_DIFFER")
    if type(review["notes"]) is not list or len(review["notes"]) > 100:
        raise ValueError("IMAGE_REVIEW_NOTES_INVALID")
    for note in review["notes"]:
        _string(note, limit=10000)
    return transcript, {"asset": original, "transcription": transcript_raw, "review": review_raw}


def replay_html(document, raw, *, source=False):
    expected_protocol = EXTRACTION_PROTOCOL if source else DERIVATION_PROTOCOL
    if (document.get("extraction_protocol") != expected_protocol
            or document.get("encoding") not in {"utf-8", "gbk", "gb18030"}):
        raise ValueError("IMAGE_CORPUS_HTML_REPLAY_PROTOCOL_REQUIRED")
    try:
        return extract_policy_text(raw.decode(document["encoding"]), document["title"])
    except UnicodeError:
        raise ValueError("IMAGE_CORPUS_HTML_ENCODING_INVALID") from None


def derive_document(html_text, document_id, transcriptions):
    """Append complete blocks in image/block order; never split a table or note."""
    text = html_text
    chunks = make_chunks(html_text, document_id)
    block_locations = []
    for item, transcript in transcriptions:
        locations = []
        for block in transcript["blocks"]:
            text = text.rstrip() + "\n\n"
            start = len(text)
            text += block["text"] + "\n"
            end = start + len(block["text"])
            digest = _sha256(block["text"].encode("utf-8"))
            chunk_id = f"{document_id}:image-{item['sequence']:02d}-{block['block_id']}"
            _identifier(chunk_id)
            chunks.append({"chunk_id": chunk_id, "content": block["text"],
                           "start": start, "end": end, "sha256": digest})
            locations.append({"block_id": block["block_id"], "kind": block["kind"],
                              "bbox": block["bbox"], "start": start, "end": end, "sha256": digest})
        block_locations.append(locations)
    return text, chunks, block_locations


def image_coverage(count, reviewed_count):
    if reviewed_count:
        return ("text_with_ai_reviewed_image_blocks_images_omitted" if reviewed_count < count
                else "text_with_ai_reviewed_image_blocks")
    return "text_only_images_omitted" if count else "text_only"


def validate_v2_document(root, document, page_bytes, full_text):
    html_text, inventory = replay_html(document, page_bytes)
    if (document["image_count"] != len(inventory)
            or _digest(document["html_text_sha256"]) != _sha256(html_text.encode("utf-8"))):
        raise ValueError("IMAGE_CORPUS_HTML_REPLAY_MISMATCH")
    images = document["image_transcriptions"]
    if type(images) is not list or len(images) > len(inventory):
        raise ValueError("IMAGE_CORPUS_TRANSCRIPTIONS_INVALID")
    transcriptions, sequences = [], []
    for stored in images:
        _fields(stored, _IMAGE_FIELDS | {"blocks"})
        item = {key: stored[key] for key in _IMAGE_FIELDS}
        transcript, _assets = validate_image(root, document["document_id"], item, inventory)
        sequences.append(item["sequence"])
        transcriptions.append((item, transcript))
    if sequences != sorted(set(sequences)):
        raise ValueError("IMAGE_CORPUS_SEQUENCE_ORDER_INVALID")
    expected_omitted = [image for image in inventory if image["sequence"] not in sequences]
    if (document.get("omitted_images") != expected_omitted
            or document["coverage"] != image_coverage(len(inventory), len(images))):
        raise ValueError("IMAGE_CORPUS_COVERAGE_MISMATCH")
    if images and not document.get("scope_note", "").endswith(AI_SCOPE_NOTE):
        raise ValueError("IMAGE_CORPUS_AI_REVIEW_DISCLOSURE_REQUIRED")
    text, chunks, locations = derive_document(html_text, document["document_id"], transcriptions)
    if (text != full_text or chunks != document["chunks"]
            or any(stored["blocks"] != location for stored, location in zip(images, locations))):
        raise ValueError("IMAGE_CORPUS_DERIVED_TEXT_REPLAY_MISMATCH")


def validate_derivation(value):
    _fields(value, {"protocol", "source_corpus_sha256", "image_packet_sha256", "human_review"})
    if value["protocol"] != DERIVATION_PROTOCOL or value["human_review"] is not False:
        raise ValueError("IMAGE_CORPUS_DERIVATION_INVALID")
    _digest(value["source_corpus_sha256"])
    _digest(value["image_packet_sha256"])
