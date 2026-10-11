"""Uploaded policy text for the knowledge base, kept apart from official snapshots.

An uploaded document is plain text supplied by the operator of this demo. It is
split into stable sections whose exact bytes are hashed, so policy cards can
quote them the same way they quote official passages. Nothing here fetches a
URL, interprets instructions inside the text or claims the text is official.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

UPLOAD_KIND = "merchant_upload"
UPLOAD_COVERAGE = "merchant_uploaded_text"
# The source note an uploaded section carries (B5d1, production voice): what it is and what nobody checked.
UPLOAD_SCOPE_NOTE = "商家补充的政策文本，由知识库管理员上传，没有和平台公开规则逐条核对过。"
MAX_DOCUMENTS = 50
MAX_SECTIONS = 200
MAX_DOCUMENT_CHARS = 200_000
MAX_SECTION_CHARS = 20_000
TARGET_SECTION_CHARS = 900
_DOCUMENT_ID = re.compile(r"upload-[a-z0-9][a-z0-9-]{0,62}\Z")
_SECTION_ID = re.compile(r"s[0-9]{2,3}\Z")
_HEADING = re.compile(r"^(#{1,6}\s+\S|第[一二三四五六七八九十百零0-9]+[条章节部分]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])")


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_text(value, *, limit, name):
    if type(value) is not str:
        raise ValueError(f"KB_{name}_INVALID")
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if "\x00" in value or len(value) > limit:
        raise ValueError(f"KB_{name}_INVALID")
    return value


def normalize_upload_text(text):
    """Stable text used for hashing and quoting: LF lines, no trailing spaces."""
    text = _clean_text(text, limit=MAX_DOCUMENT_CHARS, name="UPLOAD_TEXT")
    lines = [line.rstrip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise ValueError("KB_UPLOAD_TEXT_EMPTY")
    return text


def validate_public_url(value):
    """Optional attribution link; only a plain public https URL is kept."""
    if value in (None, ""):
        return ""
    if type(value) is not str or len(value) > 2048:
        raise ValueError("KB_UPLOAD_URL_INVALID")
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not host or "." not in host or parts.username or parts.password
            or host.endswith((".local", ".localhost")) or re.fullmatch(r"[0-9.]+", host)):
        raise ValueError("KB_UPLOAD_URL_INVALID")
    return value.strip()


def split_sections(text, *, title):
    """Group paragraphs into quotable sections, starting new ones at headings.

    Every section is an exact substring of ``text``: pieces cut from one long
    paragraph are rejoined without separators, whole paragraphs with the blank
    line that separated them in the upload.
    """
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    sections, current = [], []

    def flush():
        if current:
            body = "".join(part if index == 0 else separator + part
                           for index, (separator, part) in enumerate(current))
            first = body.lstrip().split("\n", 1)[0].lstrip("#").strip()
            heading = first[:40] if _HEADING.match(body.lstrip()) else ""
            sections.append({"title": heading, "text": body})
            current.clear()

    for paragraph in paragraphs:
        # Very long paragraphs are cut on sentence ends (then at a fixed width
        # when a sentence is itself too long) so each quote target stays
        # reviewable; the cut is recorded only by the section boundary.
        pieces = [paragraph]
        if len(paragraph) > MAX_SECTION_CHARS // 2:
            pieces = []
            for sentence in (piece for piece in re.split(r"(?<=[。！？；.!?;])", paragraph) if piece):
                width = MAX_SECTION_CHARS // 2
                pieces.extend(sentence[start:start + width] for start in range(0, len(sentence), width))
        for position, piece in enumerate(pieces):
            size = sum(len(part) for _separator, part in current)
            if current and (_HEADING.match(piece.lstrip()) or size + len(piece) > TARGET_SECTION_CHARS):
                flush()
            current.append(("" if position else "\n\n", piece))
    flush()
    if not sections:
        raise ValueError("KB_UPLOAD_TEXT_EMPTY")
    if len(sections) > MAX_SECTIONS:
        raise ValueError("KB_UPLOAD_TOO_MANY_SECTIONS")
    result = []
    for index, section in enumerate(sections, 1):
        name = section["title"] or f"{title} · 第 {index} 段"
        result.append({"section_id": f"s{index:02d}", "title": name[:80],
                       "text": section["text"], "sha256": _sha256(section["text"])})
    return result


def slug_document_id(title, existing):
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title).encode("ascii", "ignore")
                  .decode("ascii").lower()).strip("-")[:40] or "doc"
    digest = _sha256(title + "\n" + str(len(existing)))[:8]
    candidate = f"upload-{base}-{digest}"
    while candidate in existing:
        digest = _sha256(candidate)[:8]
        candidate = f"upload-{base}-{digest}"
    return candidate


def build_uploaded_document(title, text, *, url="", existing_ids=(), now=None):
    title = _clean_text(title, limit=200, name="UPLOAD_TITLE").strip()
    if not title:
        raise ValueError("KB_UPLOAD_TITLE_INVALID")
    text = normalize_upload_text(text)
    added = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    document_id = slug_document_id(title, set(existing_ids))
    return {"document_id": document_id, "title": title, "url": validate_public_url(url),
            "kind": UPLOAD_KIND, "added_at": added, "text_sha256": _sha256(text),
            "sections": split_sections(text, title=title)}


def evidence_id(document_id, section_id):
    return f"upload:{document_id}#{section_id}"


def supplementary_evidence(documents):
    """Validate uploaded documents and return evidence items keyed by ID.

    Items use the same fields as official parent evidence so card validation,
    retrieval and source cards need no special cases. Hashes bind exact text.
    """
    if documents in (None, []):
        return {}
    if type(documents) is not list or len(documents) > MAX_DOCUMENTS:
        raise ValueError("KB_SUPPLEMENTARY_SOURCES_INVALID")
    result, seen = {}, set()
    for document in documents:
        if (type(document) is not dict or type(document.get("document_id")) is not str
                or not _DOCUMENT_ID.fullmatch(document["document_id"])
                or document["document_id"] in seen or document.get("kind") != UPLOAD_KIND
                or type(document.get("sections")) is not list
                or not 1 <= len(document["sections"]) <= MAX_SECTIONS):
            raise ValueError("KB_SUPPLEMENTARY_SOURCES_INVALID")
        seen.add(document["document_id"])
        title = _clean_text(document.get("title"), limit=200, name="UPLOAD_TITLE").strip()
        url = validate_public_url(document.get("url", ""))
        if not title:
            raise ValueError("KB_UPLOAD_TITLE_INVALID")
        section_ids = set()
        for section in document["sections"]:
            if (type(section) is not dict or type(section.get("section_id")) is not str
                    or not _SECTION_ID.fullmatch(section["section_id"])
                    or section["section_id"] in section_ids):
                raise ValueError("KB_SUPPLEMENTARY_SECTION_INVALID")
            section_ids.add(section["section_id"])
            text = _clean_text(section.get("text"), limit=MAX_SECTION_CHARS, name="SECTION_TEXT")
            if not text.strip() or section.get("sha256") != _sha256(text):
                raise ValueError("KB_SUPPLEMENTARY_SECTION_HASH_MISMATCH")
            section_title = _clean_text(section.get("title", ""), limit=200, name="SECTION_TITLE").strip()
            identifier = evidence_id(document["document_id"], section["section_id"])
            display_title = title if not section_title or section_title.startswith(title) else f"{title} · {section_title}"
            source = {"title": display_title, "url": url, "chunk_id": identifier,
                      "citation_id": identifier, "document_id": document["document_id"],
                      "sha256": section["sha256"], "scope_note": UPLOAD_SCOPE_NOTE,
                      "coverage": UPLOAD_COVERAGE, "source_kind": UPLOAD_KIND}
            item = {"id": identifier, "title": display_title, "text": text, "source_url": url,
                    "source_id": identifier, "parent_citation_id": identifier,
                    "document_id": document["document_id"], "sha256": section["sha256"],
                    "spans": [{"start": 0, "end": len(text), "text": text, "document_start": 0,
                               "document_end": len(text), "sha256": section["sha256"]}],
                    "scope_note": UPLOAD_SCOPE_NOTE, "coverage": UPLOAD_COVERAGE}
            result[identifier] = (item, source)
    return result
