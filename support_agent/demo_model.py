"""Offline keyword script standing in for the LLM in product demos.

It lets someone without an API key click through the real product: the same agent turn, backend quote checks, SQLite
business state, the card catalogue, program-rendered rules and independent confirmation all run unchanged. Only the two
model proposals come from simple keyword rules here; a transport and UI demo, not language understanding, labelled
with its mode. The plan (B7: ``plan_turn`` items) names every order id the message types (a return targets each; a
withdrawal word drops an open card, a pause word keeps it, K21; the open cards come from the planning context; B7r: a
withdrawal clause beside a return word's clause is a switch, withdraw plus return_request, and a return word with a
pause word and no open card is a return wanted later, subkind later; B8: a person, a complaint, a danger word adds
a handoff_request, complaint or safety item). The
answer is ``respond`` over the turn items: a business item is handled by the system, a
question cites the cards that share the most wording with it (B6: from the whole catalogue the answer context carries)
or is a gap with no path, the own orders the message names are shown, and no words are written (B5f: the code's line
stands in for a gap; B5g: the code gives the gap its domain's verification path, as for any answer leaving a gap or
refusal without one). Like a strict provider, it reads only its call's context and offers only the fields that call's
schema offers.
"""
from __future__ import annotations

import json
import re

from .models import ModelResponse, ToolCall
from .domain import match_product_reference
from .respond import ANSWER_KINDS
from .understand import PLAN_TURN

_ORDER = re.compile(r"(?<![A-Za-z0-9_-])O\d{4}(?![A-Za-z0-9_-])")
_RETURN_INTENT = ("申请退货", "我要退货", "要退货", "帮我退货", "想退货", "办理退货", "帮我退", "我要退", "退了吧", "改退")
_LIST_INTENT = ("我的订单", "最近几单", "最近的订单", "哪几单", "哪些能退", "有能退的", "几笔订单", "全部订单",
                "所有订单", "我买过")
# Not a list of one's own simulated orders: real accounts, other people, shipping.
_NOT_OWN_LIST = ("真实", "京东账户", "京东账号", "别的账号", "app", "APP", "他的", "她的", "他买", "她买", "老公",
                 "老婆", "妈", "爸", "朋友", "别人", "家人", "尾号", "手机号", "淘宝", "拼多多", "物流", "到哪", "发货",
                 "快递", "退款", "到账", "发票", "地址", "催", "价保", "换货", "维修", "保修")
_ANY_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z]\d{4}(?![A-Za-z0-9_-])")
_ELIGIBILITY_HINTS = ("能退", "可以退", "能不能退", "拆封", "签收")
_CLAUSE_BREAK = re.compile(r"[\s，,。．.！!？?；;、：:]+")
_SENTENCE_BREAK = re.compile(r"[，,。；;！!？?]+")
# B7 (K21): only a withdrawal word drops an open card; a pause word keeps it and is chit-chat (the planner's reading).
_WITHDRAW_INTENT = ("不退了", "不要退了", "作废", "撤回", "取消这张")
_PAUSE_INTENT = ("先不要提交", "先别提交", "暂停办理", "暂停")
# B8: asking for a person, complaining, reporting danger (the planner's kinds; code decides the ticket and the line).
_HANDOFF_INTENT = ("转人工", "人工客服", "找人工", "真人")
_COMPLAINT_INTENT = ("投诉", "举报")
_MONEY_WORDS = ("扣", "少退", "退款", "多收")
_SAFETY_INTENT = {"product_hazard": ("冒烟", "起火", "着火", "烧焦", "漏电", "鼓包"),
                  "medical_emergency": ("受伤", "流血", "烫伤", "晕倒"),
                  "fraud_or_theft": ("验证码", "被骗", "盗刷")}
_POLICY_HINTS = ("赠品", "运费", "邮费", "七天", "7天", "无理由", "保修", "质保", "维修", "换货", "退换",
                 "上门取件", "自寄", "寄回", "补偿", "政策", "规则", "能退", "可以退", "怎么退", "期限",
                 "激活", "拆封", "防伪", "签收")
_QUESTION_MARKS = ("？", "?", "吗", "怎么", "多少", "是否", "哪", "如何", "能不能", "可不可以")


def _item(kind, quote, *, order_quote="", order_id="none", fee_subject="none", subkind="none"):
    return {"kind": kind, "subkind": subkind, "quote": quote, "history_index": None, "order_quote": order_quote,
            "order_id": order_id, "fee_subject": fee_subject}


def _questions(message, fee_subject):
    parts = [part for part in re.split(r"(?<=[？?])", message) if part.strip()]
    questions = []
    for part in parts:
        quote = part.strip()
        if quote and quote in message and any(mark in quote for mark in _QUESTION_MARKS):
            questions.append(quote)
    if not questions:
        questions = [message.strip()]
    return [_item("policy_question", quote, fee_subject=fee_subject) for quote in questions[:3]]


def _product_mention(message, catalog):
    """The first clause of the message that names an own product, and the order it may mean.

    The clause is quoted as is; when it fits several orders the backend asks which.
    """
    fallback = (None, None)
    for clause in _CLAUSE_BREAK.split(message):
        matched = match_product_reference(clause, catalog)["matched"] if clause else []
        if len(matched) == 1:
            return clause, matched[0]
        if matched and fallback == (None, None):
            fallback = (clause, matched[0])
    return fallback


def plan_for(context):
    message = context["current_message"]
    session = context.get("session_cards") or {}
    open_cards = session.get("open_card_order_ids") or []
    catalog = context.get("account_orders") or []
    orders = list(dict.fromkeys(_ORDER.findall(message)))
    own = {row["order_id"] for row in catalog}
    # Any other id-like token (o1001, A1234) is left to the backend's literal checks.
    other_ids = [token for token in _ANY_ORDER_TOKEN.findall(message) if token not in orders]
    mention, mentioned = (None, None) if orders or other_ids else _product_mention(message, catalog)
    listing = (None if orders or other_ids or mention or any(word in message for word in _NOT_OWN_LIST)
               else next((word for word in _LIST_INTENT if word in message), None))
    intent = next((word for word in _RETURN_INTENT if word in message), None)
    withdraw = next((word for word in _WITHDRAW_INTENT if word in message), None)
    pause = next((word for word in _PAUSE_INTENT if word in message), None)
    history = context.get("history") or []
    earlier_users = [row.get("content", "") for row in history if row.get("role") == "user"][-2:]
    last_assistant = next((row.get("content", "") for row in reversed(history)
                           if row.get("role") == "assistant"), "")
    # A reply that only names the order continues the return the user asked for
    # in the previous turn; the backend still checks everything.
    continuing = ("退货" in last_assistant and
                  any(any(word in text for word in _RETURN_INTENT) for text in earlier_users))
    collecting = continuing or bool(open_cards)
    # A question about an order ("收纳盒那单能退吗") is a read, even while a return is open.
    asks = any(mark in message for mark in _QUESTION_MARKS)

    def order(order_id):
        # The enum holds the account's own ids only: another id stays in the words (order_quote) with "none".
        return order_id if order_id in own else "none"

    items = []
    if withdraw and (open_cards or session.get("submitted_order_ids")):
        # A switch: a clause with a return word and no withdrawal word asks for its own orders; the rest are dropped.
        asking = [clause for clause in _SENTENCE_BREAK.split(message) if any(word in clause for word in _RETURN_INTENT)
                  and not any(word in clause for word in _WITHDRAW_INTENT)]
        dropped = [o for o in orders if not any(o in clause for clause in asking)]
        # Without a typed id, the product words outside the asking clauses name the card, as for a return (B7r).
        named = mention if mention and not any(mention in clause for clause in asking) else None
        items += [_item("withdraw", withdraw, order_quote=o, order_id=order(o)) for o in dropped] or [
            _item("withdraw", withdraw, order_quote=named or "", order_id=mentioned if named else "none")]
        for clause in asking:
            word = next(word for word in _RETURN_INTENT if word in clause)
            items += [_item("return_request", word, order_quote=o, order_id=order(o)) for o in orders
                      if o in clause] or [_item("return_request", word)]
    elif pause and open_cards and not intent:
        items.append(_item("chit_chat", pause))
    elif intent or (collecting and (orders or (mention and not asks))):
        # Every order id the message types is a target (B3c: the backend prepares a card for each eligible one); with a
        # pause word the user wants it later (B7r: no card).
        quote, subkind = intent or (orders[0] if orders else mention), "later" if pause else "none"
        items += [_item("return_request", quote, order_quote=o, order_id=order(o), subkind=subkind)
                  for o in orders] or [_item("return_request", quote, order_quote=mention or "",
                                             order_id=mentioned or "none", subkind=subkind)]
    elif orders:
        items += [_item("own_order_status", o, order_quote=o, order_id=order(o)) for o in orders]
    elif mention:
        items.append(_item("own_order_status", mention, order_quote=mention, order_id=mentioned))
    elif listing:
        items.append(_item("own_order_status", listing, order_quote=listing, order_id="all_mine"))
    word = next((word for word in _HANDOFF_INTENT if word in message), None)
    if word:
        items.append(_item("handoff_request", word))
    word = next((word for word in _COMPLAINT_INTENT if word in message), None)
    if word:
        items.append(_item("complaint", word,
                           subkind="money_dispute" if any(money in message for money in _MONEY_WORDS) else "none"))
    for subkind, words in _SAFETY_INTENT.items():
        word = next((word for word in words if word in message), None)
        if word:
            items.append(_item("safety", word, subkind=subkind))
            break
    hints = [hint for hint in _POLICY_HINTS if hint in message]
    if (orders or mention or listing) and all(hint in _ELIGIBILITY_HINTS for hint in hints):
        # "Can my own order be returned" is answered by the return rule on the
        # looked-up order, not by a policy question.
        hints = []
    policy = bool(hints) and (not items or any(mark in message for mark in _QUESTION_MARKS))
    subjects, fee_flow = [], "unknown"
    if policy:
        if any(word in message for word in ("运费", "邮费", "补偿", "取件费")):
            if any(word in message for word in ("维修", "寄修", "保修")):
                subjects.append("repair_shipping")
            elif any(word in message for word in ("退", "寄回")):
                subjects.append("return_shipping")
        if any(word in message for word in ("自寄", "自行寄回", "自己寄")):
            fee_flow = "return_self_ship"
        elif "上门取件" in message:
            fee_flow = "return_home_pickup"
        unknown = any(word in message for word in ("运费", "邮费")) and not subjects
        items += _questions(message, subjects[0] if subjects else "unknown" if unknown else "none")
    return {"goal": message[:600], "items": items[:8], "order_stage": "unknown",
            "fee_flow": fee_flow if subjects == ["return_shipping"] else "unknown"}


def _bigrams(text):
    text = re.sub(r"\s+", "", text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _named_own_orders(context):
    """The own orders (answer context, B5b) whose id or product name the current message names."""
    orders = (context.get("verified") or {}).get("customer_orders") or []
    message = (context.get("conversation") or {}).get("current_user_message", "")
    named = set(_ORDER.findall(message)) or set(match_product_reference(message, orders)["matched"])
    return [row["order_id"] for row in orders if row["order_id"] in named]


def _claims_for(quote, cards):
    """Up to two cards that share the most wording with the quote, and each card's best outcome."""
    wanted = _bigrams(quote)
    scored = sorted(((len(wanted & _bigrams(" ".join([card["title"], card["scope"], *card["outcomes"]]))), card)
                     for card in cards), key=lambda row: -row[0])
    top = scored[0][0] if scored else 0
    # A second card must be nearly as relevant as the first to be shown.
    chosen = [card for score, card in scored if score > 0 and score >= 0.75 * top][:2]
    claims = []
    for card in chosen:
        options = [index for index, premises in enumerate(card["outcome_premises"]) if premises]
        best = max(options, key=lambda index: len(wanted & _bigrams(card["outcomes"][index])))
        claims.append({"card_id": card["id"], "outcome_index": best})
    return claims


def answer_for(context):
    """A ``respond`` result: every turn item once; no words and no follow-up (the code renders the rest). Chit-chat
    cites no card (B7)."""
    cards = [card for card in context.get("policy_cards") or []
             if any(premises for premises in card.get("outcome_premises", []))]
    items, total = [], 0
    for item in context.get("turn_items") or []:
        if item["kind"] not in ANSWER_KINDS:
            items.append({"index": item["index"], "disposition": "handled_by_system", "claims": [], "path_id": "none",
                          "note": ""})
            continue
        if item["kind"] == "chit_chat":
            items.append({"index": item["index"], "disposition": "answered", "claims": [], "path_id": "none",
                          "note": ""})
            continue
        claims = _claims_for(item["quote"], cards)[:max(0, 6 - total)]
        total += len(claims)
        items.append({"index": item["index"], "disposition": "answered" if claims else "gap", "claims": claims,
                      "path_id": "none", "note": ""})
    return {"items": items, "show_order_ids": _named_own_orders(context), "next_step": "",
            "ask": {"text": "", "option_set_id": "none"}}


def _offered(value, tools):
    """Only the fields this call's schema offers (B5a): a list with nothing to choose is not offered."""
    parameters = tools[0]["function"]["parameters"]
    offered = parameters["properties"]
    value = {key: item for key, item in value.items() if key in offered}
    if "items" in value and "claims" not in offered["items"]["items"]["properties"]:
        value["items"] = [{key: item for key, item in row.items() if key != "claims"} for row in value["items"]]
    return value


class KeywordDemoModel:
    """Deterministic keyword stand-in; responses are labelled offline demo."""
    mode = "offline_keyword_demo"

    def complete(self, messages, tools):
        return self.complete_with_timeout(messages, tools, timeout=None)

    def complete_with_timeout(self, messages, tools, *, timeout):
        name = tools[0]["function"]["name"]
        context = json.loads(messages[1]["content"])
        value = _offered(plan_for(context) if name == PLAN_TURN else answer_for(context), tools)
        return ModelResponse(tool_calls=[ToolCall("offline-" + name, name, value)],
                             model_id="offline-keyword-demo",
                             usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class DemoModel:
    """Offline transport demonstration; scripted outputs are never model quality."""
    mode = 'offline_scripted_demo'

    def complete(self, messages, tools):
        name = tools[0]['function']['name']
        if name == PLAN_TURN:
            value = {'goal': '离线流程演示', 'items': [], 'order_stage': 'unknown', 'fee_flow': 'unknown'}
        else:
            # Every item asks the one open question (the follow-up), nothing else.
            items = json.loads(messages[1]['content']).get('turn_items') or []
            value = {'items': [{'index': item['index'], 'disposition': 'asked' if item['kind'] in ANSWER_KINDS
                                else 'handled_by_system', 'claims': [], 'path_id': 'none', 'note': ''} for item in items],
                     'show_order_ids': [], 'next_step': '',
                     'ask': {'text': '你现在遇到的具体问题是什么？', 'option_set_id': 'none'}}
        return ModelResponse(tool_calls=[ToolCall('offline-' + name, name, _offered(value, tools))],
                             model_id='offline-graph-script')
