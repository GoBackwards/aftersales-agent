"""Read-only BM25 retrieval over locally captured official policy snapshots.

This module never fetches URLs, calls a model, interprets page instructions, or
opens an order database. A lexical match is a candidate for review, not an
answer, a verified current policy, or authorization to perform an action.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from urllib.parse import urlsplit


PUBLIC_SCOPE = {"merchant": "jd_self_operated", "region": "CN", "language": "zh-CN",
                "purpose": "read_only_reference"}
_ROOT_FIELDS = {"schema_version", "corpus_id", "synthetic", "source_kind", "scope",
                "captured_at", "documents"}
_DOCUMENT_FIELDS = {"document_id", "title", "url", "fetched_at", "http_status", "raw_path",
                    "text_path", "raw_sha256", "text_sha256", "effective_from", "effective_to",
                    "coverage", "image_count", "chunks"}
_OPTIONAL_DOCUMENT_FIELDS = {"omitted_images", "content_type", "encoding", "extraction_protocol", "scope_note"}
_CHUNK_FIELDS = {"chunk_id", "content", "start", "end", "sha256"}
_SCOPE_MARKERS = ("淘宝", "天猫", "拼多多", "亚马逊", "苏宁", "amazon", "ebay", "京东国际",
                  "第三方", "非自营", "跨境", "境外", "海外购")
_MAX_FILE_BYTES = 16 * 1024 * 1024
OMITTED_IMAGE_COVERAGES = {"text_only_images_omitted", "text_with_ai_reviewed_image_blocks_images_omitted"}
AI_IMAGE_COVERAGES = {"text_with_ai_reviewed_image_blocks", "text_with_ai_reviewed_image_blocks_images_omitted"}


def image_warnings(evidence):
    """Selected evidence may contain omitted images and/or unaccepted AI text."""
    warnings = []
    if any(item["coverage"] in OMITTED_IMAGE_COVERAGES for item in evidence):
        warnings.append("source_images_not_indexed_or_verified")
    if any(item["coverage"] in AI_IMAGE_COVERAGES for item in evidence):
        warnings.append("image_transcriptions_ai_reviewed_not_human")
    return warnings


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def evidence_chars(evidence):
    """The full compact evidence JSON size; neither a token nor prompt budget."""
    return len(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("政策清单含重复 JSON 字段。")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("政策清单不允许非有限 JSON 数字。")


def _fields(value, required, optional=frozenset()):
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("政策清单字段不完整或包含未支持字段。")


def _string(value, *, limit=10000, allow_empty=False):
    if (type(value) is not str or len(value) > limit or "\x00" in value
            or (not allow_empty and not value.strip())):
        raise ValueError("政策文本字段类型、长度或内容无效。")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("政策文本字段必须是有效的 Unicode 文本。") from None
    return value


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("政策文档或片段标识符无效。")
    return value


def _timestamp(value):
    if (type(value) is not str
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value)):
        raise ValueError("采集时间必须是包含 UTC 时区的 ISO 时间。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("采集时间无效。") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("采集时间必须使用 UTC。")
    return value


def _official_url(value):
    value = _string(value, limit=2048)
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.netloc != "help.jd.com"
            or not re.fullmatch(r"/user/issue/\d+-\d+\.html", parts.path)
            or parts.query or parts.fragment
            or value != f"https://help.jd.com{parts.path}"):
        raise ValueError("政策 URL 必须是允许的京东官方 HTTPS 帮助页面。")
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("政策文件或片段 SHA256 无效。")
    return value


def _ordinary_path(path, *, file=True):
    """Check every existing component before reading; never follow a link."""
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except (OSError, ValueError):
            raise ValueError("政策快照路径不存在或不可读取。") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("政策快照路径不能包含符号链接或 Windows 重解析点。")
        if component == path:
            expected = stat.S_ISREG(info.st_mode) if file else stat.S_ISDIR(info.st_mode)
            if not expected:
                raise ValueError("政策快照必须使用普通文件和目录。")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("政策快照父路径必须是普通目录。")
    return path


def _read_file(path):
    _ordinary_path(path)
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("政策快照文件超过本地读取上限。")
        data = path.read_bytes()
    except OSError:
        raise ValueError("政策快照文件不可读取。") from None
    if not data or len(data) > _MAX_FILE_BYTES:
        raise ValueError("政策快照文件为空或超过本地读取上限。")
    return data


def _local_file(root, relative):
    relative = _string(relative, limit=1024)
    parts = PurePosixPath(relative)
    if (parts.is_absolute() or PureWindowsPath(relative).drive or "\\" in relative
            or any(mark in relative for mark in (":", "?", "#"))
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("政策文件必须使用语料根内的普通相对路径。")
    path = root.joinpath(*parts.parts)
    _ordinary_path(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("政策文件不能离开语料根目录。")
    return path


def _terms(text):
    """Fixed character bigrams and ASCII words; no labels or gold keywords."""
    result = []
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._-][a-z0-9]+)*", text.casefold()):
        if "\u3400" <= run[0] <= "\u9fff":
            result.extend(run[index:index + 2] for index in range(len(run) - 1))
            if len(run) == 1:
                result.append(run)
        else:
            result.append(run)
    return result


class PublicPolicyIndex:
    """Immutable in-memory view of one validated, explicitly scoped snapshot."""

    def __init__(self, corpus_path):
        path = Path(corpus_path).absolute()
        raw_manifest = _read_file(path)
        try:
            payload = json.loads(raw_manifest.decode("utf-8-sig"), object_pairs_hook=_object,
                                 parse_constant=_reject_constant)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("政策清单不是有效的 UTF-8 JSON。") from None
        is_v2 = type(payload) is dict and payload.get("schema_version") == "official-policy-corpus-v2"
        _fields(payload, _ROOT_FIELDS | ({"derivation"} if is_v2 else set()))
        version = "v2" if is_v2 else "v1"
        if (payload["schema_version"] != f"official-policy-corpus-{version}"
                or payload["corpus_id"] != f"jd-self-operated-after-sales-cn-{version}"
                or payload["synthetic"] is not False
                or payload["source_kind"] != "official_public_web"):
            raise ValueError("需要明确标为官方公开网页快照的受支持政策语料。")
        if is_v2:
            from .public_policy_images import validate_derivation, validate_v2_document
            validate_derivation(payload["derivation"])
        if type(payload["scope"]) is not dict or payload["scope"] != PUBLIC_SCOPE:
            raise ValueError("官方政策语料必须明确限定京东自营、中国大陆、只读引用范围。")
        self._captured_at = _timestamp(payload["captured_at"])
        documents = payload["documents"]
        if type(documents) is not list or not documents or len(documents) > 100:
            raise ValueError("官方政策语料需要 1 至 100 份文档。")
        self._documents, self._records, self._evidence = [], [], {}
        seen_documents, seen_chunks = set(), set()
        for document in documents:
            _fields(document, _DOCUMENT_FIELDS | ({"image_transcriptions", "html_text_sha256"} if is_v2 else set()),
                    _OPTIONAL_DOCUMENT_FIELDS)
            document_id = _identifier(document["document_id"])
            if document_id in seen_documents:
                raise ValueError("官方政策文档 ID 重复。")
            seen_documents.add(document_id)
            _string(document["title"], limit=1000)
            _official_url(document["url"])
            _timestamp(document["fetched_at"])
            if type(document["http_status"]) is not int or document["http_status"] != 200:
                raise ValueError("只允许 HTTP 200 的已采集官方页面。")
            if document["effective_from"] is not None or document["effective_to"] is not None:
                raise ValueError("本快照协议未核实生效日期，必须保留为 null。")
            count = document["image_count"]
            if type(count) is not int or count < 0 or count > 10000:
                raise ValueError("图片数量必须是有效的非负整数。")
            if not is_v2 and document["coverage"] != ("text_only_images_omitted" if count else "text_only"):
                raise ValueError("政策文字覆盖标记与遗漏图片数量不一致。")
            for field in ("content_type", "encoding", "extraction_protocol"):
                if field in document:
                    _string(document[field], limit=200)
            if "scope_note" in document:
                _string(document["scope_note"], limit=2000)
            if "omitted_images" in document and not is_v2:
                images = document["omitted_images"]
                if type(images) is not list or len(images) != count:
                    raise ValueError("遗漏图片清单与图片数量不一致。")
                for number, omitted in enumerate(images, start=1):
                    _fields(omitted, {"sequence", "source_url", "alt"})
                    if type(omitted["sequence"]) is not int or omitted["sequence"] != number:
                        raise ValueError("遗漏图片序号必须连续。")
                    _string(omitted["source_url"], limit=4096, allow_empty=True)
                    _string(omitted["alt"], limit=2048, allow_empty=True)
            raw_path = _local_file(path.parent, document["raw_path"])
            text_path = _local_file(path.parent, document["text_path"])
            if raw_path == text_path:
                raise ValueError("原始页面与归一文本必须使用不同文件。")
            page_bytes, text_bytes = _read_file(raw_path), _read_file(text_path)
            if (_sha256(page_bytes) != _digest(document["raw_sha256"])
                    or _sha256(text_bytes) != _digest(document["text_sha256"])):
                raise ValueError("政策文件内容与清单 SHA256 不一致。")
            try:
                full_text = text_bytes.decode("utf-8")
            except UnicodeError:
                raise ValueError("政策归一文本必须使用 UTF-8。") from None
            if is_v2:
                validate_v2_document(path.parent, document, page_bytes, full_text)
            chunks = document["chunks"]
            if type(chunks) is not list or not chunks or len(chunks) > 2000:
                raise ValueError("每份政策文档需要 1 至 2000 个完整片段。")
            previous_end = 0
            for chunk in chunks:
                _fields(chunk, _CHUNK_FIELDS)
                chunk_id = _identifier(chunk["chunk_id"])
                if chunk_id in seen_chunks:
                    raise ValueError("官方政策片段 ID 重复。")
                seen_chunks.add(chunk_id)
                content = _string(chunk["content"], limit=_MAX_FILE_BYTES)
                start, end = chunk["start"], chunk["end"]
                if (type(start) is not int or type(end) is not int
                        or not previous_end <= start < end <= len(full_text)
                        or full_text[start:end] != content
                        or _sha256(content.encode("utf-8")) != _digest(chunk["sha256"])):
                    raise ValueError("政策片段偏移、原文或 SHA256 不一致。")
                previous_end = end
                citation_id = f"{document_id}@{document['text_sha256'][:12]}#{chunk_id}"
                evidence = {"document_id": document_id, "chunk_id": chunk_id,
                            "citation_id": citation_id, "title": document["title"],
                            "url": document["url"], "fetched_at": document["fetched_at"],
                            "content": content, "sha256": chunk["sha256"], "score": 0.0,
                            "coverage": document["coverage"], "effective_from": None,
                            "effective_to": None}
                if "scope_note" in document:
                    evidence["scope_note"] = document["scope_note"]
                frequencies = Counter(_terms(document["title"] + "\n" + content))
                self._records.append({"evidence": evidence, "frequencies": frequencies,
                                      "length": sum(frequencies.values())})
                self._evidence[citation_id] = evidence
            self._documents.append(deepcopy(document))
        self._corpus_sha256 = _sha256(raw_manifest)
        self._document_frequencies = Counter()
        for record in self._records:
            self._document_frequencies.update(record["frequencies"].keys())
        self._average_length = sum(record["length"] for record in self._records) / len(self._records)

    @property
    def corpus_sha256(self):
        return self._corpus_sha256

    @property
    def documents(self):
        return deepcopy(self._documents)

    @property
    def scope(self):
        return deepcopy(PUBLIC_SCOPE)

    @property
    def captured_at(self):
        return self._captured_at

    def evidence_by_id(self, citation_id):
        """Lookup only in memory; citation IDs are never treated as paths."""
        return deepcopy(self._evidence.get(citation_id)) if type(citation_id) is str else None

    def search(self, query, top_k=3, max_context_chars=3000,
               merchant="jd_self_operated", region="CN", document_ids=None):
        query = _string(query, limit=4000)
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k 必须是 1 至 100 的整数。")
        if type(max_context_chars) is not int or not 2 <= max_context_chars <= 1000000:
            raise ValueError("上下文字符预算必须是 2 至 1000000 的整数。")
        _string(merchant, limit=100)
        _string(region, limit=100)
        if document_ids is not None and (type(document_ids) is not list or not document_ids
                or any(type(value) is not str or not value for value in document_ids)):
            raise ValueError("document_ids 必须为非空来源 ID 列表。")
        result = {"schema_version": "public-policy-search-v1", "status": "no_evidence",
                  "mode": "offline_public_policy_lexical", "read_only": True,
                  "writes_allowed": False, "result_kind": "retrieval_candidates_not_answer",
                  "source_kind": "official_public_web", "corpus_sha256": self._corpus_sha256,
                  "scope": self.scope, "requested_scope": {"merchant": merchant, "region": region},
                  "captured_at": self._captured_at,
                  "validity": "snapshot_not_current_policy_guarantee", "query": query,
                  "integrity": "local_manifest_consistency_only",
                  "authenticity": "not_independently_verified",
                  "top_k": top_k, "max_context_chars": max_context_chars, "context_chars": 2,
                  "candidate_count": 0, "candidate_ids": [], "budget_omitted_ids": [],
                  "evidence": [], "warnings": ["effective_dates_unverified", "answer_requires_review"],
                  "retrieval": {"method": "bm25", "tokenizer": "cjk_bigrams_ascii_words_v1",
                                "k1": 1.2, "b": 0.75, "embeddings": False},
                  "limitations": ["仅对本地快照进行字面检索；未使用嵌入向量，不保证理解同义表达。",
                                  "命中只代表候选证据，不保证回答完整、适用或仍为当前政策。",
                                  "本地哈希只核对清单与文件的一致性，不能证明两者未同时被改写，也不独立验证网页当前真伪。",
                                  "范围提示仅检查有限字面标记，不能完整识别意图或否定关系。",
                                  "预算包含完整证据 JSON 元数据，不是 token 或整个提示词预算。"]}
        if merchant != PUBLIC_SCOPE["merchant"] or region != PUBLIC_SCOPE["region"]:
            result["status"] = "scope_mismatch"
            result["warnings"].append("requested_scope_not_in_corpus")
            return result
        scope_markers = [marker for marker in _SCOPE_MARKERS if marker in query.casefold()]
        if scope_markers:
            result.update(status="scope_warning", scope_warning_markers=scope_markers)
            result["warnings"].append("possible_out_of_scope_query_requires_review")
            return result
        query_terms = set(_terms(query))
        candidates = []
        count = len(self._records)
        for record in self._records:
            if document_ids is not None and record['evidence']['document_id'] not in document_ids:
                continue
            score = 0.0
            for term in sorted(query_terms & record["frequencies"].keys()):
                frequency = record["frequencies"][term]
                inverse_frequency = math.log(1 + (count - self._document_frequencies[term] + 0.5)
                                             / (self._document_frequencies[term] + 0.5))
                length_ratio = record["length"] / self._average_length if self._average_length else 0
                score += inverse_frequency * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length_ratio))
            if score > 0:
                candidates.append((score, {**record["evidence"], "score": round(score, 8)}))
        candidates.sort(key=lambda item: (-item[0], item[1]["citation_id"]))
        result["candidate_count"] = len(candidates)
        result["candidate_ids"] = [item[1]["citation_id"] for item in candidates]
        for _score, candidate in candidates:
            if len(result["evidence"]) >= top_k:
                break
            proposed = result["evidence"] + [candidate]
            if evidence_chars(proposed) > max_context_chars:
                result["budget_omitted_ids"].append(candidate["citation_id"])
                continue
            result["evidence"] = proposed
        result["context_chars"] = evidence_chars(result["evidence"])
        if result["evidence"]:
            result["status"] = "evidence_found"
        elif candidates:
            result["status"] = "budget_exhausted"
        if result["budget_omitted_ids"]:
            result["warnings"].append("whole_chunks_omitted_for_context_budget")
        result["warnings"].extend(image_warnings(result["evidence"]))
        if not candidates:
            result["warnings"].append("no_literal_match_does_not_prove_policy_absence")
        return deepcopy(result)
