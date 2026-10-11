"""The return part of the decision table (plan §4.1; B3a, several cards B3c), decided by code in one place.

Pure functions: they read facts the caller has already resolved (the user's own orders with their rule
facts, the session's open cards, the orders K5 may use) and name the outcome. BusinessAdapter
executes the outcome through the store; the store re-checks every fact inside its transaction, so a
race only ends in an error, never in a different write. Nothing here can submit a return.
"""
from __future__ import annotations

from .tickets import COMPLAINT_DISPUTE, SAFETY_TRIGGER, URGENCIES, USER_REQUEST

# The planner's business items (B7: plan_turn names them; understand.py checks their words).
RETURN_REQUEST = "return_request"     # prepare a card for each named eligible order (D4)
WITHDRAW = "withdraw"                 # void one open card; a pause is no withdrawal and keeps the card (K21)
OWN_ORDER_STATUS = "own_order_status"  # read own orders, including "can it be returned" (D3)
LATER = "later"                       # a return_request's subkind: wanted later, or not to be submitted now (B7r)

# How a return item's order was found; a "by" of None means the user is shown candidates instead.
BY_MESSAGE, BY_OPEN_CARD, BY_K5 = "current_message", "open_card", "K5_latest_unique_order"
# Decision D4: one turn prepares a card for each eligible order the current message names, at most this many.
MAX_AUTO_CARDS = 4


def return_targets(named, ambiguous, *, current, all_mine, withdrawn, open_orders, latest_resolved, own):
    """(order_ids, by, candidates) for a return item (§4.1 rows "唯一", "没指明", "有歧义", "点名多单"; K5).

    named: own orders the current message names exactly; ambiguous: own orders a product mention may mean;
    current: the item pointed at "the current card"; withdrawn: orders whose card this turn's withdraw item voided
    ("不退那单，改退另一单", B7); open_orders: the orders of the session's open cards after that; latest_resolved: what
    the latest completed turn of this session resolved. Every order the current message names is a target (B3c, D4).
    A history pointer is never trusted: an order the current message does not name is found by the only open card (for
    "current") or by K5, never one withdrawn this turn, and otherwise the user picks one of the other own orders.
    """
    if ambiguous:
        return [], None, sorted(set(named) | set(ambiguous))
    if named:
        return sorted(named), BY_MESSAGE, []
    others = sorted(set(own) - set(withdrawn))
    if all_mine:
        return [], None, others
    if current and len(open_orders) == 1:
        return list(open_orders), BY_OPEN_CARD, []
    if len(latest_resolved) == 1 and latest_resolved[0] in others:
        return [latest_resolved[0]], BY_K5, []
    return [], None, others


def return_outcome(view, open_orders, *, later=False):
    """One own order's row of the table: prepare, reshow, ineligible or already_requested; for a return the user wants
    later (subkind LATER, B7r) always "facts": its read-only facts and "申请退货" button, never a card."""
    if later:
        return "facts"
    if view.get("return_request"):
        return "already_requested"
    if not view.get("return_eligible"):
        return "ineligible"
    if view["order_id"] in open_orders:
        return "reshow"
    return "prepare"


def carded(targets, outcomes):
    """The targets that get a card this turn: the eligible ones, in order, at most MAX_AUTO_CARDS (D4)."""
    return [order_id for order_id in targets if outcomes[order_id] in {"prepare", "reshow"}][:MAX_AUTO_CARDS]


def withdraw_outcome(named, open_orders, requested, *, unclear=False):
    """(outcome, order_ids) for a withdrawal (§4.1 withdraw row): void, choose, submitted or no_open_card.

    named: own orders the current message names exactly; unclear: the message refers to an order it does not
    pin down (a mention that fits several own orders, a number that is none of them); requested: the named
    orders that already have a return request (for a withdrawal that names none, the session's latest
    submitted one). A chat withdrawal voids at most one card (B3e): the open card of the one order the message
    names, or the only open card when it names none. Anything less certain voids nothing and lists the open
    cards, each dropped by its own "暂不提交" ("choose"). A submitted request cannot be withdrawn here (D4):
    "submitted" answers with the after-sales cancellation path.
    """
    if not unclear:
        pointed = sorted(set(named) & set(open_orders)) if named else list(open_orders)
        if len(pointed) == 1 and len(set(named)) <= 1:
            return "void", pointed
        if not pointed and requested:
            return "submitted", sorted(requested)
    return ("choose", sorted(open_orders)) if open_orders else ("no_open_card", [])


# B8 (plan §6.1, §6.3; standard HR1, HR2 minimal, HR3): the chat items code turns into the session's ticket. The planner
# only labels the item (understand.KINDS); this table decides. An improper request opens nothing (HN5): refusing it is the
# answer's job. A safety item that reports danger now is urgent; harm already done and a complaint are high.
TICKET_TRIGGERS = {"handoff_request": USER_REQUEST, "complaint": COMPLAINT_DISPUTE, "safety": SAFETY_TRIGGER}
URGENT_SAFETY = frozenset({"product_hazard", "medical_emergency", "fraud_or_theft", "crisis", "none"})


def ticket_request(items):
    """What a turn's handoff, complaint and safety items ask of the session's ticket, or None: the triggers in the
    items' order (each once), the highest urgency and the items' indexes. The store opens or appends the ticket when it
    finalizes the completed turn (sessions._finalize_turn), so only a finished turn ever states a ticket."""
    triggers, urgency, indexes = [], URGENCIES[0], []
    for item in items:
        trigger = TICKET_TRIGGERS.get(item["kind"])
        if trigger is None:
            continue
        level = ("safety" if item["kind"] == "safety" and item["subkind"] in URGENT_SAFETY
                 else "high" if item["kind"] in ("safety", "complaint") else "normal")
        if trigger not in triggers:
            triggers.append(trigger)
        urgency = max(urgency, level, key=URGENCIES.index)
        indexes.append(item["index"])
    return {"triggers": triggers, "urgency": urgency, "item_indexes": indexes} if triggers else None
