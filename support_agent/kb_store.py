"""Versioned policy-card knowledge base for the product console.

Layout under one directory::

    versions/<sha256>.json   immutable published catalogs (content addressed)
    index.json               ordered version metadata
    active.json              the published version the chat runtime reads
    history.jsonl            append-only publish/activate/rollback events
    draft.json               the single working draft edited in the console

A turn reads exactly one immutable version file, so publishing or rolling back
never changes a conversation that is already running. Every version is checked
with the same loader the runtime uses before it can become active; this proves
source binding and verbatim quotes, not that a rule is correct or current. B6: that
loader also refuses a catalogue over the publication cap (D15, catalog.cap_problem),
so a draft edit, a publish or an activation past it fails with the loader's message.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from .catalog import (DEFAULT_SOURCE_KIND, MAX_ALIAS_CHARS, MAX_ALIASES, SOURCE_KINDS, alias_problems,
                          publisher_problems)
from .kb_sources import build_uploaded_document, supplementary_evidence

SCHEMA = "kb-store-v1"
EFFECT_DOMAINS = {
    "return_eligibility": "退换资格", "eligibility_deadline": "申请时限", "return_items": "退回物品与赠品",
    "return_deduction": "退款扣减", "return_shipping_responsibility": "退货运费承担",
    "return_shipping_compensation": "退货运费补偿", "return_shipping_benefit": "运费权益",
    "return_pickup_fee": "上门取件费用", "return_pickup_procedure": "上门取件流程",
    "repair_procedure": "维修流程", "repair_shipping_responsibility": "维修运费",
    "warranty_coverage": "质保范围", "exchange_procedure": "换货流程", "general_guidance": "一般说明",
}
ORIGINS = {"baseline", "edited", "created", "ai_draft"}
_CARD_ID = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_LIMITS = {"title": 80, "scope": 400, "item": 600, "premise": 400, "quote": 2000, "note": 400}


class KBValidationError(ValueError):
    """Field-level problems a console user can fix; codes stay stable for tests."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encode(catalog):
    return (json.dumps(catalog, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _strings(value, field, errors, *, minimum, maximum, limit, allow_empty_list=False):
    if type(value) is not list:
        errors.append({"field": field, "message": "必须是文本列表"})
        return []
    cleaned = [item.strip() for item in value if type(item) is str and item.strip()]
    if len(cleaned) != len(value):
        errors.append({"field": field, "message": "不能包含空白或非文本项"})
    if not allow_empty_list and len(cleaned) < minimum:
        errors.append({"field": field, "message": f"至少需要 {minimum} 项"})
    if len(cleaned) > maximum:
        errors.append({"field": field, "message": f"最多 {maximum} 项"})
    for index, item in enumerate(cleaned):
        if len(item) > limit:
            errors.append({"field": f"{field}[{index}]", "message": f"不能超过 {limit} 字"})
    return cleaned


def normalize_card(proposal, *, evidence, existing_card=None):
    """Check one editable card against source text; return the stored form.

    ``evidence`` maps evidence IDs to items with ``text`` and ``sha256``. The
    returned card always has per-outcome premises and server-computed hashes.
    """
    errors = []
    if type(proposal) is not dict:
        raise KBValidationError([{"field": "card", "message": "卡片必须是对象"}])
    card_id = proposal.get("id")
    if type(card_id) is not str or not _CARD_ID.fullmatch(card_id):
        errors.append({"field": "id", "message": "ID 须以小写字母开头，只含小写字母、数字和下划线（2–64 位）"})
    fields = {}
    for name in ("title", "scope"):
        value = proposal.get(name)
        if type(value) is not str or not value.strip():
            errors.append({"field": name, "message": "不能为空"})
        elif len(value.strip()) > _LIMITS[name]:
            errors.append({"field": name, "message": f"不能超过 {_LIMITS[name]} 字"})
        fields[name] = value.strip() if type(value) is str else ""
    domain = proposal.get("effect_domain")
    if domain not in EFFECT_DOMAINS:
        errors.append({"field": "effect_domain", "message": "请选择一个已登记的规则类型"})
    conditions = _strings(proposal.get("conditions"), "conditions", errors, minimum=1, maximum=12, limit=_LIMITS["item"])
    outcomes = _strings(proposal.get("outcomes"), "outcomes", errors, minimum=1, maximum=6, limit=_LIMITS["item"])
    exceptions = _strings(proposal.get("exceptions", []), "exceptions", errors, minimum=0, maximum=12,
                          limit=_LIMITS["item"], allow_empty_list=True)
    premises_value = proposal.get("outcome_premises")
    premises = []
    if type(premises_value) is not list or len(premises_value) != len(proposal.get("outcomes") or []):
        errors.append({"field": "outcome_premises", "message": "每条结论都需要一组适用前提"})
    else:
        for index, rows in enumerate(premises_value):
            premises.append(_strings(rows, f"outcome_premises[{index}]", errors, minimum=1, maximum=6,
                                     limit=_LIMITS["premise"]))
    references_value = proposal.get("references")
    references = []
    if type(references_value) is not list or not 1 <= len(references_value) <= 6:
        errors.append({"field": "references", "message": "需要 1–6 条原文引用"})
        references_value = references_value if type(references_value) is list else []
    seen = set()
    for index, ref in enumerate(references_value):
        field = f"references[{index}]"
        if type(ref) is not dict or type(ref.get("evidence_id")) is not str:
            errors.append({"field": field, "message": "引用需要来源段落"})
            continue
        identifier = ref["evidence_id"]
        if identifier in seen:
            errors.append({"field": field, "message": "同一来源段落只能引用一次，可在其中添加多段原文"})
            continue
        seen.add(identifier)
        item = evidence.get(identifier)
        if item is None:
            errors.append({"field": field, "message": "来源段落不存在于当前知识版本"})
            continue
        quotes = _strings(ref.get("quotes"), field + ".quotes", errors, minimum=1, maximum=6, limit=_LIMITS["quote"])
        for position, quote in enumerate(quotes):
            if quote not in item["text"]:
                errors.append({"field": f"{field}.quotes[{position}]",
                               "message": "引用文字必须与来源原文逐字一致（含标点）"})
        references.append({"evidence_id": identifier, "parent_sha256": item["sha256"], "quotes": quotes})
    note = proposal.get("review_note", "")
    if type(note) is not str or len(note) > _LIMITS["note"]:
        errors.append({"field": "review_note", "message": f"备注不能超过 {_LIMITS['note']} 字"})
        note = ""
    previous = existing_card or {}
    # B5d1: the source kind (guard G5; D16 publishes no law-source card) and the aliases (guard G3's rule
    # names). An edit that leaves them out keeps the card's own; a new card is a platform rule with none.
    source_kind = proposal.get("source_kind", previous.get("source_kind", DEFAULT_SOURCE_KIND))
    if source_kind not in SOURCE_KINDS:
        errors.append({"field": "source_kind", "message": "请选择一个已登记的来源类型"})
    aliases = _strings(proposal.get("aliases", previous.get("aliases", [])), "aliases", errors, minimum=0,
                       maximum=MAX_ALIASES, limit=MAX_ALIAS_CHARS, allow_empty_list=True)
    errors += [{"field": field, "message": message} for field, message in alias_problems(
        {**fields, "conditions": conditions, "outcomes": outcomes, "exceptions": exceptions,
         "outcome_premises": premises, "references": references, "aliases": aliases})]
    if errors:
        raise KBValidationError(errors)
    support = previous.get("support_scope_ids") or ["kb-editor"]
    origin = proposal.get("origin") if proposal.get("origin") in ORIGINS else None
    if origin is None:
        origin = "edited" if existing_card is not None else "created"
    card = {"id": card_id, "effect_domain": domain, "title": fields["title"], "source_kind": source_kind,
            "aliases": aliases, "support_scope_ids": list(support), "scope": fields["scope"], "conditions": conditions,
            "outcomes": outcomes, "outcome_premises": premises, "exceptions": exceptions,
            "evidence_ids": [ref["evidence_id"] for ref in references], "references": references,
            "kb": {"origin": origin, "updated_at": _now(), "review_note": note.strip()}}
    return card


def card_changed(before, after):
    keys = ("effect_domain", "title", "source_kind", "aliases", "scope", "conditions", "outcomes", "outcome_premises",
            "exceptions", "references")
    defaults = {"source_kind": DEFAULT_SOURCE_KIND, "aliases": []}
    return [key for key in keys
            if (before or {}).get(key, defaults.get(key)) != (after or {}).get(key, defaults.get(key))]


def diff_catalogs(before, after):
    old = {card["id"]: card for card in (before or {}).get("cards", [])}
    new = {card["id"]: card for card in (after or {}).get("cards", [])}
    changed = []
    for identifier in sorted(old.keys() & new.keys()):
        fields = card_changed(old[identifier], new[identifier])
        if fields:
            changed.append({"id": identifier, "title": new[identifier]["title"], "fields": fields})
    old_docs = {doc["document_id"]: doc for doc in (before or {}).get("supplementary_sources", [])}
    new_docs = {doc["document_id"]: doc for doc in (after or {}).get("supplementary_sources", [])}
    publishers = [(catalog or {}).get("source_publishers", []) for catalog in (before, after)]
    return {"added": [{"id": key, "title": new[key]["title"]} for key in sorted(new.keys() - old.keys())],
            "removed": [{"id": key, "title": old[key]["title"]} for key in sorted(old.keys() - new.keys())],
            "changed": changed,
            "unchanged_count": len(old.keys() & new.keys()) - len(changed),
            "sources_added": [{"id": key, "title": new_docs[key]["title"]} for key in sorted(new_docs.keys() - old_docs.keys())],
            "sources_removed": [{"id": key, "title": old_docs[key]["title"]} for key in sorted(old_docs.keys() - new_docs.keys())],
            # B5g (D8): the catalogue's source publishers (G10's lexicon), when they changed
            "publishers": None if publishers[0] == publishers[1] else {"before": publishers[0], "after": publishers[1]}}


class KnowledgeBaseStore:
    """File-backed store; one process owns writes (the local service)."""

    def __init__(self, directory, *, baseline_catalog, official_evidence, runtime_check):
        """``official_evidence``: {id: item} from the pinned release (read only).
        ``runtime_check(path)`` must raise ValueError for a catalog the chat
        runtime would reject; it is the same loader used during a turn.
        """
        self.directory = Path(directory)
        self.official_evidence = official_evidence
        self.runtime_check = runtime_check
        self.lock = threading.RLock()
        with self.lock:
            (self.directory / "versions").mkdir(parents=True, exist_ok=True)
            if not (self.directory / "index.json").exists():
                self._seed(Path(baseline_catalog))
            self._active = _read_json(self.directory / "active.json")

    # ----- storage helpers -------------------------------------------------
    def _index(self):
        return _read_json(self.directory / "index.json")

    def _version_path(self, version_id):
        if type(version_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", version_id):
            raise KeyError("KB_VERSION_NOT_FOUND")
        path = self.directory / "versions" / f"{version_id}.json"
        if not path.exists():
            raise KeyError("KB_VERSION_NOT_FOUND")
        return path

    def _history_append(self, event):
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with (self.directory / "history.jsonl").open("a", encoding="utf-8", newline="\n") as output:
            output.write(line)

    def _store_version(self, catalog, *, label, author, parent):
        raw = _encode(catalog)
        version_id = _sha(raw)
        index = self._index() if (self.directory / "index.json").exists() else {"schema": SCHEMA, "versions": []}
        existing = next((row for row in index["versions"] if row["version_id"] == version_id), None)
        if existing is not None:
            return existing
        self.runtime_check_bytes(raw)
        _atomic_write(self.directory / "versions" / f"{version_id}.json", raw)
        row = {"version_id": version_id, "number": len(index["versions"]) + 1, "label": label,
               "created_at": _now(), "author": author, "parent_version_id": parent,
               "card_count": len(catalog["cards"]),
               "supplementary_source_count": len(catalog.get("supplementary_sources", []))}
        index["versions"].append(row)
        _atomic_write(self.directory / "index.json", _encode(index))
        return row

    def runtime_check_bytes(self, raw):
        with tempfile.TemporaryDirectory(prefix="kb-check-") as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_bytes(raw)
            self.runtime_check(path)

    def _activate(self, version_id, *, reason, author, operation):
        self._version_path(version_id)
        previous = self._active if hasattr(self, "_active") else None
        pointer = {"schema": SCHEMA, "version_id": version_id,
                   "generation": (previous or {}).get("generation", 0) + 1,
                   "activated_at": _now(), "reason": reason, "author": author}
        _atomic_write(self.directory / "active.json", _encode(pointer))
        self._history_append({"event": operation, **pointer,
                              "previous_version_id": (previous or {}).get("version_id")})
        self._active = pointer
        return pointer

    def _seed(self, baseline):
        catalog = json.loads(baseline.read_text(encoding="utf-8-sig"))
        for card in catalog.get("cards", []):
            card.setdefault("kb", {"origin": "baseline", "updated_at": None, "review_note": ""})
        row = self._store_version(catalog, label="仓库基线卡片", author="system", parent=None)
        self._activate(row["version_id"], reason="首次启动导入仓库中的卡片目录", author="system", operation="seed")

    # ----- read API ---------------------------------------------------------
    def active_pointer(self):
        with self.lock:
            return dict(self._active)

    def active_catalog_path(self):
        with self.lock:
            return self._version_path(self._active["version_id"])

    def load_version(self, version_id):
        return json.loads(self._version_path(version_id).read_text(encoding="utf-8"))

    def active_catalog(self):
        return self.load_version(self.active_pointer()["version_id"])

    def versions(self):
        with self.lock:
            index = self._index()
            active = self._active["version_id"]
            rows = [{**row, "active": row["version_id"] == active} for row in index["versions"]]
            return list(reversed(rows))

    def history(self, limit=50):
        path = self.directory / "history.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines) if line.strip()]

    def draft(self):
        with self.lock:
            path = self.directory / "draft.json"
            if path.exists():
                return _read_json(path)
            return {"schema": SCHEMA, "base_version_id": self._active["version_id"], "updated_at": None,
                    "catalog": self.active_catalog()}

    def evidence_for(self, catalog):
        """Official evidence plus the catalog's own uploaded sections."""
        merged = dict(self.official_evidence)
        for identifier, (item, _source) in supplementary_evidence(catalog.get("supplementary_sources", [])).items():
            merged[identifier] = item
        return merged

    def draft_status(self):
        with self.lock:
            draft = self.draft()
            active = self.active_catalog()
            difference = diff_catalogs(active, draft["catalog"])
            dirty = bool(difference["added"] or difference["removed"] or difference["changed"]
                         or difference["sources_added"] or difference["sources_removed"] or difference["publishers"])
            return {"base_version_id": draft["base_version_id"], "updated_at": draft["updated_at"],
                    "stale": draft["base_version_id"] != self._active["version_id"], "dirty": dirty,
                    "diff": difference}

    # ----- draft editing ----------------------------------------------------
    def _save_draft(self, draft):
        draft["updated_at"] = _now()
        _atomic_write(self.directory / "draft.json", _encode(draft))
        return draft

    def upsert_card(self, proposal, *, author):
        with self.lock:
            draft = self.draft()
            catalog = draft["catalog"]
            cards = catalog["cards"]
            existing = next((card for card in cards if card["id"] == (proposal or {}).get("id")), None)
            replacing = (proposal or {}).get("replaces")
            if replacing is None and existing is not None:
                raise KBValidationError([{"field": "id", "message": "该 ID 已被其他卡片使用"}])
            if replacing is not None and replacing != (proposal or {}).get("id"):
                raise KBValidationError([{"field": "id", "message": "已有卡片不能改 ID，请新建卡片后删除旧卡"}])
            card = normalize_card(proposal, evidence=self.evidence_for(catalog), existing_card=existing)
            card["kb"]["updated_by"] = author
            if existing is not None and existing.get("kb", {}).get("origin") == "baseline" and not card_changed(existing, card):
                card["kb"] = existing["kb"]
            if existing is None:
                cards.append(card)
            else:
                cards[cards.index(existing)] = card
            self._check_draft_runtime(catalog)
            self._save_draft(draft)
            return card

    def check_card(self, proposal):
        """Dry run of ``upsert_card``: same checks, nothing written."""
        with self.lock:
            catalog = self.draft()["catalog"]
            existing = next((card for card in catalog["cards"] if card["id"] == (proposal or {}).get("id")), None)
            replacing = (proposal or {}).get("replaces")
            if replacing is None and existing is not None:
                raise KBValidationError([{"field": "id", "message": "该 ID 已被其他卡片使用"}])
            if replacing is not None and replacing != (proposal or {}).get("id"):
                raise KBValidationError([{"field": "id", "message": "已有卡片不能改 ID，请新建卡片后删除旧卡"}])
            return normalize_card(proposal, evidence=self.evidence_for(catalog), existing_card=existing)

    def delete_card(self, card_id):
        with self.lock:
            draft = self.draft()
            cards = draft["catalog"]["cards"]
            remaining = [card for card in cards if card["id"] != card_id]
            if len(remaining) == len(cards):
                raise KeyError("KB_CARD_NOT_FOUND")
            if not remaining:
                raise KBValidationError([{"field": "cards", "message": "知识库至少保留一张卡片"}])
            draft["catalog"]["cards"] = remaining
            self._save_draft(draft)
            return {"deleted": card_id}

    def add_upload(self, *, title, text, url="", author):
        with self.lock:
            draft = self.draft()
            catalog = draft["catalog"]
            documents = catalog.setdefault("supplementary_sources", [])
            document = build_uploaded_document(title, text, url=url,
                                               existing_ids=[doc["document_id"] for doc in documents])
            document["added_by"] = author
            documents.append(document)
            self._check_draft_runtime(catalog)
            self._save_draft(draft)
            return document

    def delete_upload(self, document_id):
        with self.lock:
            draft = self.draft()
            catalog = draft["catalog"]
            documents = catalog.get("supplementary_sources", [])
            target = next((doc for doc in documents if doc["document_id"] == document_id), None)
            if target is None:
                raise KeyError("KB_SOURCE_NOT_FOUND")
            prefix = f"upload:{document_id}#"
            users = [card["id"] for card in catalog["cards"]
                     if any(identifier.startswith(prefix) for identifier in card["evidence_ids"])]
            if users:
                raise KBValidationError([{"field": "document", "message": "仍有卡片引用该文档：" + "、".join(users)}])
            catalog["supplementary_sources"] = [doc for doc in documents if doc is not target]
            if not catalog["supplementary_sources"]:
                catalog.pop("supplementary_sources")
            self._save_draft(draft)
            return {"deleted": document_id}

    def set_publishers(self, names):
        """B5g (D8): the draft's ``source_publishers``, the names G10 keeps out of the answer's words, checked with the
        runtime loader's own rule (catalog.publisher_problems); an empty list turns the guard off."""
        with self.lock:
            draft = self.draft()
            cleaned = [name.strip() if type(name) is str else name for name in names] if type(names) is list else names
            problems = publisher_problems({**draft["catalog"], "source_publishers": cleaned})
            if problems:
                raise KBValidationError([{"field": field, "message": message} for field, message in problems])
            draft["catalog"]["source_publishers"] = cleaned
            self._save_draft(draft)
            return cleaned

    def discard_draft(self):
        with self.lock:
            path = self.directory / "draft.json"
            if path.exists():
                path.unlink()
            return self.draft_status()

    def _check_draft_runtime(self, catalog):
        try:
            self.runtime_check_bytes(_encode(catalog))
        except ValueError as exc:
            raise KBValidationError([{"field": "catalog", "message": "运行时校验未通过：" + str(exc)}]) from None

    def validate_draft(self):
        with self.lock:
            catalog = self.draft()["catalog"]
            evidence = self.evidence_for(catalog)
            problems = []
            for card in catalog["cards"]:
                if card.get("kb", {}).get("origin") == "baseline":
                    continue
                try:
                    normalize_card(card, evidence=evidence, existing_card=card)
                except KBValidationError as exc:
                    problems.extend({"card_id": card["id"], **error} for error in exc.errors)
            runtime = "passed"
            try:
                self.runtime_check_bytes(_encode(catalog))
            except ValueError as exc:
                runtime = str(exc)
            return {"ok": not problems and runtime == "passed", "card_errors": problems,
                    "runtime_check": runtime, "card_count": len(catalog["cards"]),
                    "scope": "source_binding_and_verbatim_quotes_not_policy_correctness"}

    # ----- publishing -------------------------------------------------------
    def publish(self, *, label, author):
        if type(label) is not str or not label.strip() or len(label) > 80:
            raise KBValidationError([{"field": "label", "message": "请填写 1–80 字的版本说明"}])
        with self.lock:
            report = self.validate_draft()
            if not report["ok"]:
                raise KBValidationError([{"field": e.get("field", "catalog"),
                                          "message": f"{e.get('card_id', '')} {e['message']}".strip()}
                                         for e in report["card_errors"]] or
                                        [{"field": "catalog", "message": report["runtime_check"]}])
            draft = self.draft()
            catalog = draft["catalog"]
            row = self._store_version(catalog, label=label.strip(), author=author,
                                      parent=self._active["version_id"])
            pointer = self._activate(row["version_id"], reason=label.strip(), author=author, operation="publish")
            path = self.directory / "draft.json"
            if path.exists():
                path.unlink()
            return {"version": row, "active": pointer}

    def activate(self, version_id, *, reason, author):
        if type(reason) is not str or not reason.strip() or len(reason) > 200:
            raise KBValidationError([{"field": "reason", "message": "请说明切换原因（1–200 字）"}])
        with self.lock:
            raw = self._version_path(version_id).read_bytes()
            self.runtime_check_bytes(raw)
            operation = "rollback" if version_id != self._active["version_id"] else "reactivate"
            return self._activate(version_id, reason=reason.strip(), author=author, operation=operation)

    def diff(self, version_id, against=None):
        with self.lock:
            index = self._index()["versions"]
            row = next((item for item in index if item["version_id"] == version_id), None)
            if row is None:
                raise KeyError("KB_VERSION_NOT_FOUND")
            base_id = against or row.get("parent_version_id")
            before = self.load_version(base_id) if base_id else {"cards": []}
            return {"version_id": version_id, "against": base_id,
                    "diff": diff_catalogs(before, self.load_version(version_id))}
