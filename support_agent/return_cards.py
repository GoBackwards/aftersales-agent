"""Return confirmation cards (confirmation protocol v3, plan §4.2–§4.6; B3a, lifecycle B3b, several cards B3c).

A card is only a proposal bound to one order of one customer in one session; a session holds at most one
open card per order and may hold one for each of its orders (B3c, decision D4). The return request is
written in exactly one place, ``ReturnCards.confirm_card``, which only the click endpoint
``POST /sessions/{id}/cards/{action_id}/confirm`` calls; the chat turn can prepare, re-show or void a
card but has no path to it. The reason comes only from that click: a code from RETURN_REASONS_V1,
and for "other" a short text that is stored with the request and never written to the conversation,
a model context or a summary (the code message after a click names the preset label only, "其他"
for other). See docs/confirmation-protocol-v3.md.

Lifecycle (B3b, plan §4.4): a card changes only through a click (confirm, or "暂不提交" on
``POST /sessions/{id}/cards/{action_id}/void``), a chat withdrawal (B7: switching to another order is a withdrawal
plus a new return; a pause keeps the card, K21), the rule no longer holding at the click, or its expiry. A failed,
interrupted or cancelled turn never changes a card and nothing holds one: a click while a turn runs is
refused as BUSY by the session's busy lock. A submission does not end the session (B3c) and cannot be
undone here: a submitted card cannot be voided, and the only way back is the after-sales cancellation on
the platform (SUBMITTED_NO_UNDO).

The methods run on SessionStore (``class SessionStore(ReturnCards, Store)``) and use its connection,
transactions, leases and request cache; each one is a single short transaction.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import uuid

from .domain import RETURN_WINDOW_DAYS, SCENARIO_DATE, ToolError, last_return_date, order_rule_code
# Decision D4: no undo after a submission; the platform's after-sales cancellation is the only path. The two
# sentences live in copy.py (B5d2), which the chat reply also reads.
from .copy import CANCEL_AFTERSALES_PATH, SUBMITTED_NO_UNDO

# Server catalogue of return reasons (decision D5): neutral codes, newly written labels, sent with the
# card. The version is part of the protocol; a request stores the code (and the text for "other").
RETURN_REASONS_VERSION = "RETURN_REASONS_V1"
RETURN_REASONS_V1 = (
    ("changed_mind", "不再需要"),
    ("size_or_style", "尺码或款式不合适"),
    ("quality_issue", "质量问题"),
    ("wrong_or_missing_item", "发错货或少件"),
    ("damaged_on_arrival", "收到时已损坏"),
    ("other", "其他"),
)
REASON_LABELS = dict(RETURN_REASONS_V1)
OTHER_REASON = "other"
OTHER_TEXT_LIMIT = 200   # D5: characters, after trimming spaces at both ends
CARD_STATUSES = ("pending", "consumed", "voided", "expired")
# Void reasons (plan §4.4): the "暂不提交" click, a chat withdrawal (B7: also the first half of a switch to another
# order), the rule changed at the click.
VOID_REASONS = ("user", "chat_withdraw", "eligibility_changed")

# Labels of the return rule's outcomes (domain.eligibility_code) for the click errors, worded as the return rule
# and never as law (plan I7). A chat reply words the same outcomes with copy.NOT_RETURNABLE (B5d2).
INELIGIBLE_REASONS = {"not_delivered": "订单还没签收", "outside_window": f"已过签收后 {RETURN_WINDOW_DAYS} 天",
                      "food_opened": "食品已拆封", "used_or_impaired": "商品已使用或不再完好"}
# Item condition as the card and the order panel show it (rule v2, domain.CONDITIONS).
CONDITION_LABELS = {"sealed": "未拆封", "inspected_only": "只拆开查验过", "used_or_impaired": "已使用或不再完好"}
CARD_NOT_SUBMITTED = "尚未提交"

# Code-written copy of the card protocol (production voice; the demo environment is stated in the UI notice).
SUBMITTED_MESSAGE = "退货申请已提交（订单 {order_id}，原因：{label}）。"
ALREADY_SUBMITTED_NOTE = "这张确认卡已经提交过，原因以第一次提交的为准。"
EXISTING_REQUEST_MESSAGE = "这一单已经有一条退货申请，没有再新增，仍以原有那条为准。"
CARD_VOIDED_MESSAGE = "确认卡已作废，退货申请没有提交。"
ERRORS = {
    "CONFIRMATION_INVALID": "这张确认卡不存在、已经失效，或不属于这次咨询。",
    "CONFIRMATION_EXPIRED": "这张确认卡已过有效期，请重新申请退货。",
    "INVALID_REASON": f"请先在卡上选择退货原因；选“其他”时，请填写不超过 {OTHER_TEXT_LIMIT} 字的说明。",
    "RETURN_NOT_ELIGIBLE": "这一单现在不符合退货规则（{reason}），申请没有提交。",
    "CARD_NOT_ELIGIBLE": "这一单不符合退货规则（{reason}），不能出确认卡。",
    "RETURN_ALREADY_REQUESTED": "这一单已经有一条退货申请，不会再出确认卡。",
    "ORDER_NOT_FOUND": "当前账号下没有找到这个订单。",
    "SUBMITTED_NO_UNDO": SUBMITTED_NO_UNDO,
}


def reasons_payload():
    """The catalogue as sent with a card: version, [{code, label}] and the "other" text limit."""
    return {"version": RETURN_REASONS_VERSION, "other_text_limit": OTHER_TEXT_LIMIT,
            "options": [{"code": code, "label": label} for code, label in RETURN_REASONS_V1]}


def return_card_block(card, order):
    """An open card as a response declares it (protocol v3): binding, the order's facts and the reasons.

    ``card`` is the card view, ``order`` the customer's order record. A chat turn declares every open card
    with it and the "申请退货" click the card it created or re-showed (B3e); the page draws a card and its
    reason options from this declaration only (plan I13). The card carries no reason: the click sends one. Its words
    are the code's (author, as every reply block declares since B5d2; B5e).
    """
    return {"type": "return_card", "author": "code", "action_id": card["action_id"], "version": card["version"],
            "order_id": card["order_id"], "status": card["status"], "expires_at": card["expires_at"],
            "text": CARD_NOT_SUBMITTED,
            "order": {"order_id": order.order_id, "product_name": order.product_name,
                      "paid_amount_cents": order.paid_amount_cents, "delivered_at": order.delivered_at,
                      "days_since_delivery": (SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days,
                      "last_return_date": last_return_date(order), "condition": order.condition,
                      "condition_label": CONDITION_LABELS.get(order.condition, "")},
            "reasons": reasons_payload()}


def validate_reason(reason_code, reason_text):
    """(code, stored text) for a click, or None when the reason is invalid (plan I2).

    The code must be in the catalogue. "other" needs a text that is non-empty and at most
    OTHER_TEXT_LIMIT characters once trimmed and has no control character anywhere; the stored text
    is the trimmed one. Every other code needs an empty text and stores NULL.
    """
    if type(reason_code) is not str or reason_code not in REASON_LABELS or type(reason_text) is not str:
        return None
    if reason_code != OTHER_REASON:
        return (reason_code, None) if reason_text == "" else None
    text = reason_text.strip()
    if not text or len(text) > OTHER_TEXT_LIMIT or any(ord(char) < 32 or 127 <= ord(char) < 160
                                                       for char in reason_text):
        return None
    return reason_code, text


def click_payload(session_id, action_id, version, reason_code, reason_text):
    """What the request cache keeps of a click (protocol v3 step 1): the binding, the code and a digest of the
    text. The digest still tells a changed body apart under one request_id (REQUEST_CONFLICT), and the
    "other" text itself is kept only in the request row it was submitted with."""
    digest = (hashlib.sha256(reason_text.encode("utf-8")).hexdigest()
              if type(reason_text) is str and reason_text else "")
    return {"session_id": session_id, "action_id": action_id, "version": version,
            "reason_code": reason_code, "reason_text_sha256": digest}


def card_error(code, *, copy=None, **values):
    from .sessions import SessionError
    return SessionError(code, ERRORS[copy or code].format(**values))


class ReturnCards:
    """Card storage and the click transaction; mixed into SessionStore (see the module docstring)."""

    # ----- reads -----------------------------------------------------------------------------------
    def _card(self, customer_id, session_id, action_id):
        return self._connection.execute(
            "SELECT * FROM return_cards WHERE action_id = ? AND session_id = ? AND customer_id = ?",
            (action_id, session_id, customer_id)).fetchone()

    def _open_card(self, session_id, order_id):
        return self._connection.execute(
            "SELECT * FROM return_cards WHERE session_id = ? AND order_id = ? AND status = 'pending'",
            (session_id, order_id)).fetchone()

    def _open_cards(self, session_id):
        """The session's open cards as views, oldest first: pending and not past their expiry (B3c)."""
        rows = self._connection.execute(
            "SELECT * FROM return_cards WHERE session_id = ? AND status = 'pending' ORDER BY created_at, rowid",
            (session_id,)).fetchall()
        return [view for view in map(self._card_view, rows) if view["status"] == "pending"]

    def _latest_card(self, session_id):
        return self._connection.execute(
            "SELECT * FROM return_cards WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,)).fetchone()

    def _card_view(self, row):
        """The card as the snapshot shows it; a pending card past its expiry reads as expired."""
        if row is None:
            return None
        view = {key: row[key] for key in ("action_id", "version", "order_id", "status", "void_reason", "expires_at")}
        if view["status"] == "pending" and self._now() >= view["expires_at"]:
            view["status"] = "expired"
        return view

    def _session_return_request(self, customer_id, session_id):
        """The newest request a click in this session submitted (or found already there), without its reason.

        B3c: a session may submit several; requests are compared by when they were written."""
        row = self._connection.execute(
            """SELECT id, order_id, status FROM return_requests WHERE customer_id = ? AND order_id IN
                   (SELECT order_id FROM return_cards WHERE session_id = ? AND status = 'consumed')
               ORDER BY rowid DESC LIMIT 1""", (customer_id, session_id)).fetchone()
        return dict(row) if row else None

    # ----- preparing and voiding -------------------------------------------------------------------
    def _void(self, action_id, reason):
        if reason not in VOID_REASONS:
            raise ValueError("unknown void reason")
        self._connection.execute(
            "UPDATE return_cards SET status = 'voided', void_reason = ? WHERE action_id = ? AND status = 'pending'",
            (reason, action_id))

    def _expire(self, action_id):
        self._connection.execute(
            "UPDATE return_cards SET status = 'expired' WHERE action_id = ? AND status = 'pending'", (action_id,))

    def _prepare(self, customer_id, session_id, order_id, run_id):
        """Show a card for one eligible own order without a request; ('created' | 'reshown', card row).

        A still valid card for the same order is shown again with its version; an expired one is marked
        expired and a new card takes its place. The session's cards for other orders stay as they are (one
        open card per session and order, B3c). The rule facts are read again here, so a race only ever
        ends in an error.
        """
        try:
            order = self._owned_order(customer_id, order_id)
        except ToolError:
            raise card_error("ORDER_NOT_FOUND") from None
        if self._connection.execute("SELECT 1 FROM return_requests WHERE customer_id = ? AND order_id = ?",
                                    (customer_id, order_id)).fetchone():
            raise card_error("RETURN_ALREADY_REQUESTED")
        code = order_rule_code(order)
        if code != "eligible":
            raise card_error("RETURN_NOT_ELIGIBLE", copy="CARD_NOT_ELIGIBLE", reason=INELIGIBLE_REASONS[code])
        now = self._now()
        current = self._open_card(session_id, order_id)
        if current is not None:
            if now < current["expires_at"]:
                return "reshown", current
            self._expire(current["action_id"])
        version = 1 + self._connection.execute(
            "SELECT COUNT(*) FROM return_cards WHERE session_id = ? AND order_id = ?", (session_id, order_id)).fetchone()[0]
        action_id = "ACT-" + uuid.uuid4().hex
        self._connection.execute(
            """INSERT INTO return_cards(action_id, customer_id, session_id, order_id, version, status,
                   created_run_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (action_id, customer_id, session_id, order_id, version, run_id, now, now + self.confirmation_ttl))
        return "created", self._card(customer_id, session_id, action_id)

    def prepare_card(self, customer_id, session_id, run_id, order_id):
        """Inside a chat turn (under its lease): {'outcome', 'card'} for the order's card."""
        return self.prepare_cards(customer_id, session_id, run_id, [order_id])[0]

    def prepare_cards(self, customer_id, session_id, run_id, order_ids):
        """Inside a chat turn, in one transaction: [{'outcome', 'card'}] for each order, in the given order."""
        with self._transaction():
            self._lease(customer_id, session_id, run_id)
            prepared = [self._prepare(customer_id, session_id, order_id, run_id) for order_id in order_ids]
            return [{"outcome": outcome, "card": self._card_view(row)} for outcome, row in prepared]

    def request_card(self, customer_id, session_id, order_id, request_id):
        """The "申请退货" button and candidate clicks (POST /sessions/{id}/cards): no model call.

        The response is the session snapshot with ``card`` set to the card this click created or re-showed
        (B3c: with one card per order, a re-shown card need not be the session's latest) and ``return_card``
        declaring it the way a chat turn does (B3e)."""
        payload = {"session_id": session_id, "order_id": order_id}

        def execute():
            outcome, row = self._prepare(customer_id, session_id, order_id, None)
            card = self._card_view(row)
            return {**self._snapshot(customer_id, session_id), "card": card, "card_outcome": outcome,
                    "return_card": return_card_block(card, self._owned_order(customer_id, order_id))}

        return self._command(customer_id, session_id, "card_prepare", request_id, payload, execute)

    def void_cards(self, customer_id, session_id, run_id, action_ids, reason):
        """Inside a chat turn, in one transaction: void these open cards of this session (a chat withdrawal)."""
        with self._transaction():
            self._lease(customer_id, session_id, run_id)
            for action_id in action_ids:
                row = self._card(customer_id, session_id, action_id)
                if row is None or row["status"] != "pending":
                    raise card_error("CONFIRMATION_INVALID")
                self._void(action_id, reason)
            return self._snapshot(customer_id, session_id)

    def request_void(self, customer_id, session_id, action_id, request_id):
        """The "暂不提交" button (POST /sessions/{id}/cards/{action_id}/void): no model call.

        Voids this session's pending, unexpired card as voided(user) and appends the code message, in one
        transaction cached by request_id like every click. Refused while a turn runs (BUSY). A card that is
        not this session's open card is CONFIRMATION_INVALID; a submitted one stays submitted and the message
        names the after-sales cancellation path (there is no undo, decision D4); an expired one is marked
        expired (CONFIRMATION_EXPIRED).
        """
        payload = {"session_id": session_id, "action_id": action_id}

        def execute():
            card = self._card(customer_id, session_id, action_id)
            if card is not None and card["status"] == "consumed":
                raise card_error("CONFIRMATION_INVALID", copy="SUBMITTED_NO_UNDO")
            if card is None or card["status"] != "pending":
                raise card_error("CONFIRMATION_INVALID")
            if self._now() >= card["expires_at"]:
                self._expire(action_id)
                raise card_error("CONFIRMATION_EXPIRED")
            self._void(action_id, "user")
            self._append_messages(session_id, [{"role": "assistant", "content": CARD_VOIDED_MESSAGE}])
            self._connection.execute("UPDATE sessions SET last_reply = ? WHERE session_id = ?",
                                     (CARD_VOIDED_MESSAGE, session_id))
            # B3c: ``card`` is the card this click voided, which need not be the session's latest.
            return {**self._snapshot(customer_id, session_id),
                    "card": self._card_view(self._card(customer_id, session_id, action_id))}

        return self._command(customer_id, session_id, "card_void", request_id, payload, execute)

    # ----- the click ---------------------------------------------------------------------------------
    def confirm_card(self, customer_id, session_id, action_id, version, reason_code, reason_text, request_id):
        """The only write of a return request (protocol v3, one BEGIN IMMEDIATE transaction).

        Order: request cache (same request_id and body replays, another body is REQUEST_CONFLICT); session
        ownership and BUSY (a click while a turn runs; B3b: this replaces the old hold); card, owner,
        session and version; an already consumed card returns its saved result; pending and not expired;
        the reason; the order and the rule again; the insert; the card consumed and the code message
        appended. Failed clicks are cached like successful ones.
        """
        payload = click_payload(session_id, action_id, version, reason_code, reason_text)

        def execute():
            card = self._card(customer_id, session_id, action_id)
            if card is None or card["version"] != version:
                raise card_error("CONFIRMATION_INVALID")
            if card["status"] == "consumed":
                return {**json.loads(card["result"]), "already_submitted": True, "notice": ALREADY_SUBMITTED_NOTE}
            if card["status"] != "pending":
                raise card_error("CONFIRMATION_INVALID")
            if self._now() >= card["expires_at"]:
                self._expire(action_id)
                raise card_error("CONFIRMATION_EXPIRED")
            reason = validate_reason(reason_code, reason_text)
            if reason is None:
                raise card_error("INVALID_REASON")
            try:
                order = self._owned_order(customer_id, card["order_id"])
            except ToolError:
                raise card_error("ORDER_NOT_FOUND") from None
            existing = self._connection.execute(
                "SELECT id FROM return_requests WHERE customer_id = ? AND order_id = ?",
                (customer_id, order.order_id)).fetchone()
            if existing is None:
                code = order_rule_code(order)
                if code != "eligible":
                    self._void(action_id, "eligibility_changed")
                    raise card_error("RETURN_NOT_ELIGIBLE", reason=INELIGIBLE_REASONS[code])
                self._connection.execute(
                    """INSERT INTO return_requests(id, customer_id, order_id, reason_code, reason_text,
                           source_action_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                       ON CONFLICT(customer_id, order_id) DO NOTHING""",
                    ("RET-" + order.order_id, customer_id, order.order_id, reason[0], reason[1], action_id,
                     SCENARIO_DATE.isoformat()))
                message = SUBMITTED_MESSAGE.format(order_id=order.order_id, label=REASON_LABELS[reason[0]])
            else:
                message = EXISTING_REQUEST_MESSAGE
            self._connection.execute("UPDATE return_cards SET status = 'consumed' WHERE action_id = ?", (action_id,))
            # The code message names the preset label only; the "other" text never enters the conversation.
            self._append_messages(session_id, [{"role": "assistant", "content": message}])
            self._connection.execute("UPDATE sessions SET last_reply = ? WHERE session_id = ?", (message, session_id))
            # B3c: ``card`` is the card this click submitted, which need not be the session's latest.
            result = {**self._snapshot(customer_id, session_id),
                      "card": self._card_view(self._card(customer_id, session_id, action_id)),
                      "confirmation_message": message}
            self._connection.execute("UPDATE return_cards SET result = ? WHERE action_id = ?",
                                     (json.dumps(result, ensure_ascii=False, sort_keys=True), action_id))
            return result

        return self._command(customer_id, session_id, "card_confirm", request_id, payload, execute)
