"""Human-service tickets (first-principles plan §6.1–§6.5, stages B4a, B4b and B4d): the "找人工" button, the ticket
card and its withdrawal, and the ticket two failed turns in a row open.

A ticket records that the customer asked for a person. A session holds at most one open ticket (pending or
assigned; partial unique index ``one_open_ticket``); asking again while it is open appends an event to it and
creates nothing new (standard A3). Opening, appending and withdrawing are clicks on their own endpoints,
``POST /sessions/{id}/handoff`` and ``POST /sessions/{id}/tickets/{ticket_id}/withdraw``: no model call, cached by
request_id like every click, and allowed while a turn runs (§6.3); the persistent button and the one a failure card
offers (B4d) are the same click. B4b adds the one trigger that is not a click: when a turn fails and the turn before
it failed too, finalizing it opens the session's ticket or appends to its open one (trigger technical_failure,
standard HR5) in the same transaction, unless the customer withdrew a ticket during that run of failures and has
none open (HN7, B4d); the failed turn's reply says so (technical_failure_text; sessions._failure_next_step). Both go
through _open_or_append_ticket, the one place a ticket opens. B8 adds the chat triggers: a completed turn whose
handoff, complaint or safety items ask for the ticket (decide.ticket_request) gets it opened or appended when the store
finalizes that turn (_chat_ticket), with the highest urgency asked; a complaint's lines add the dispute path. A ticket's status changes only here: pending when it
opens, cancelled when its owner withdraws it (A5, A6); nothing in this program moves it to assigned or resolved. A
ticket never touches a return card.

The reply is code-written (§6.4): the ticket's number and status as recorded and that it can be withdrawn, then a
channel of the original platform that works now (copy.PATHS["platform_human"]); with an open
return card, that the card stays valid and submitting it still takes the customer's own click. It promises no
time and no follow-up (G2); the persistent UI notice says nobody follows these tickets up (KF2, decision D8).

The summary (§6.5) is written once, when the ticket opens (``summary_written`` = 1): the trigger and its evidence;
the latest completed turn's planned goal, marked as an unverified automatic summary, with any run of 12 or more
digits cut to its last 4 (standard §9.4) and every typed order id (domain.ORDER_TOKEN) that is not one of this
customer's orders replaced by **** (B4d; a bare number is no order id, B3e, and stays); the customer's own orders
among those this session's turns resolved or carded, with their rule facts; the cited card outcomes; the session's
cards and the requests its clicks submitted (by reason code only); the request ids of the replies already sent and
the error codes of the failed turns. It holds no "other" reason text (kept only in its return_requests row), no
order id of another customer's, no prompt, model reasoning or key. The goal is the planner's wording, so other
words it carries (a product name, a date) stay as written.

The methods run on SessionStore (``class SessionStore(Tickets, ReturnCards, Store)``) and use its connection,
transactions and request cache; each one is a single short transaction.
"""
from __future__ import annotations

import json
import re
import uuid

from .domain import ORDER_TOKEN, last_return_date, order_rule_code
from .copy import PATHS

TICKET_STATUSES = ("pending", "assigned", "resolved", "cancelled")
OPEN_STATUSES = ("pending", "assigned")
STATUS_LABELS = {"pending": "待处理", "assigned": "处理中", "resolved": "已解决", "cancelled": "已撤回"}
URGENCIES = ("normal", "high", "safety")
# Triggers: the button (standard HR1) and, from B4b, two failed turns in a row (HR5), both urgency normal; B8 adds the
# chat ones below, with their urgency (decide.ticket_request).
USER_REQUEST = "user_request"
TECHNICAL_FAILURE = "technical_failure"
# B8: a chat message asking for a person (HR1, the same trigger as the button), complaining about a decision already
# made (HR3) or reporting danger (HR2 minimal); decide.ticket_request names them, _chat_ticket opens or appends.
COMPLAINT_DISPUTE = "complaint_dispute"
SAFETY_TRIGGER = "safety"
CHAT_TRIGGERS = (USER_REQUEST, COMPLAINT_DISPUTE, SAFETY_TRIGGER)
EVENT_KINDS = ("opened", "appended", "withdrawn")

# Code-written copy (production voice; plan §6.4, standard W1–W4).
TICKET_OPENED = "已提交人工工单 {short_no}，当前状态：{status}。可以随时撤回。"
# B4d: a failed turn appends itself, not a request of the customer's, so its sentence names no request.
FAILURE_APPENDED = "已追加到人工工单 {short_no}，当前状态：{status}。可以随时撤回。"
TICKET_APPENDED = "这次请求" + FAILURE_APPENDED
TICKET_WITHDRAWN = "人工工单 {short_no} 已撤回。"
PLATFORM_CHANNEL = PATHS["platform_human"]
CARD_STILL_VALID = "确认卡仍然有效，提交仍需你本人点按。"
# B4b: leads the ticket sentence of a failed turn that follows a failed turn; the channel is its one next step. B4d:
# it states no count, since later failures of the same run append with the same sentence.
TECHNICAL_FAILURE_LEAD = "上一轮也没有处理完，"
NEED_NOTE = "自动摘要，未核实"
ERRORS = {"TICKET_NOT_FOUND": "这次咨询里没有找到这张人工工单。",
          "TICKET_CLOSED": "这张人工工单已经关闭，不能再撤回。",
          "INVALID_ARGUMENTS": "参数必须为非空字符串。"}
_LONG_DIGITS = re.compile(r"\d(?:[\s-]?\d){11,}")


def ticket_error(code):
    from .sessions import SessionError
    return SessionError(code, ERRORS[code])


def ticket_view(row):
    """A ticket as responses and GET /tickets show it (without its summary and events)."""
    return {"ticket_id": row["ticket_id"], "short_no": row["short_no"], "session_id": row["session_id"],
            "status": row["status"], "status_label": STATUS_LABELS[row["status"]], "urgency": row["urgency"],
            "summary_written": bool(row["summary_written"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def ticket_block(view):
    """The ticket card a response declares; the page draws it from this declaration only (plan I13)."""
    return {"type": "ticket", "ticket_id": view["ticket_id"], "short_no": view["short_no"],
            "status": view["status"], "status_label": view["status_label"],
            "actions": ["withdraw"] if view["status"] in OPEN_STATUSES else []}


def ticket_sentence(view, outcome, appended=TICKET_APPENDED):
    """The sentence that states what a click or a failure did to the ticket ("opened" or "appended")."""
    template = TICKET_OPENED if outcome == "opened" else appended
    return template.format(short_no=view["short_no"], status=view["status_label"])


def technical_failure_text(view, outcome):
    """B4b: the failed turn's statement of the ticket two failed turns in a row opened or appended to, and its one
    next step, the original platform's channel (no retry beside it, W6)."""
    return TECHNICAL_FAILURE_LEAD + ticket_sentence(view, outcome, FAILURE_APPENDED) + "\n" + PLATFORM_CHANNEL


def _masked_need(goal, owned):
    if type(goal) is not str or not goal.strip():
        return None
    text = ORDER_TOKEN.sub(lambda match: match.group() if match.group() in owned else "****", goal.strip())
    return _LONG_DIGITS.sub(lambda match: "****" + re.sub(r"\D", "", match.group())[-4:], text)


class Tickets:
    """Ticket storage and the two clicks; mixed into SessionStore (see the module docstring)."""

    def _ticket(self, customer_id, session_id, ticket_id):
        return self._connection.execute(
            "SELECT * FROM tickets WHERE ticket_id = ? AND session_id = ? AND customer_id = ?",
            (ticket_id, session_id, customer_id)).fetchone()

    def _open_ticket(self, session_id):
        return self._connection.execute(
            "SELECT * FROM tickets WHERE session_id = ? AND status IN ('pending', 'assigned')", (session_id,)).fetchone()

    def _ticket_event(self, ticket_id, kind, trigger, evidence):
        sequence = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM ticket_events WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]
        self._connection.execute(
            "INSERT INTO ticket_events(ticket_id, seq, kind, trigger, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, sequence, kind, trigger, json.dumps(evidence, ensure_ascii=False, sort_keys=True),
             self._now()))
        self._connection.execute("UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", (self._now(), ticket_id))

    def _ticket_summary(self, customer_id, session_id, trigger, evidence):
        goal, sent, attempts, cited = None, [], [], []
        for row in self._connection.execute(
                """SELECT request_id, status, response FROM session_requests WHERE customer_id = ? AND session_id = ?
                   AND entry = 'turn' AND response IS NOT NULL ORDER BY rowid""", (customer_id, session_id)):
            response = json.loads(row["response"])
            graph = response.get("graph") or {}
            sent.append(row["request_id"])
            if row["status"] != "completed":
                attempts.append({"request_id": row["request_id"], "status": response.get("status"),
                                 "error_code": (response.get("error") or {}).get("code")})
                continue
            goal = (graph.get("plan") or {}).get("goal", goal)
            # B5d2: the answer's card outcomes are chosen per turn item (respond.items[].claims).
            for row in (graph.get("selection") or {}).get("items") or []:
                for claim in row.get("claims") or []:
                    item = {"card_id": claim.get("card_id"), "outcome_index": claim.get("outcome_index")}
                    if item not in cited:
                        cited.append(item)
        order_ids = {order_id for row in self._connection.execute(
            "SELECT resolved_orders FROM session_runs WHERE customer_id = ? AND session_id = ?", (customer_id, session_id))
            for order_id in json.loads(row["resolved_orders"])}
        cards = self._connection.execute(
            "SELECT * FROM return_cards WHERE customer_id = ? AND session_id = ? ORDER BY created_at, rowid",
            (customer_id, session_id)).fetchall()
        order_ids |= {card["order_id"] for card in cards}
        owned = self.owned_order_ids(customer_id)   # only this customer's own orders are facts here
        orders = [{"order_id": order.order_id, "product_name": order.product_name, "status": order.status,
                   "delivered_at": order.delivered_at, "rule": order_rule_code(order),
                   "last_return_date": last_return_date(order)}
                  for order in (self._owned_order(customer_id, order_id) for order_id in sorted(order_ids & owned))]
        requests = [dict(row) for row in self._connection.execute(
            """SELECT order_id, reason_code, status FROM return_requests WHERE customer_id = ? AND order_id IN
                   (SELECT order_id FROM return_cards WHERE session_id = ? AND status = 'consumed') ORDER BY rowid""",
            (customer_id, session_id))]
        return {"trigger": [{"trigger": trigger, "evidence": evidence}],
                "user_need": {"text": _masked_need(goal, owned), "note": NEED_NOTE},
                "verified_facts": {"orders": orders, "cited_card_outcomes": cited},
                "task_state": {"cards": [{key: view[key] for key in ("order_id", "version", "status", "void_reason")}
                                         for view in map(self._card_view, cards)],
                               "return_requests": requests},
                "assistant_statements": sent, "attempts": attempts}

    def _ticket_reply(self, customer_id, session_id, view, reply):
        """Append the code message to the conversation and return the click's response."""
        self._append_messages(session_id, [{"role": "assistant", "content": reply}])
        self._connection.execute("UPDATE sessions SET last_reply = ? WHERE session_id = ?", (reply, session_id))
        return {**self._snapshot(customer_id, session_id), "ticket": view, "ticket_card": ticket_block(view)}

    def _open_or_append_ticket(self, customer_id, session_id, trigger, evidence):
        """Open the session's ticket, or append an event to its open one (A3), inside the caller's transaction; the one
        place a ticket opens. Returns the ticket's view and "opened" or "appended"."""
        row = self._open_ticket(session_id)
        if row is None:
            number = 1 + self._connection.execute(
                "SELECT COUNT(*) FROM tickets WHERE customer_id = ?", (customer_id,)).fetchone()[0]
            ticket_id, now = "TKT-" + uuid.uuid4().hex, self._now()
            summary = self._ticket_summary(customer_id, session_id, trigger, evidence)
            self._connection.execute(
                """INSERT INTO tickets(ticket_id, short_no, customer_id, session_id, status, urgency, summary,
                       summary_written, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', 'normal', ?, 1, ?, ?)""",
                (ticket_id, f"H-{number:04d}", customer_id, session_id,
                 json.dumps(summary, ensure_ascii=False, sort_keys=True), now, now))
            kind = "opened"
        else:
            ticket_id, kind = row["ticket_id"], "appended"
        self._ticket_event(ticket_id, kind, trigger, evidence)
        return ticket_view(self._ticket(customer_id, session_id, ticket_id)), kind

    def _chat_ticket(self, customer_id, session_id, request_id, wanted, shown=""):
        """B8: the ticket a completed turn's handoff, complaint or safety items ask for (decide.ticket_request), opened
        or appended inside the finalizing transaction, so only a finished turn states a ticket (like B4b's failures).
        Raises the ticket's urgency to the highest asked, never lowers it. Returns (view, outcome, the code's lines);
        a path line the turn's reply already shows (``shown``, e.g. the answer's path block) is not repeated (R122)."""
        from .sessions import SessionError
        if (type(wanted) is not dict or not wanted.get("triggers")
                or any(trigger not in CHAT_TRIGGERS for trigger in wanted["triggers"])
                or wanted.get("urgency") not in URGENCIES):
            raise SessionError("INVALID_ARGUMENTS", "工单请求格式无效。")
        evidence = {"source": "chat", "request_id": request_id, "item_indexes": list(wanted.get("item_indexes") or [])}
        view = outcome = None
        for trigger in wanted["triggers"]:
            view, kind = self._open_or_append_ticket(customer_id, session_id, trigger, evidence)
            outcome = outcome or kind
        if URGENCIES.index(wanted["urgency"]) > URGENCIES.index(view["urgency"]):
            self._connection.execute("UPDATE tickets SET urgency = ? WHERE ticket_id = ?",
                                     (wanted["urgency"], view["ticket_id"]))
            view = ticket_view(self._ticket(customer_id, session_id, view["ticket_id"]))
        lines = [ticket_sentence(view, outcome), PLATFORM_CHANNEL]
        if COMPLAINT_DISPUTE in wanted["triggers"]:
            lines.append(PATHS["platform_dispute"])
        if self._open_cards(session_id):
            lines.append(CARD_STILL_VALID)
        lines = [lines[0], *(line for line in lines[1:] if line not in shown)]
        return view, outcome, "\n".join(lines)

    def request_handoff(self, customer_id, session_id, request_id):
        """The "找人工" button (POST /sessions/{id}/handoff): opens the session's ticket or appends to its open one."""
        evidence = {"source": "handoff_button", "request_id": request_id}

        def execute():
            view, kind = self._open_or_append_ticket(customer_id, session_id, USER_REQUEST, evidence)
            lines = [ticket_sentence(view, kind), PLATFORM_CHANNEL]
            if self._open_cards(session_id):
                lines.append(CARD_STILL_VALID)
            return {**self._ticket_reply(customer_id, session_id, view, "\n".join(lines)), "ticket_outcome": kind}

        return self._command(customer_id, session_id, "ticket_handoff", request_id, {"session_id": session_id},
                             execute, allow_busy=True)

    def withdraw_ticket(self, customer_id, session_id, ticket_id, request_id):
        """The ticket card's "撤回" (POST /sessions/{id}/tickets/{ticket_id}/withdraw): the open ticket becomes cancelled.

        Only this customer's ticket of this session can be withdrawn (TICKET_NOT_FOUND otherwise); a closed one
        stays as it is (TICKET_CLOSED)."""
        payload = {"session_id": session_id, "ticket_id": ticket_id}

        def execute():
            row = self._ticket(customer_id, session_id, ticket_id)
            if row is None:
                raise ticket_error("TICKET_NOT_FOUND")
            if row["status"] not in OPEN_STATUSES:
                raise ticket_error("TICKET_CLOSED")
            self._connection.execute("UPDATE tickets SET status = 'cancelled' WHERE ticket_id = ?", (ticket_id,))
            self._ticket_event(ticket_id, "withdrawn", USER_REQUEST,
                               {"source": "withdraw_button", "request_id": request_id})
            view = ticket_view(self._ticket(customer_id, session_id, ticket_id))
            reply = TICKET_WITHDRAWN.format(short_no=view["short_no"])
            return {**self._ticket_reply(customer_id, session_id, view, reply), "ticket_outcome": "withdrawn"}

        return self._command(customer_id, session_id, "ticket_withdraw", request_id, payload, execute, allow_busy=True)

    def list_tickets(self, customer_id):
        """GET /tickets: this customer's tickets, oldest first, with their summary and events (none of anyone else's)."""
        if type(customer_id) is not str or not customer_id.strip():
            raise ticket_error("INVALID_ARGUMENTS")
        with self._transaction(read_only=True):
            tickets = []
            for row in self._connection.execute(
                    "SELECT * FROM tickets WHERE customer_id = ? ORDER BY created_at, rowid", (customer_id,)).fetchall():
                events = [{"seq": event["seq"], "kind": event["kind"], "trigger": event["trigger"],
                           "evidence": json.loads(event["evidence"]), "created_at": event["created_at"]}
                          for event in self._connection.execute(
                              "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY seq", (row["ticket_id"],))]
                tickets.append({**ticket_view(row), "customer_id": row["customer_id"],
                                "summary": json.loads(row["summary"]), "events": events})
            return tickets
