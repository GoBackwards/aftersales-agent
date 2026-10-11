"""Text extraction and chunking of saved public JD policy pages; never an execution policy.

The network collector (source catalog, fetch, snapshot writer) was deleted in S1-1e; see af5307e.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re


EXTRACTION_PROTOCOL = "jd-contxt-text-v1"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_MAJOR_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百]+条|[一二三四五六七八九十]+[、．.]|\d{1,2}、|(?:适用范围|退换货时限|退换货原因|售后流程|注意事项|收费标准|收费方式)[:：])")


class PublicPolicyCollectionError(ValueError):
    """Only stable, non-sensitive error codes cross the collection boundary."""


def _fail(code: str) -> None:
    raise PublicPolicyCollectionError(code)


class _PolicyBodyParser(HTMLParser):
    """Extract only JD's article container; keep table rows and whole paragraphs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.root_depth: int | None = None
        self.root_count = 0
        self.closed = False
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.images: list[dict] = []

    def _inside(self) -> bool:
        return self.root_depth is not None and not self.closed

    def _suppressed(self) -> bool:
        return any(tag in {"script", "style", "noscript", "template"} or attrs.get("hidden") is not None or attrs.get("aria-hidden") == "true" or re.search(r"display\s*:\s*none", attrs.get("style", ""), re.I) for tag, attrs in self.stack)

    def _in_table(self) -> bool:
        return any(tag == "table" for tag, _ in self.stack)

    def _separator(self, value: str = "\n\n") -> None:
        if self._inside() and not self._suppressed():
            self.parts.append(value)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: (value if value is not None else "") for key, value in attrs_list}
        if tag == "div" and "contxt" in attrs.get("class", "").split() and any(a.get("id") == "pdfContainer" for _, a in self.stack):
            self.root_count += 1
            if self.root_count > 1:
                _fail("ARTICLE_CONTAINER_AMBIGUOUS")
            self.root_depth = len(self.stack) + 1
        if tag not in _VOID:
            self.stack.append((tag, attrs))
        if not self._inside() or self._suppressed():
            return
        if tag == "img":
            self.images.append({"sequence": len(self.images) + 1, "source_url": attrs.get("src", ""), "alt": attrs.get("alt", "")})
        elif tag in {"td", "th"}:
            self._separator(" | ")
        elif tag == "br":
            self._separator("\n")
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "table"}:
            self._separator(" " if self._in_table() and tag != "table" else "\n\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._inside():
            if tag == "tr":
                self._separator("\n")
            elif tag in {"p", "li", "div", "h1", "h2", "h3", "h4", "table"}:
                self._separator(" " if self._in_table() and tag != "table" else "\n\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if self.root_depth is not None and index + 1 <= self.root_depth:
                    self.closed = True
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._inside() and not self._suppressed():
            self.parts.append(re.sub(r"\s+", " ", data))
            if any("help-tit1" in attrs.get("class", "").split() for _, attrs in self.stack):
                self.title_parts.append(data)


def extract_policy_text(html: str, expected_title: str) -> tuple[str, list[dict]]:
    parser = _PolicyBodyParser()
    parser.feed(html)
    parser.close()
    if parser.root_count != 1 or not parser.closed:
        _fail("ARTICLE_CONTAINER_MISSING_OR_INCOMPLETE")
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    if title != expected_title:
        _fail("ARTICLE_TITLE_MISMATCH")
    # Newlines in HTML source are not paragraph boundaries. Explicit block
    # separators remain two newlines; table rows and br remain one newline.
    text = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip().strip("|").strip() for line in text.split("\n")]
    normalized = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(normalized.replace(title, "", 1).strip()) < 20:
        _fail("ARTICLE_TEXT_MISSING")
    return normalized + "\n", parser.images


def make_chunks(text: str, document_id: str, max_characters: int = 1200) -> list[dict]:
    """Never slice through a paragraph, table, numbered section, or its notes."""
    paragraphs = list(re.finditer(r"\S(?:.*?\S)?(?=\n\n|\Z)", text.rstrip(), re.S))
    if not paragraphs:
        return []
    boundaries = [0]
    for paragraph in paragraphs[1:]:
        if _MAJOR_HEADING.match(paragraph.group()) and paragraph.start() > len(paragraphs[0].group()) + 2:
            boundaries.append(paragraph.start())
    boundaries.append(len(text.rstrip()))
    sections = [(start, end) for start, end in zip(boundaries, boundaries[1:]) if text[start:end].strip()]
    spans = []
    start = end = None
    for section_start, section_end in sections:
        if start is not None and section_end - start > max_characters:
            spans.append((start, end))
            start = None
        if start is None:
            start = section_start
        end = section_end
    if start is not None:
        spans.append((start, end))
    chunks = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        content = text[start:end]
        chunks.append({"chunk_id": f"{document_id}:section-{len(chunks) + 1:02d}", "content": content, "start": start, "end": end, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    return chunks
