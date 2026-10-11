"""The single assembler of a chat reply (first-principles plan §5.3, B5d2; B5f).

One fixed order, no mode switch, no whole-reply replacement:
  1. safety lines (B8; none yet)
  2. this turn's action blocks: the business outcomes, one per business item kind (card, candidates, voided card,
     request status, order list), a dropped return or withdrawal item, an order id this account does not have
  3. the order facts the answer chose to show (show_order_ids)
  4. the answer's items, in item order (B5f, item_blocks): cards whole (selected outcomes first as the direct answer,
     with their conditions; the rest folded, D7), the item's note or the code's line, its path
  5. the route block (D10)
  6. the model's one next-step sentence (none on a route turn, K23, like the notes)
  7. at most one follow-up question (its words are the model's, its options the code's)
Only when every answer item was refused or is a gap do the items come before the order facts (service standard S1).
The fallback sentence stands alone, only when the answer was asked for and the reply would show nothing at all: no block
and no open card, whose reminder line counts as content too (§5.5; B7r).

Every block declares ``type``, ``author`` ("code" or "model") and ``text``; the reply saved to the history is the
blocks' text joined, in this order. Every code sentence comes from copy.py; card text is the card's own words.
Nothing here decides anything a record does not already say, and nothing here writes.
"""
from __future__ import annotations

from copy import deepcopy

from . import copy as text
from .decide import MAX_AUTO_CARDS
from .business import mention_refs
from .catalog import claim_premises

PRESENTATION_SCHEMA = "graph-presentation-blocks-v2"
CODE, MODEL = "code", "model"
CONDITIONS_LABEL, BASIS_LABEL = "适用条件：", "依据："


# ---------------------------------------------------------------- one order
def money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def _receipt_day(view):
    days = view.get("days_since_delivery")
    return text.RECEIPT_DAY_ZERO if days == 0 else text.RECEIPT_DAY.format(days=days)


def eligibility_text(view):
    """What the return rule says about one own order (its code, worded once here), or '' for a bare record."""
    if "eligibility_code" not in view:
        return ""
    request = view.get("return_request")
    if request:
        status = text.REQUEST_STATUS.get(request.get("status"), request.get("status"))
        return f"已经有一条退货申请，当前状态：{status}。"
    code = view["eligibility_code"]
    if code == "eligible":
        return text.RETURNABLE_LAST_DAY if view.get("last_return_day") else \
            text.RETURNABLE.format(last_date=view.get("last_return_date"))
    return text.NOT_RETURNABLE[code].format(last_date=view.get("last_return_date"))


def order_line(view):
    line = text.ORDER_LINE.format(order_id=view["order_id"], product=view["product_name"],
                                  amount=money(view["paid_amount_cents"]), delivered_at=view["delivered_at"],
                                  day=_receipt_day(view))
    return line + eligibility_text(view)


def _open(view):
    return bool(view.get("return_eligible")) and not view.get("return_request")


# ---------------------------------------------------------------- the business outcome
def _product(result, order_id):
    rows = list(result.get("orders") or []) + list(result.get("candidate_orders") or [])
    if isinstance(result.get("order"), dict):
        rows.append(result["order"])
    return next((row.get("product_name") for row in rows if row.get("order_id") == order_id), "")


def _mentions(result):
    """Lead sentences for the user's own product or number mentions the backend resolved."""
    lead = []
    for ref in mention_refs(result.get("binding") or {}):
        span = str(ref.get("span") or "")[:12]
        if ref.get("outcome") == "unique":
            order_id = ref["matched_order_ids"][0]
            product = _product(result, order_id)   # none for a withdrawal or a card choice (B7r)
            sentence = (text.MENTION_MATCHED.format(span=span, order_id=order_id, product=product) if product
                        else text.MENTION_MATCHED_ID.format(span=span, order_id=order_id))
        elif ref.get("outcome") == "unmatched":
            sentence = text.ORDERS_NOT_IN_ACCOUNT.format(order_ids=f"“{span}”")
        else:
            continue
        if sentence not in lead:
            lead.append(sentence)
    return lead


def _quality_note(views):
    return [text.QUALITY_PROBLEM_PATH] if any(view.get("eligibility_code") == "outside_window" for view in views) else []


def _several(result):
    """One line per order a return named (B3c), in the named order."""
    views = {row.get("order_id"): row for row in result.get("orders") or [] if isinstance(row, dict)}
    lines, ineligible = [], []
    for row in result["return_outcomes"]:
        order_id, outcome = row.get("order_id"), row.get("outcome")
        view = views.get(order_id) or {"order_id": order_id}
        if outcome == "created":
            lines.append(text.CARD_READY.format(order_id=order_id, product=view.get("product_name", "")))
        elif outcome == "reshown":
            lines.append(text.CARD_KEPT.format(order_id=order_id))
        elif outcome == "ineligible":
            lines.append(order_line(view))
            ineligible.append(view)
        elif outcome == "already_requested":
            status = (view.get("return_request") or {}).get("status")
            lines.append(text.REQUEST_EXISTS.format(order_id=order_id,
                                                    status=text.REQUEST_STATUS.get(status, status)))
        else:
            lines.append(text.CARD_LIMIT.format(limit=MAX_AUTO_CARDS, order_id=order_id))
    return lines, _quality_note(ineligible)


def business_parts(result, *, foreign_ids=()):
    """(lead, lines, note) of a business outcome, from its structured fields only (never its ``reply``)."""
    status = result.get("status")
    if result.get("ok") is not True:
        if (result.get("error") or {}).get("code") == "ORDER_NOT_FOUND":
            return "", [text.ORDERS_NOT_IN_ACCOUNT.format(order_ids="、".join(foreign_ids)) if foreign_ids
                        else text.ORDER_NOT_FOUND], ""
        return "", [text.ITEM_DEGRADED], ""
    lead, lines, note = _mentions(result), [], []
    if isinstance(result.get("return_outcomes"), list):
        lines, note = _several(result)
    elif status == "order_found":
        orders = result.get("orders") or []
        if result.get("return_later"):   # B7r: a return wanted later shows the facts and says no card was prepared
            lead.append(text.RETURN_NOT_STARTED)
        if (result.get("binding") or {}).get("list_scope") == "all_owned":
            lead.append(text.ORDERS_LEAD.format(count=len(orders)) if orders else text.NO_ORDERS)
        elif any(ref.get("outcome") == "ambiguous" for ref in mention_refs(result.get("binding") or {})):
            lead.append(text.CANDIDATES_LEAD)
        lines = [order_line(view) for view in orders]
        note = _quality_note(orders) + ([text.APPLY_HINT] if any(_open(view) for view in orders) else [])
        if not lines and not lead:
            lines = [text.NO_ORDERS]
    elif status == "ineligible" and isinstance(result.get("order"), dict):
        lines, note = [order_line(result["order"])], _quality_note([result["order"]])
    elif status == "already_requested" and isinstance(result.get("order"), dict):
        order = result["order"]
        state = (order.get("return_request") or {}).get("status")
        lines = [text.REQUEST_EXISTS.format(order_id=order["order_id"],
                                            status=text.REQUEST_STATUS.get(state, state))]
    elif status == "awaiting_confirmation":
        card, order = result.get("card") or {}, result.get("order") or {}
        template = text.CARD_KEPT if result.get("card_outcome") == "reshown" else text.CARD_READY
        lines = [template.format(order_id=card.get("order_id") or order.get("order_id"),
                                 product=order.get("product_name", ""))]
    elif status == "card_voided":
        lines = [text.CARD_VOIDED.format(order_id=result.get("voided_order_id"))]
    elif status == "card_choice_needed":
        lines = [text.CARD_CHOOSE]
    elif status == "no_open_card":
        lines = [text.NO_OPEN_CARD + text.CANCEL_AFTERSALES_PATH]
    elif status == "submitted_no_undo":
        lines = [text.SUBMITTED_NO_UNDO]
    elif status == "clarification_needed":
        candidates = sorted(result.get("candidate_orders") or [], key=lambda view: not _open(view))
        if candidates:
            lines = [text.CANDIDATES_LEAD if any(_open(view) for view in candidates) else text.NO_RETURNABLE_ORDER]
            lines += [order_line(view) for view in candidates]
        else:
            lines = [text.ASK_WHICH_ORDER]
    return " ".join(lead), lines, "".join(note)


def business_block(result, *, foreign_ids=()):
    """One business outcome as one code block (the page draws its orders and candidates), or None. ``action`` names the
    planner's business item the outcome carries out (B7)."""
    if not isinstance(result, dict) or result.get("status") == "no_action":
        return None
    lead, lines, note = business_parts(result, foreign_ids=foreign_ids)
    orders = result.get("orders") or ([result["order"]] if isinstance(result.get("order"), dict) else [])
    block = {"type": "business", "author": CODE, "action": result.get("item_kind"), "status": result.get("status"),
             "ok": result.get("ok") is True, "text": "\n".join(part for part in (lead, *lines, note) if part),
             "lead": lead, "note": note, "orders": deepcopy(orders)}
    if isinstance(result.get("candidate_orders"), list):
        block["candidate_orders"] = deepcopy(result["candidate_orders"])
    if result.get("ok") is not True:
        block["error_code"] = (result.get("error") or {}).get("code")
    return block


def not_in_account_block(order_ids):
    """Order ids the message typed that this account does not have, when no business outcome already says so (I4):
    neither confirms such an order exists nor says whose it is."""
    return {"type": "text", "author": CODE, "text": text.ORDERS_NOT_IN_ACCOUNT.format(order_ids="、".join(order_ids))}


def order_facts_block(views, *, above=""):
    """The own orders the answer chose to show (B5b), worded like an order lookup; None when there are none."""
    if not views:
        return None
    lines = [order_line(view) for view in views]
    note = "".join(_quality_note(views) + ([text.APPLY_HINT] if any(_open(view) for view in views) else []))
    note = "" if note and note in above else note
    return {"type": "order_facts", "author": CODE, "text": "\n".join([*lines, note] if note else lines),
            "orders": deepcopy(views), "note": note}


# ---------------------------------------------------------------- cards
def _outcome(card, index):
    premises = claim_premises(card, index)
    return {"outcome_index": index, "outcome": card["outcomes"][index].strip(),
            "premises": [part.strip() for part in premises or [] if part.strip()]}


def card_block(card, selected):
    """One card, whole: the selected outcomes first, as the direct answer, each with its conditions; the card's title
    as the basis after them; the others folded (D7). No "this applies to you": the conditions are shown, not judged."""
    chosen = [_outcome(card, index) for index in selected]
    others = [_outcome(card, index) for index in range(len(card["outcomes"])) if index not in selected]
    lines = []
    for row in chosen:
        lines.append(row["outcome"])
        if row["premises"]:
            lines.append(CONDITIONS_LABEL + "；".join(part.rstrip("。； ") for part in row["premises"]) + "。")
    lines.append(BASIS_LABEL + card["title"])
    return {"type": "policy_card", "author": CODE, "card_id": card["id"], "card_title": card["title"],
            "effect_domain": card.get("effect_domain", "unclassified"),
            "source_kind": card.get("source_kind", "platform"), "text": "\n".join(lines),
            "selected": chosen, "other_outcomes": others, "exceptions": list(card.get("exceptions") or []),
            "evidence_ids": list(card.get("evidence_ids") or []),
            "references": [{"evidence_id": ref.get("evidence_id"), "quotes": list(ref.get("quotes") or [])}
                           for ref in card.get("references") or [] if isinstance(ref, dict)]}


def item_blocks(rows, cards, *, answering, ask_shown=False, code_shown=False):
    """The answer's rows as blocks, in item order (B5f): its cards where they first appear (every outcome any row chose,
    D7); its note (already guarded) or, for an answer item left with nothing of its own (an outcome, a note, a shown
    follow-up if asked, the turn's action or order-facts blocks if answered, partial or handled), the code's line for
    its disposition; its path, once. The fallback is the assembler's (B7r: only for a reply showing nothing at all)."""
    by_id, outcomes = {card["id"]: card for card in cards}, {}
    for claim in (claim for row in rows for claim in row.get("claims") or []):
        card = by_id.get(claim.get("card_id"))
        if card is not None and claim_premises(card, claim.get("outcome_index")) is not None:
            chosen = outcomes.setdefault(card["id"], [])
            chosen += [] if claim["outcome_index"] in chosen else [claim["outcome_index"]]
    blocks, placed = [], set()
    for row in rows:
        mine = [claim.get("card_id") for claim in row.get("claims") or [] if claim.get("card_id") in outcomes]
        blocks += [card_block(by_id[card], outcomes[card]) for card in dict.fromkeys(mine) if card not in placed]
        placed.update(mine)
        note, disposition = row.get("note"), row.get("disposition")
        own = (bool(mine or note) or (disposition == "asked" and ask_shown)
               or (disposition in ("answered", "partial", "handled_by_system") and code_shown))
        if note:
            blocks.append({"type": "text", "author": MODEL, "text": note, "item": row["index"]})
        elif row["index"] in answering and not own and disposition in text.ITEM_LINES:
            blocks.append({"type": "text", "author": CODE, "text": text.ITEM_LINES[disposition], "item": row["index"],
                           "disposition": disposition})
        if row.get("path_id") in text.PATHS and not any(block.get("path_id") == row["path_id"] for block in blocks):
            blocks.append({"type": "path", "author": CODE, "path_id": row["path_id"],
                           "text": text.PATHS[row["path_id"]]})
    return blocks


# ---------------------------------------------------------------- route (D10)
def route_text(route):
    status = (route or {}).get("status", "not_requested")
    if status == "evidence_found":
        requested = route["requested_route"]
        places = {"origin": requested["origin"], "destination": requested["destination"]}
        parts = [text.ROUTE_AMOUNT.format(amount=route["source_amount_text"], **places) + text.ROUTE_REFERENCE_ONLY
                 if route.get("amount_requested") else text.ROUTE_DIRECTION.format(**places), text.ROUTE_CONDITIONS]
        return "".join(parts)
    if status in {"needs_clarification", "ambiguous"}:
        missing = route.get("missing") or ["origin", "destination"]
        return text.ROUTE_NEEDS_PLACES.format(missing="和".join(text.ROUTE_PLACE[side] for side in missing))
    if status != "not_requested":
        return text.ROUTE_NOT_FOUND
    return ""


def route_block(route):
    words = route_text(route)
    if not words:
        return None
    block = {"type": "route", "author": CODE, "status": route["status"], "text": words}
    requested = route.get("requested_route") or {}
    if requested.get("origin") and requested.get("destination"):
        block["origin"], block["destination"] = requested["origin"], requested["destination"]
    if route["status"] == "evidence_found" and route.get("amount_requested"):
        block["amount_text"] = route.get("source_amount_text")
    if route.get("missing"):
        block["missing"] = list(route["missing"])
    return block


# ---------------------------------------------------------------- the reply
def assemble(*, action_blocks=(), order_facts=None, items=(), route=None, next_step=None, ask=None, notes=(),
             lead=False, fallback=False):
    """(reply, blocks) in the fixed order of the module docstring; ``lead`` puts ``items`` (item_blocks) before the
    order facts. ``notes``: code sentences about an assumption the code made (copy.ASSUMED_RETURN_PICKUP).
    ``fallback``: the answer was asked for and no open card stays; then an empty reply is the fallback (B7r)."""
    blocks = [block for block in action_blocks if block]
    blocks += [{"type": "text", "author": CODE, "text": note} for note in notes]
    facts = [order_facts] if order_facts else []
    blocks += [*items, *facts] if lead else [*facts, *items]
    if route:
        blocks.append(route)
    if next_step and next_step.strip():
        blocks.append({"type": "text", "author": MODEL, "text": next_step.strip(), "next_step": True})
    if ask and ask.get("text", "").strip():
        option = ask.get("option_set_id", "none")
        blocks.append({"type": "ask", "author": MODEL, "text": ask["text"].strip(), "option_set_id": option,
                       "options": list(text.OPTION_SETS.get(option, {}).get("options", []))})
    if fallback and not blocks:
        blocks = [{"type": "text", "author": CODE, "text": text.FALLBACK, "fallback": True}]
    return reply_text(blocks), blocks


def reply_text(blocks):
    """The saved reply: every block's text in order; a path carries its label."""
    parts = [(text.PATH_LABEL if block["type"] == "path" else "") + block["text"] for block in blocks
             if block.get("text")]
    return "\n\n".join(parts)


def presentation_payload(blocks):
    return {"schema": PRESENTATION_SCHEMA, "display_only": True, "canonical_text_field": "reply",
            "blocks": [block for block in blocks if block]}
