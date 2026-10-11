"""Business boundary of a chat turn: order resolution and the return decisions for the planner's business items.

The planner lists items (B7, understand.py); this adapter resolves the user's own orders each business item names and
executes the outcome decide.py names for it: read orders, show candidates, prepare or re-show return cards (one per
order, B3c), or void an open card. A card is only a proposal: submitting a return is not available here, and nothing in
a turn carries a reason (B3a, plan §4.1–§4.3). A quote proves where an order came from, not that a model understood
consent correctly; that stays an evaluation dimension.
"""
from __future__ import annotations

import re
import uuid

from . import decide
from .domain import (_CLAUSE_BREAK, ORDER_TOKEN, SCENARIO_DATE, ToolError, order_view,
                     resolve_product_mention)
from .return_cards import CANCEL_AFTERSALES_PATH, CARD_VOIDED_MESSAGE, SUBMITTED_NO_UNDO
from .runtime import STOP_CODES, RunStopped
from .sessions import SessionError, SessionStore


# Alias for "all of the current customer's orders": a read, or a return that has not picked an order yet.
ALL_MINE = "all_mine"
# The number part of an order id ("1003" in "O1003"), or a bare number proposed as an id as typed (B3d). It
# has exactly the order token's four digits (B3e): a day count, an amount or a phone number is no order number.
_ORDER_NUMBER = re.compile(r"[A-Za-z]?([0-9]{4})")
# A bare four-digit number standing alone in the user's words (B3d, B3e): the order number an item names when its
# order_id is "none" (the planner's enum holds only the account's own ids, B7).
_BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9_-])[0-9]{4}(?![A-Za-z0-9_-])")
# Where a resolved turn records how the user's own words named orders other than by a literal id.
MENTION_REF_KEYS = ("product_refs", "number_refs")
# The business items in the order a turn executes them, as (kind, subkind): a withdrawal first, so a switch ("不退那单，
# 改退这单") never re-prepares the order it dropped (decide.return_targets), then returns (now, then later, B7r), reads.
ITEM_ORDER = ((decide.WITHDRAW, "none"), (decide.RETURN_REQUEST, "none"), (decide.RETURN_REQUEST, decide.LATER),
              (decide.OWN_ORDER_STATUS, "none"))

# Trace wording of the outcomes (business result ``reply``). B5d2: no model and no reply reads it; the reply's code
# sentences come from copy.py through assemble.py, and the answer reads a normalised action list (respond.actions).
CARD_READY_RESULT = "退货确认卡已备好，尚未提交；退货原因由用户在卡上选择，提交只能由用户本人点按完成。"
ALREADY_REQUESTED_RESULT = "这笔订单已有退货申请，不会生成新的确认卡。"
INELIGIBLE_RESULT = "这笔订单不符合退货规则，所以没有出确认卡。"
CANDIDATES_RESULT = "没有确定要退哪一单，已列出用户名下的订单供点选。"
RETURN_LATER_RESULT = "用户要以后再办这次退货，只列出订单信息，没有备确认卡。"
# B3c: several orders named in one message, each handled by the table; return_outcomes lists the results.
SEVERAL_ORDERS_RESULT = "用户点名了多笔订单，已逐单核对：可退的各备好一张确认卡（尚未提交），其余见各单结果。"
NO_OPEN_CARD_RESULT = "当前没有待提交的退货确认卡。" + CANCEL_AFTERSALES_PATH
# Several open cards and the withdrawal names none of them: the user drops one on the card itself (the reply
# already ends with the open cards' orders, ConversationAgent._envelope).
CHOOSE_CARD_RESULT = "要作废的是哪张，请直接点那张卡上的“暂不提交”。"
# A business item understand.check dropped (K20); the reply's sentence is copy.ITEM_DEGRADED.
ITEM_REJECTED_RESULT = "这条消息里有一项要办的事没有通过核对，这次没有办理。"


def order_number(order_id):
    """The digits that identify an order without its letter ("O1003" -> "1003"), or None."""
    match = _ORDER_NUMBER.fullmatch(order_id) if type(order_id) is str else None
    return match[1] if match else None


def mention_refs(binding):
    """Every product or number mention a result's binding records, in that order."""
    return [ref for key in MENTION_REF_KEYS for ref in (binding or {}).get(key, [])]


class BusinessAdapter:
    """Execute a turn's checked business items (B7) under an already active SessionStore run lease.

    Items (understand.check kept them; their words come from the current message): {kind: return_request | withdraw |
    own_order_status, quote, order_quote, order_id: an own order id | all_mine | current | none}. Each item becomes one
    order reference (``item_ref``): its order_id with the user's words naming it (the order_quote, else the item's
    quote); an order_id of "none" names whatever order id or bare number those words type (the planner may only pick
    the account's own ids). Items of a kind (and subkind) are executed together, in ITEM_ORDER, as one outcome of
    decide.py; a return wanted later shows its orders' facts and prepares no card (B7r).

    An order is named by the current message when a ref's quote contains its id, or is the user's own
    product mention that matches it among the current customer's orders (``order_catalog``); a mention
    matching several orders lists them. A bare four-digit number the user typed without the letter ("1003",
    B3d; other lengths are no order number, B3e) names the own order whose number it is when exactly one
    does; when several or none of the own orders have that number the user picks from them (several) or from
    all own orders (none), so the number never reaches another customer's order. A return or withdrawal
    target the current message does not name (a history pointer, "这单") is never trusted: "current" means
    the session's only open card, and otherwise K5 or the candidates decide (decide.return_targets). Every
    order the current message names is a return target, and each eligible one gets its own card (B3c, D4);
    a withdrawal voids one card at most (decide.withdraw_outcome, B3e). A read keeps the strict check: a read whose
    words name no order is dropped before this step (``read_problem``). ``all_mine`` reads every order of the
    current customer, or offers them all for a return.
    """

    def __init__(self, store: SessionStore, customer_id: str, session_id: str, run_id: str):
        self.store = store
        self.customer_id = customer_id
        self.session_id = session_id
        self.run_id = run_id

    @property
    def _args(self):
        return self.customer_id, self.session_id, self.run_id

    def _current_message(self):
        return self.store.current_user_message(*self._args)

    def _snapshot(self):
        return self.store.get_session(self.customer_id, self.session_id)

    def _reserve(self, tool_name):
        self.store.reserve_tool_call(*self._args, tool_name=tool_name,
                                     tool_call_id="graph-" + uuid.uuid4().hex)

    @staticmethod
    def _raise_if_stopped(error):
        # Runtime termination must leave the graph, never become ordinary
        # business facts that allow another model request and completed status.
        if error.code in STOP_CODES:
            raise RunStopped(*STOP_CODES[error.code]) from None

    @classmethod
    def _order_in_quote(cls, order_id, quote, message):
        """The quote encloses this order id as the user typed it. A bare number is no typed id: it names an
        order only as a four-digit order number (_number_mention; B3e)."""
        return not order_id.isdigit() and cls._token_in_quote(order_id, quote, message)

    @staticmethod
    def _token_in_quote(token, quote, message):
        # Match the complete token in the original message, then require
        # that exact occurrence to be enclosed by an exact quote occurrence.
        # A separately quoted prefix must not manufacture a token boundary.
        pattern = r"(?<![A-Za-z0-9_-])" + re.escape(token) + r"(?![A-Za-z0-9_-])"
        occurrences = list(re.finditer(pattern, message))
        offset = message.find(quote)
        while offset != -1:
            if any(offset <= match.start() and match.end() <= offset + len(quote)
                   for match in occurrences):
                return True
            offset = message.find(quote, offset + 1)
        return False

    @staticmethod
    def order_catalog(store, customer_id):
        """The current customer's order ids and names: what a product mention may refer to.

        The planner context, planning-time validation, ``apply`` and the final
        rejection all use this one read, so they agree on every mention. It is
        session context, not a tool call.
        """
        return {"orders": [{"order_id": order.order_id, "product_name": order.product_name}
                           for order in store.owned_orders(customer_id)]}

    @staticmethod
    def _mention(ref, catalog, message=""):
        quote = ref.get("quote")
        typed = type(quote) is str and any(
            quote in clause and ORDER_TOKEN.search(clause) for clause in _CLAUSE_BREAK.split(message or ""))
        if not catalog or type(quote) is not str or ORDER_TOKEN.search(quote) or typed:
            # A quote, or the user's clause around it, that types an order id is
            # about that id ("O2001那个杯子"), never a product mention.
            return {"outcome": "none", "matched": [], "span": None}
        return resolve_product_mention(quote, ref["order_id"], catalog["orders"])

    @classmethod
    def _number_mention(cls, ref, message, catalog):
        """B3d: the proposed order's number typed bare in the quote, resolved among the own orders; or None.

        The number must stand alone in the current message (not inside "O1003" or a longer number), the
        same boundary a literal id has. ``unique``: one own order has this number; ``ambiguous``: several
        do (they are the candidates); ``unmatched``: none does. Only the own orders are ever matched.
        """
        number, quote = order_number(ref["order_id"]), ref.get("quote")
        if number is None or type(quote) is not str or not cls._token_in_quote(number, quote, message):
            return None
        matched = sorted(order["order_id"] for order in (catalog or {}).get("orders", [])
                         if order_number(order["order_id"]) == number)
        outcome = "unique" if len(matched) == 1 else "ambiguous" if matched else "unmatched"
        return {"outcome": outcome, "matched": matched, "span": number}

    @classmethod
    def _product_sourced(cls, ref, quote, message, catalog):
        return (catalog is not None and type(quote) is str and bool(quote.strip()) and quote in message
                and ref["order_id"] in {order["order_id"] for order in catalog["orders"]}
                and cls._mention(ref, catalog, message)["outcome"] != "none")

    @staticmethod
    def item_ref(item, role):
        """The order reference of one business item, or None when its words name no order (see the class docstring)."""
        order_id, quote = item.get("order_id"), item.get("order_quote") or item.get("quote") or ""
        if order_id == "none":
            typed = ORDER_TOKEN.search(quote) or _BARE_NUMBER.search(quote)
            if typed is None:
                return None
            order_id = typed.group()
        return {"order_id": order_id, "role": role, "quote": quote}

    @classmethod
    def read_problem(cls, item, message, catalog, open_card_order_ids):
        """Why a read (own_order_status) cannot be done as proposed, or None (understand.check drops it, K20): its words
        name no order (NO_ORDER: also "current" without an open card), a literal or product order its words do not
        source (ORDER_NOT_SOURCED), or all of the account's orders while the message types an order id of another
        account (FOREIGN_ORDER_TYPED: the message is about that order, not a list of mine). A typed number is sourced
        whichever own orders it fits (B3d: the read offers them, or all own orders)."""
        ref = cls.item_ref(item, "query")
        if ref is None or (ref["order_id"] == "current" and not open_card_order_ids):
            return "NO_ORDER"
        if ref["order_id"] == ALL_MINE:
            owned = {order["order_id"] for order in catalog["orders"]}
            return "FOREIGN_ORDER_TYPED" if any(token not in owned for token in ORDER_TOKEN.findall(message)) else None
        if ref["order_id"] == "current":
            return None
        quote = ref["quote"]
        if not (quote.strip() and quote in message and (cls._order_in_quote(ref["order_id"], quote, message)
                                                         or cls._number_mention(ref, message, catalog) is not None
                                                         or cls._product_sourced(ref, quote, message, catalog))):
            return "ORDER_NOT_SOURCED"
        return None

    def _catalog(self):
        return self.order_catalog(self.store, self.customer_id)

    def _result(self, status, reply, **data):
        snapshot = self._snapshot()
        return {"ok": True, "status": status, "reply": reply,
                "card": snapshot["card"], "return_request": snapshot["return_request"],
                "scope": "simulated_business", "scenario_date": SCENARIO_DATE.isoformat(), **data}

    def _views(self, order_ids=None):
        """Rule views of the current customer's orders (all, newest delivery first)."""
        returns = self.store.owned_return_requests(self.customer_id)
        return [order_view(order, returns.get(order.order_id)) for order in self.store.owned_orders(self.customer_id)
                if order_ids is None or order.order_id in order_ids]

    def _named(self, refs, message, catalog, role):
        """Orders the current message names for ``role``: (named, ambiguous, binding, current, all_mine).

        A typed number that fits no own order sets ``all_mine``: the user picks from all own orders, and
        K5 never stands in for the number they typed (B3d).
        """
        named, ambiguous, product_refs, number_refs, ignored = set(), set(), [], [], 0
        current = all_mine = False
        for ref in refs:
            if ref["role"] != role:
                continue
            order_id, quote = ref["order_id"], ref.get("quote")
            sourced = type(quote) is str and bool(quote.strip()) and quote in message
            number = self._number_mention(ref, message, catalog) if sourced else None
            if order_id == ALL_MINE:
                all_mine = True
            elif order_id == "current":
                current = current or sourced
            elif number is not None:
                # Before the literal check: a number proposed as typed ("1003") is no order id.
                number_refs.append({"quote": quote, "span": number["span"], "role": role,
                                    "proposed_order_id": order_id, "matched_order_ids": number["matched"],
                                    "outcome": number["outcome"]})
                if number["outcome"] == "unique":
                    named.update(number["matched"])
                elif number["outcome"] == "ambiguous":
                    ambiguous.update(number["matched"])
                else:
                    all_mine = True
            elif sourced and self._order_in_quote(order_id, quote, message):
                named.add(order_id)
            else:
                mention = self._mention(ref, catalog, message) if sourced else {"outcome": "none"}
                if mention["outcome"] == "none":
                    ignored += 1
                    continue
                product_refs.append({"quote": quote, "span": mention["span"], "role": role,
                                     "proposed_order_id": order_id, "matched_order_ids": mention["matched"],
                                     "outcome": mention["outcome"]})
                if mention["outcome"] == "unique":
                    named.add(order_id)
                else:
                    ambiguous.update(mention["matched"])
        binding = {"original_refs": refs, "canonical_order_ids": sorted(named)}
        if product_refs:
            binding["product_refs"] = product_refs
        if number_refs:
            binding["number_refs"] = number_refs
        if ignored:
            binding["ignored_refs"] = ignored
        return named, ambiguous, binding, current, all_mine

    def _check_owned(self, named, message, catalog, *, ambiguous=()):
        """Another customer's order, typed by the user, is reported as not found (never confirmed)."""
        owned = {order["order_id"] for order in catalog["orders"]}
        typed_foreign = [token for token in ORDER_TOKEN.findall(message) if token not in owned]
        if set(named) - owned or (not named and not ambiguous and typed_foreign):
            raise ToolError("ORDER_NOT_FOUND", "订单不存在或无权访问。")
        return owned

    def _lookup(self, refs, message, catalog, open_cards):
        orders, ambiguous, binding, current, list_all = self._named(refs, message, catalog, "query")
        if current:
            orders.update(card["order_id"] for card in open_cards)
        if not orders and not ambiguous and not list_all:
            return self._result("clarification_needed", "请提供要查询的模拟订单号。",
                                missing_fields=["query_order_id"], binding=binding)
        self._reserve("get_order")
        # Literal ids keep the all-or-nothing ownership check; mentions and the
        # list only ever come from this customer's own orders.
        for order_id in sorted(orders):
            self.store.owned_order(self.customer_id, order_id)
        if list_all:
            binding["list_scope"] = "all_owned"
            values = self._views()
        else:
            values = sorted(self._views(orders | ambiguous), key=lambda row: row["order_id"])
        reply = "已查询到本人的模拟订单。" if values else "当前账号下没有模拟订单。"
        return self._result("order_found", reply, orders=values,
                            order=values[0] if len(values) == 1 else None, binding=binding)

    def _return(self, refs, message, catalog, open_cards, notes, *, withdrawn=(), later=False):
        named, ambiguous, binding, current, all_mine = self._named(refs, message, catalog, "target")
        owned = self._check_owned(named, message, catalog, ambiguous=ambiguous)
        open_orders = [card["order_id"] for card in open_cards]
        latest = [] if named or ambiguous else self.store.latest_resolved_orders(self.customer_id, self.session_id)
        targets, by, candidates = decide.return_targets(
            named, ambiguous, current=current, all_mine=all_mine, withdrawn=withdrawn,
            open_orders=open_orders, latest_resolved=latest, own=owned)
        binding["order_resolution"] = by
        notes.append({"type": "order_resolution", "by": by,
                      **({"order_ids": targets} if len(targets) > 1 else {"order_id": next(iter(targets), None)})})
        if not targets:
            self._reserve("get_order")
            views = sorted(self._views(set(candidates)),
                           key=lambda row: not (row["return_eligible"] and not row["return_request"]))
            return self._result("clarification_needed", CANDIDATES_RESULT, missing_fields=["order_id"],
                                binding=binding, candidate_orders=views)
        views = {row["order_id"]: row for row in self._views(set(targets))}
        outcomes = {order_id: decide.return_outcome(views[order_id], open_orders, later=later) for order_id in targets}
        if "facts" in outcomes.values():
            self._reserve("get_order")
            return self._result("order_found", RETURN_LATER_RESULT, orders=[views[o] for o in targets],
                                binding=binding, return_later=True)
        if len(targets) > 1:
            return self._several(targets, views, outcomes, binding)
        view, outcome = views[targets[0]], outcomes[targets[0]]
        if outcome == "already_requested":
            self._reserve("get_order")
            return self._result("already_requested", ALREADY_REQUESTED_RESULT, order=view, binding=binding)
        if outcome == "ineligible":
            # No card is prepared; the open cards for other orders stay as they were.
            self._reserve("check_return_eligibility")
            return self._result("ineligible", INELIGIBLE_RESULT, eligible=False, card_unchanged=True,
                                order=view, binding=binding)
        self._reserve("prepare_return")
        prepared = self.store.prepare_cards(*self._args, targets)[0]
        return self._result("awaiting_confirmation", CARD_READY_RESULT, eligible=True, order=view, binding=binding,
                            card=prepared["card"], card_outcome=prepared["outcome"])

    def _several(self, targets, views, outcomes, binding):
        """Several orders named in one message (B3c, D4): one card for each eligible one, up to the limit.

        ``return_outcomes`` keeps each order's row of the table in the named order: created or reshown
        (with the card's action_id), ineligible, already_requested, or not_prepared for an eligible order
        past decide.MAX_AUTO_CARDS (its "申请退货" button stays).
        """
        carded = decide.carded(targets, outcomes)
        prepared = {}
        if carded:
            self._reserve("prepare_return")
            for row in self.store.prepare_cards(*self._args, carded):
                prepared[row["card"]["order_id"]] = row
        else:
            self._reserve("check_return_eligibility")
        rows = []
        for order_id in targets:
            if order_id in prepared:
                rows.append({"order_id": order_id, "outcome": prepared[order_id]["outcome"],
                             "action_id": prepared[order_id]["card"]["action_id"]})
            else:
                outcome = outcomes[order_id]
                rows.append({"order_id": order_id,
                             "outcome": "not_prepared" if outcome in {"prepare", "reshow"} else outcome})
        status = ("awaiting_confirmation" if prepared else
                  "ineligible" if any(row["outcome"] == "ineligible" for row in rows) else "already_requested")
        return self._result(status, SEVERAL_ORDERS_RESULT, eligible=bool(prepared), orders=[views[o] for o in targets],
                            binding=binding, return_outcomes=rows)

    def _withdraw(self, refs, message, catalog, open_cards):
        # A mention that fits several own orders, or a typed number that fits none (all_mine), names no card.
        named, ambiguous, binding, _, all_mine = self._named(refs, message, catalog, "target")
        if named:
            requested = {row["order_id"] for row in self._views(named) if row["return_request"]}
        else:
            latest = self._snapshot()["return_request"]
            requested = {latest["order_id"]} if latest else set()
        outcome, order_ids = decide.withdraw_outcome(named, [card["order_id"] for card in open_cards], requested,
                                                     unclear=bool(ambiguous) or all_mine)
        if outcome == "no_open_card":
            return self._result("no_open_card", NO_OPEN_CARD_RESULT, binding=binding)
        if outcome == "submitted":
            # Decision D4: a submitted request has no undo here; the reply names the after-sales path.
            return self._result("submitted_no_undo", SUBMITTED_NO_UNDO, binding=binding,
                                submitted_order_ids=order_ids)
        if outcome == "choose":
            return self._result("card_choice_needed", CHOOSE_CARD_RESULT, binding=binding,
                                open_card_order_ids=order_ids)
        self._reserve("update_return_request")
        self.store.void_cards(*self._args, [card["action_id"] for card in open_cards if card["order_id"] in order_ids],
                              "chat_withdraw")
        return self._result("card_voided", CARD_VOIDED_MESSAGE, binding=binding, voided_order_id=order_ids[0])

    def apply(self, items, *, rejected=()):
        """The outcomes of a turn's checked business items, in ITEM_ORDER (one per kind and subkind present), each
        tagged with its ``item_kind`` and the ``item_indexes`` of the items it carries out (B7r: the answer's turn items
        read their orders by index); then one blocked outcome when understand.check dropped a return or withdrawal item
        (``rejected``: its rows), and a no_action outcome when there is nothing else. A kind whose execution raises a
        business error gets its own blocked outcome; the other kinds still run. No outcome ever submits a return."""
        results, withdrawn = [], []
        try:
            message = self._current_message()
            catalog = self._catalog()
        except (SessionError, ToolError) as exc:
            return [{**self._rejected(exc), "item_kind": "none"}]
        for kind, subkind in ITEM_ORDER:
            mine = [item for item in items if item["kind"] == kind and item["subkind"] == subkind]
            if not mine:
                continue
            role = "query" if kind == decide.OWN_ORDER_STATUS else "target"
            refs = [ref for ref in (self.item_ref(item, role) for item in mine) if ref is not None]
            notes = []
            try:
                open_cards = self._snapshot()["open_cards"]
                if kind == decide.WITHDRAW:
                    result = self._withdraw(refs, message, catalog, open_cards)
                elif kind == decide.RETURN_REQUEST:
                    result = self._return(refs, message, catalog, open_cards, notes, withdrawn=withdrawn,
                                          later=subkind == decide.LATER)
                else:
                    result = self._lookup(refs, message, catalog, open_cards)
            except (SessionError, ToolError) as exc:
                result = self._rejected(exc)
            if result.get("status") == "card_voided":
                withdrawn.append(result["voided_order_id"])
            if notes:
                result["trace_notes"] = notes
            results.append({**result, "item_kind": kind, "item_indexes": [item["index"] for item in mine]})
        if rejected:
            results.append({**self._rejected(SessionError(rejected[0]["code"], ITEM_REJECTED_RESULT)),
                            "item_kind": rejected[0]["kind"], "item_indexes": [row["index"] for row in rejected]})
        if not results:
            results.append({**self._result("no_action", "", missing_fields=[]), "item_kind": "none"})
        resolved = sorted({order_id for result in results for order_id in self._result_orders(result)})
        if resolved:
            self.store.record_resolved_orders(*self._args, resolved)
        return results

    @staticmethod
    def _result_orders(result):
        """The own orders this turn's result is about: what K5 may use in a later turn."""
        ids = {row["order_id"] for key in ("orders", "candidate_orders") for row in result.get(key) or []
               if type(row) is dict}
        if type(result.get("order")) is dict:
            ids.add(result["order"]["order_id"])
        if result.get("voided_order_id"):
            ids.add(result["voided_order_id"])
        ids.update(result.get("submitted_order_ids") or [])
        return sorted(ids)

    def _rejected(self, exc):
        # A rejected item or a business error leaves every card as it was (plan §4.1: "其他 → 卡不变").
        self._raise_if_stopped(exc)
        return {"ok": False, "status": "blocked", "reply": exc.message,
                "error": {"code": exc.code, "message": exc.message}, "scope": "simulated_business"}
