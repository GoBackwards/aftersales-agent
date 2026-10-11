"""Product-facing HTTP routes: demo sign-in, customer workspace and knowledge base.

These routes sit beside the original session API and reuse its guarantees:
loopback-only binding, exact same-origin checks, Bearer identities resolved on
the server, strict JSON bodies and no model calls on read paths. Demo sign-in
exists only when the server is started with it; knowledge-base routes need the
separate administrator identity and never accept a customer token.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .domain import SCENARIO_DATE, order_view
from .assemble import eligibility_text
from .models import LazyModel

ADMIN_IDENTITY = "kb-admin"
# The one statement of the demo environment (B1, decisions D8 and D13): the chat page shows it in a
# persistent notice and on the sign-in page; replies and other copy never repeat it. B4a names the human-service
# tickets in it: the ticket replies state the record and its status, and this notice says nobody follows it up (KF2).
# B4d: it lists only data the page shows (B4b deleted the per-failure record, so nothing on the page is one).
ENVIRONMENT_NOTICE = (
    "这里是自动回复的售后助手，能查本账号订单、讲解售后规则、帮你准备退货申请，不能直接退款，也动不了其他平台上的订单；"
    "申请只在你点按确认卡的确认按钮后提交，聊天里说的话不能代替这一步。"
    "现在是演示环境：账号、订单、退货申请和人工工单都是模拟数据，不会发生真实交易，也不会有真人跟进或联系你；"
    f"演示业务日期是 {SCENARIO_DATE.isoformat()}。政策内容取自保存的公开网页快照，生效日期未核实。")
DEMO_PROFILES = {
    "alice": {"display_name": "Alice", "subtitle": "买家 · 4 笔订单", "role": "customer", "color": "#2f6f5e"},
    "bob": {"display_name": "Bob", "subtitle": "买家 · 1 笔订单", "role": "customer", "color": "#6b4fa0"},
    ADMIN_IDENTITY: {"display_name": "知识库管理员", "subtitle": "维护政策卡片与来源文档", "role": "admin", "color": "#9a5b13"},
}
_SCOPES = {"draft", "active"}
# B5g (D8): one line for the administrator when the published catalogue names no source publishers, so G10 is off.
PUBLISHER_WARNING = "生效版本没有登记来源发布方，答复文字点名发布方的检查（G10）没有启用；在“来源发布方”里登记后发布即可。"
# B6 (plan K2, D15): the knowledge base previews what the answer model reads, the whole catalogue, instead of a search.
MODEL_VIEW_NOTE = "答复模型每一轮读到的就是这份整份目录，不再按问题检索；这里只核对篇幅是否在上限内，不说明哪张卡适用或写得对。"


class ProductHTTPError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def _require(condition, status, code, message):
    if not condition:
        raise ProductHTTPError(status, code, message)


def _fields(body, allowed, required=()):
    _require(type(body) is dict and set(required) <= set(body) and set(body) <= set(allowed),
             400, "INVALID_ARGUMENTS", "请求字段缺失或包含未允许字段。")


def _text(value, name, *, limit, allow_empty=False):
    _require(type(value) is str and len(value) <= limit and (allow_empty or value.strip()),
             400, "INVALID_ARGUMENTS", f"{name} 必须为 1 至 {limit} 字的文本。")
    return value


def split_path(raw_path):
    parts = urlsplit(raw_path)
    query = {key: values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
    return parts.path, query


def is_product_path(path):
    return path.startswith(("/demo/", "/me/", "/kb/")) or path in {"/demo", "/me", "/kb"}


# ----- public demo sign-in --------------------------------------------------
def demo_identities(server):
    _require(server.demo_login, 404, "NOT_FOUND", "接口不存在。")
    identities = []
    for identity in sorted(set(server.credentials.values())):
        profile = DEMO_PROFILES.get(identity, {"display_name": identity, "subtitle": "买家",
                                               "role": "customer", "color": "#44546a"})
        identities.append({"identity": identity, **profile})
    if server.kb is not None:
        identities.append({"identity": ADMIN_IDENTITY, **DEMO_PROFILES[ADMIN_IDENTITY]})
    return 200, {"identities": identities, "mode": server.client_mode,
                 "knowledge_base": server.kb is not None,
                 "knowledge_release": (server.public_policy_config.release_metadata()
                                       if server.public_policy_config is not None else None),
                 "notice": ENVIRONMENT_NOTICE}


def demo_login(server, body):
    _require(server.demo_login, 404, "NOT_FOUND", "接口不存在。")
    _fields(body, {"identity"}, {"identity"})
    identity = body["identity"]
    if identity == ADMIN_IDENTITY and server.kb is not None:
        token = next(iter(server.admin_tokens))
    else:
        token = next((token for token, customer in server.credentials.items() if customer == identity), None)
    _require(token is not None, 404, "IDENTITY_NOT_FOUND", "没有这个演示身份。")
    profile = DEMO_PROFILES.get(identity, {"display_name": identity, "role": "customer"})
    return 200, {"token": token, "identity": identity, "role": profile["role"],
                 "display_name": profile["display_name"]}


# ----- customer workspace ---------------------------------------------------
def list_sessions(store, customer_id):
    rows = store._connection.execute("""
        SELECT s.session_id, s.created_at, s.last_reply, s.busy_run_id,
          (SELECT payload FROM session_requests r WHERE r.session_id = s.session_id AND r.entry = 'turn'
             ORDER BY rowid ASC LIMIT 1) AS first_payload,
          (SELECT COUNT(*) FROM session_requests r WHERE r.session_id = s.session_id AND r.entry = 'turn') AS turns,
          (SELECT MAX(created_at) FROM session_messages m WHERE m.session_id = s.session_id) AS updated_at
        FROM sessions s
        WHERE s.customer_id = ? ORDER BY COALESCE(updated_at, s.created_at) DESC LIMIT 60""",
        (customer_id,)).fetchall()
    sessions = []
    for row in rows:
        first = json.loads(row["first_payload"]).get("user_message", "") if row["first_payload"] else ""
        title = re.sub(r"\s+", " ", first).strip()
        # B3a: the session's cards replace the task status in the list. B3c: a session holds several cards and
        # goes on after a submission, so the list reads it in the page header's order: an open card, else the
        # request a click in this session submitted last (with that request's order), else the latest card.
        open_cards = store._open_cards(row["session_id"])
        submitted = store._session_return_request(customer_id, row["session_id"])
        if open_cards:
            card = open_cards[-1]
        elif submitted:
            card = {"status": "consumed", "order_id": submitted["order_id"]}
        else:
            card = store._card_view(store._latest_card(row["session_id"])) or {}
        sessions.append({"session_id": row["session_id"], "title": (title[:28] + "…") if len(title) > 28 else title,
                         "preview": re.sub(r"\s+", " ", row["last_reply"] or "")[:60],
                         "created_at": row["created_at"], "updated_at": row["updated_at"] or row["created_at"],
                         "turns": row["turns"], "busy": row["busy_run_id"] is not None,
                         "card_status": card.get("status"), "order_id": card.get("order_id")})
    return {"sessions": sessions}


def list_orders(store, customer_id, *, shipping_rule=None):
    returns = store.owned_return_requests(customer_id)
    records = store.owned_orders(customer_id)
    # eligibility_text is a display field of this response only, worded by the chat reply's own code sentences
    # (assemble.eligibility_text). A card's facts and reasons come with the card's own declaration (B3e).
    orders = [order_view(order, returns.get(order.order_id)) for order in records]
    return {"orders": [{**order, "eligibility_text": eligibility_text(order)} for order in orders],
            "scenario_date": SCENARIO_DATE.isoformat(), "notice": ENVIRONMENT_NOTICE,
            # The expandable return-shipping rule under a card (D5), rendered by code.
            "return_shipping_rule": shipping_rule}


def return_shipping_rule(server):
    """The return-shipping rule card of the running catalogue, every branch with its premises, or None.

    Shown under the confirmation card as it is written (decision D5); it does not change with the reason
    the user picks and decides nothing about the order. None without a pinned knowledge release.
    """
    if server.public_policy_config is None:
        return None
    from .catalog import CATALOG_PATH, claim_premises
    path = server.kb.active_catalog_path() if server.kb is not None else CATALOG_PATH
    try:
        catalog = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    card = next((card for card in catalog.get("cards", [])
                 if card.get("effect_domain") == "return_shipping_responsibility"), None)
    if card is None:
        return None
    branches = []
    for index, outcome in enumerate(card.get("outcomes", [])):
        premises = claim_premises(card, index) or []
        branches.append({"outcome": outcome, "premises": [part for part in premises if part.strip()]})
    return {"card_id": card["id"], "title": card["title"], "branches": branches,
            "exceptions": list(card.get("exceptions", []))}


# ----- knowledge base -------------------------------------------------------
def _scope(query):
    scope = query.get("scope", "active")
    _require(scope in _SCOPES, 400, "INVALID_ARGUMENTS", "scope 只能是 draft 或 active。")
    return scope


def _catalog(kb, scope):
    return kb.draft()["catalog"] if scope == "draft" else kb.active_catalog()


def _card_summary(card):
    meta = card.get("kb", {})
    return {"id": card["id"], "title": card["title"], "effect_domain": card.get("effect_domain", "unclassified"),
            "scope": card["scope"], "outcome_count": len(card["outcomes"]),
            "reference_count": len(card["references"]), "origin": meta.get("origin", "baseline"),
            "updated_at": meta.get("updated_at"),
            "uses_upload": any(identifier.startswith("upload:") for identifier in card["evidence_ids"])}


def _source_meta(identifier, item):
    return {"evidence_id": identifier, "title": item["title"], "url": item.get("source_url", ""),
            "document_id": item["document_id"], "sha256": item["sha256"], "chars": len(item["text"]),
            "kind": ("merchant_upload" if identifier.startswith("upload:")
                     else "authored_rule" if identifier.startswith("rule:") else "official_snapshot"),
            "coverage": item.get("coverage"), "scope_note": item.get("scope_note", "")}


def _resolved_card(kb, card, evidence):
    references = []
    for ref in card["references"]:
        item = evidence.get(ref["evidence_id"])
        references.append({**ref, "source": _source_meta(ref["evidence_id"], item) if item else None,
                           "text": item["text"] if item else None,
                           "quotes_found": [bool(item) and quote in item["text"] for quote in ref["quotes"]]})
    premises = card.get("outcome_premises")
    if premises is None:
        premises = [[card["scope"], *card["conditions"]]] if len(card["outcomes"]) == 1 else [[] for _ in card["outcomes"]]
    return {**card, "outcome_premises": premises, "premises_authored": "outcome_premises" in card,
            "references": references}


def _model_view(catalog):
    """B6: the compact model view the answer context carries for this catalogue, and its size against the cap (D15)."""
    from .catalog import MAX_CATALOGUE_CARDS, MAX_MODEL_VIEW_CHARS, cap_problem, model_view, model_view_chars
    cards = catalog["cards"]
    return {"card_count": len(cards), "chars": model_view_chars(cards),
            "limits": {"cards": MAX_CATALOGUE_CARDS, "chars": MAX_MODEL_VIEW_CHARS},
            "over_cap": cap_problem(cards), "view": model_view(cards), "note": MODEL_VIEW_NOTE}


def kb_dispatch(handler, method, path, query, body):
    """Route one administrator request; raises ProductHTTPError on bad input."""
    from .catalog import SOURCE_KINDS
    from .kb_store import EFFECT_DOMAINS, KBValidationError
    server = handler.server
    kb = server.kb
    _require(kb is not None, 404, "KB_DISABLED", "当前服务没有启用知识库管理。")
    author = handler.customer_id
    try:
        if method == "GET" and path == "/kb/overview":
            pointer = kb.active_pointer()
            versions = kb.versions()
            active_row = next((row for row in versions if row["active"]), None)
            status = kb.draft_status()
            publishers = {"active": kb.active_catalog().get("source_publishers", []),
                          "draft": kb.draft()["catalog"].get("source_publishers", [])}
            return 200, {"active": pointer, "active_version": active_row, "draft": status,
                         "source_publishers": publishers,
                         "publisher_warning": "" if publishers["active"] else PUBLISHER_WARNING,
                         "draft_card_count": len(kb.draft()["catalog"]["cards"]),
                         "version_count": len(versions), "effect_domains": EFFECT_DOMAINS,
                         "source_kinds": SOURCE_KINDS,
                         "mode": server.client_mode, "ai_drafting": "model" if server.client_mode == "live_agent" else "template",
                         "knowledge_release": server.public_policy_config.release_metadata(),
                         "scope_notice": "校验只证明来源绑定与逐字引用，不证明规则正确或现行有效。"}
        if method == "GET" and path == "/kb/cards":
            scope = _scope(query)
            catalog = _catalog(kb, scope)
            return 200, {"scope": scope, "cards": [_card_summary(card) for card in catalog["cards"]],
                         "limitations": catalog.get("limitations", [])}
        match = re.fullmatch(r"/kb/cards/([a-z][a-z0-9_]{1,63})", path)
        if method == "GET" and match:
            scope = _scope(query)
            catalog = _catalog(kb, scope)
            card = next((card for card in catalog["cards"] if card["id"] == match[1]), None)
            _require(card is not None, 404, "KB_CARD_NOT_FOUND", "卡片不存在。")
            return 200, {"scope": scope, "card": _resolved_card(kb, card, kb.evidence_for(catalog))}
        if method == "POST" and path == "/kb/draft/cards":
            _fields(body, {"card"}, {"card"})
            return 200, {"card": kb.upsert_card(body["card"], author=author), "draft": kb.draft_status()}
        if method == "POST" and path == "/kb/draft/cards/check":
            _fields(body, {"card"}, {"card"})
            return 200, {"ok": True, "card": kb.check_card(body["card"])}
        match = re.fullmatch(r"/kb/draft/cards/([a-z][a-z0-9_]{1,63})/delete", path)
        if method == "POST" and match:
            _fields(body, set())
            return 200, {**kb.delete_card(match[1]), "draft": kb.draft_status()}
        if method == "POST" and path == "/kb/draft/publishers":
            _fields(body, {"source_publishers"}, {"source_publishers"})
            return 200, {"source_publishers": kb.set_publishers(body["source_publishers"]), "draft": kb.draft_status()}
        if method == "POST" and path == "/kb/draft/validate":
            _fields(body, set())
            return 200, kb.validate_draft()
        if method == "POST" and path == "/kb/draft/discard":
            _fields(body, set())
            return 200, {"draft": kb.discard_draft()}
        if method == "POST" and path == "/kb/draft/publish":
            _fields(body, {"label"}, {"label"})
            return 200, kb.publish(label=body["label"], author=author)
        if method == "GET" and path == "/kb/versions":
            return 200, {"versions": kb.versions(), "history": kb.history()}
        match = re.fullmatch(r"/kb/versions/([0-9a-f]{64})/diff", path)
        if method == "GET" and match:
            return 200, kb.diff(match[1], query.get("against") or None)
        match = re.fullmatch(r"/kb/versions/([0-9a-f]{64})/activate", path)
        if method == "POST" and match:
            _fields(body, {"reason"}, {"reason"})
            return 200, {"active": kb.activate(match[1], reason=body["reason"], author=author)}
        if method == "GET" and path == "/kb/sources":
            scope = _scope(query)
            catalog = _catalog(kb, scope)
            evidence = kb.evidence_for(catalog)
            documents = {}
            for identifier, item in evidence.items():
                if identifier.startswith("rule:"):
                    continue
                meta = _source_meta(identifier, item)
                document = documents.setdefault(item["document_id"], {
                    "document_id": item["document_id"], "title": item["title"].split(" · ")[0],
                    "url": meta["url"], "kind": meta["kind"], "sections": []})
                document["sections"].append(meta)
            uploads = {doc["document_id"]: doc for doc in catalog.get("supplementary_sources", [])}
            for document in documents.values():
                upload = uploads.get(document["document_id"])
                if upload:
                    document.update(added_at=upload.get("added_at"), added_by=upload.get("added_by"))
                    document["title"] = upload["title"]
            ordered = sorted(documents.values(), key=lambda doc: (doc["kind"] != "merchant_upload", doc["document_id"]))
            return 200, {"scope": scope, "documents": ordered}
        match = re.fullmatch(r"/kb/sources/(.+)", path)
        if method == "GET" and match:
            scope = _scope(query)
            identifier = unquote(match[1])
            catalog = _catalog(kb, scope)
            item = kb.evidence_for(catalog).get(identifier)
            _require(item is not None, 404, "KB_SOURCE_NOT_FOUND", "来源段落不存在。")
            users = [card["id"] for card in catalog["cards"] if identifier in card["evidence_ids"]]
            return 200, {"source": {**_source_meta(identifier, item), "text": item["text"]}, "cited_by": users}
        if method == "POST" and path == "/kb/uploads":
            _fields(body, {"title", "text", "url"}, {"title", "text"})
            document = kb.add_upload(title=_text(body["title"], "title", limit=200),
                                     text=_text(body["text"], "text", limit=200_000),
                                     url=body.get("url", ""), author=author)
            return 201, {"document": document, "draft": kb.draft_status()}
        match = re.fullmatch(r"/kb/uploads/(upload-[a-z0-9-]{1,63})/delete", path)
        if method == "POST" and match:
            _fields(body, set())
            return 200, {**kb.delete_upload(match[1]), "draft": kb.draft_status()}
        if method == "GET" and path == "/kb/model-view":
            scope = _scope(query)
            return 200, {"scope": scope, **_model_view(_catalog(kb, scope))}
        if method == "POST" and path == "/kb/ai-draft":
            _fields(body, {"evidence_ids", "instruction"}, {"evidence_ids"})
            identifiers = body["evidence_ids"]
            _require(type(identifiers) is list and 1 <= len(identifiers) <= 3
                     and all(type(value) is str for value in identifiers), 400, "INVALID_ARGUMENTS",
                     "请选择 1–3 个来源段落。")
            instruction = _text(body.get("instruction", ""), "instruction", limit=300, allow_empty=True)
            return 200, _ai_draft(server, kb, identifiers, instruction)
        raise ProductHTTPError(404, "NOT_FOUND", "接口不存在。")
    except KBValidationError as exc:
        raise ProductHTTPError(422, "KB_VALIDATION_FAILED", "知识库校验未通过。", exc.errors) from None
    except KeyError as exc:
        code = str(exc.args[0]) if exc.args else "KB_NOT_FOUND"
        raise ProductHTTPError(404, code if code.startswith("KB_") else "KB_NOT_FOUND", "请求的知识库对象不存在。") from None
    except ValueError as exc:
        raise ProductHTTPError(400, "KB_INVALID", "知识库请求无效：" + str(exc)[:200]) from None


def _ai_draft(server, kb, identifiers, instruction):
    from .kb_ai import MAX_SECTION_CHARS, model_draft, template_draft
    catalog = kb.draft()["catalog"]
    evidence = kb.evidence_for(catalog)
    sections = []
    for identifier in identifiers:
        item = evidence.get(identifier)
        _require(item is not None and not identifier.startswith("rule:"), 404, "KB_SOURCE_NOT_FOUND",
                 "来源段落不存在。")
        sections.append({"evidence_id": identifier, "title": item["title"], "text": item["text"][:MAX_SECTION_CHARS]})
    existing = {card["id"] for card in catalog["cards"]}
    if server.client_mode != "live_agent":
        return {"card": template_draft(sections, existing), "generator": "offline_template", "usage": {},
                "notice": "离线模式：用句子拆分模板生成草稿，需人工改写；启用真实模型后由大模型起草。"}
    model = LazyModel(server.model_factory)
    card, usage = model_draft(model, sections, instruction=instruction, existing_ids=existing, evidence=evidence)
    return {"card": card, "generator": "model", "usage": usage,
            "notice": "大模型起草，未保存；请逐项核对原文后再保存到草稿。"}
