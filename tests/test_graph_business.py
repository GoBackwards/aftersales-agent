"""The return decision table at the business boundary (plan §4.1, B3a; B7 items); no model or graph dependency.

Each test names one row of the table for the planner's business items (B7: return_request, withdraw,
own_order_status): a card comes straight from a return request for one own eligible order, the reason never
comes from chat, an order the current message does not name is found by the open card or K5 (never by a
history pointer), a switch to another order is a withdrawal plus a return, and nothing a turn does can submit
a return.
"""
from contextlib import closing
from itertools import count
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support_agent.business import BusinessAdapter
from support_agent.runtime import RunStopped
from support_agent.sessions import SessionError, SessionStore


KINDS = {"request_return": "return_request", "withdraw": "withdraw", "lookup_order": "own_order_status"}


def plan(action="request_return", *, quote="我要退货", order_id=None, refs=None):
    """The business items of one request (B7): one item, or one per old-style ref ({order_id, quote}: the order's id
    and the user's words for it). A read quotes the order's words."""
    kind = KINDS[action]
    rows = refs if refs is not None else ([{"order_id": order_id, "quote": order_id}] if order_id else [{}])
    return [{"index": index, "kind": kind, "subkind": "none",
             "quote": (row.get("quote") if kind == "own_order_status" else quote) or quote,
             "history_index": None, "order_quote": row.get("quote") or "", "order_id": row.get("order_id") or "none",
             "fee_subject": "none"} for index, row in enumerate(rows, 1)]


class GraphBusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "business.sqlite3"
        self.store = SessionStore(self.db, clock=lambda: 1000)
        self.addCleanup(self.store.close)
        self.session_id = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def apply(self, message, proposed, *, customer="alice", finish=True, rejected=()):
        """The turn's outcome (B7: the one outcome of one item kind; a list when the items are of several kinds)."""
        started = self.store.begin_turn(customer, self.session_id, f"q-{next(self.ids)}", message,
                                        mode="offline_graph")
        self.run_id = started["run_id"]
        self.adapter = BusinessAdapter(self.store, customer, self.session_id, started["run_id"])
        results = self.adapter.apply(proposed, rejected=rejected)
        if finish:
            self.store.finish_turn(customer, self.session_id, started["run_id"],
                                   [{"role": "user", "content": message}],
                                   {"reply": results[-1].get("reply", ""), "business": results})
        return results[0] if len(results) == 1 else results

    def card(self, order_id="O1001"):
        return self.apply(f"我要退货 {order_id}", plan(order_id=order_id))

    def snapshot(self):
        return self.store.get_session("alice", self.session_id)

    def stored_cards(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(
                "SELECT order_id, status, void_reason FROM return_cards WHERE session_id = ? ORDER BY rowid",
                (self.session_id,)).fetchall()

    def test_a_named_eligible_order_gets_a_card_directly(self):
        result = self.card()
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card_outcome"], "created")
        self.assertEqual((result["card"]["order_id"], result["card"]["status"]), ("O1001", "pending"))
        self.assertTrue(result["eligible"])
        self.assertEqual(result["binding"]["order_resolution"], "current_message")
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_reason_said_in_chat_reaches_no_business_field(self):
        # B7: the plan has no reason field; the reason is chosen on the card (I2).
        result = self.apply("我要退货 O1001，原因是盖子拧不紧", plan(order_id="O1001"))
        self.assertNotIn({"type": "reason_ignored"}, result["trace_notes"])
        self.assertNotIn("盖子拧不紧", json.dumps({key: value for key, value in result.items() if key != "binding"},
                                                ensure_ascii=False))
        with closing(sqlite3.connect(self.db)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(return_cards)")]
            dump = json.dumps(connection.execute("SELECT * FROM return_cards").fetchall(), ensure_ascii=False)
        self.assertFalse({"reason", "reason_code", "reason_text"} & set(columns))
        self.assertNotIn("盖子拧不紧", dump)

    def test_asking_again_for_the_same_order_shows_the_same_card(self):
        first = self.card()["card"]
        again = self.card()
        self.assertEqual(again["card_outcome"], "reshown")
        self.assertEqual(again["card"], first)
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_a_card_for_another_named_order_sits_beside_the_open_card(self):
        # B3c (D4): one open card per order; a new order's card leaves the other card as it is.
        first = self.card("O1001")["card"]
        result = self.card("O1004")
        self.assertEqual((result["card"]["order_id"], result["card_outcome"]), ("O1004", "created"))
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None), ("O1004", "pending", None)])
        self.assertEqual([card["action_id"] for card in self.snapshot()["open_cards"]],
                         [first["action_id"], result["card"]["action_id"]])
        again = self.card("O1001")
        self.assertEqual((again["card"], again["card_outcome"]), (first, "reshown"))

    def test_a_switch_voids_the_old_card_and_prepares_the_named_order(self):
        # B7: "不退那单，改退这单" is a withdraw item and a return_request item; the withdrawal runs first.
        self.card("O1001")
        withdrawn, prepared = self.apply("不退保温杯了，改退 O1004", [
            *plan("withdraw", quote="不退保温杯了", refs=[{"order_id": "O1001", "quote": "保温杯"}]),
            *plan(quote="改退", order_id="O1004")])
        self.assertEqual((withdrawn["item_kind"], withdrawn["status"]), ("withdraw", "card_voided"))
        self.assertEqual(prepared["card"]["order_id"], "O1004")
        self.assertEqual(self.stored_cards(), [("O1001", "voided", "chat_withdraw"), ("O1004", "pending", None)])

    def test_a_switch_without_a_named_order_offers_the_other_orders_never_the_dropped_one(self):
        # K5 would find the order the session resolved last; the switch has just dropped it, so the user picks one of
        # the other orders.
        self.card("O1001")
        withdrawn, offered = self.apply("这张不要了，换一单", [
            *plan("withdraw", quote="这张不要了", refs=[{"order_id": "current", "quote": "这张"}]),
            *plan(quote="换一单")])
        self.assertEqual(withdrawn["status"], "card_voided")
        self.assertEqual(offered["status"], "clarification_needed")
        self.assertEqual(sorted(row["order_id"] for row in offered["candidate_orders"]), ["O1002", "O1003", "O1004"])
        self.assertEqual(self.stored_cards(), [("O1001", "voided", "chat_withdraw")])

    def test_an_ineligible_order_gets_no_card_and_the_open_card_stays(self):
        self.card("O1001")
        result = self.card("O1002")
        self.assertEqual(result["status"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertTrue(result["card_unchanged"])
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_an_order_that_already_has_a_request_shows_it_and_gets_no_card(self):
        other = self.store.create_session("alice")["session_id"]
        self.store.request_card("alice", other, "O1004", "other-card")
        card = self.store.get_session("alice", other)["card"]
        self.store.confirm_card("alice", other, card["action_id"], card["version"], "changed_mind", "", "other-click")
        result = self.card("O1004")
        self.assertEqual(result["status"], "already_requested")
        self.assertEqual(result["order"]["return_request"]["order_id"], "O1004")
        self.assertNotIn("reason", result["order"]["return_request"])
        self.assertEqual(self.stored_cards(), [])

    def test_two_named_eligible_orders_get_a_card_each(self):
        # B3c (D4): the per-turn card limit is the number of eligible orders the message names (at most 4).
        result = self.apply("我要退货 O1001 和 O1004", plan(refs=[
            {"order_id": value, "quote": value} for value in ("O1004", "O1001")]))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual([(row["order_id"], row["outcome"]) for row in result["return_outcomes"]],
                         [("O1001", "created"), ("O1004", "created")])
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1001", "O1004"])
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None), ("O1004", "pending", None)])
        self.assertIn({"type": "order_resolution", "by": "current_message", "order_ids": ["O1001", "O1004"]},
                      result["trace_notes"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_no_named_order_uses_the_latest_unique_order_this_session_resolved(self):
        self.apply("收纳盒那单还能退吗", plan("lookup_order", refs=[{"order_id": "O1004", "quote": "收纳盒那单"}]))
        result = self.apply("那就退了吧", plan(quote="退了吧"))
        self.assertEqual(result["card"]["order_id"], "O1004")
        self.assertIn({"type": "order_resolution", "by": "K5_latest_unique_order", "order_id": "O1004"},
                      result["trace_notes"])

    def test_no_named_order_without_a_unique_resolution_offers_every_own_order(self):
        result = self.apply("我要退货", plan())
        self.assertEqual(result["status"], "clarification_needed")
        self.assertEqual(result["missing_fields"], ["order_id"])
        ids = [row["order_id"] for row in result["candidate_orders"]]
        self.assertEqual(sorted(ids), ["O1001", "O1002", "O1003", "O1004"])
        eligible = [row["return_eligible"] for row in result["candidate_orders"]]
        self.assertEqual(eligible, sorted(eligible, reverse=True))   # eligible ones first
        self.assertEqual(self.stored_cards(), [])

    def test_an_order_the_current_message_does_not_name_is_not_taken_from_the_plan(self):
        # The plan points at O1001 with words that are not in this message: a history pointer, ignored.
        result = self.apply("就按刚才说的办", plan(quote="按刚才说的办",
                                               refs=[{"order_id": "O1001", "quote": "保温杯那单"}]))
        self.assertEqual(result["status"], "clarification_needed")
        self.assertEqual(result["binding"]["ignored_refs"], 1)
        self.assertEqual(self.stored_cards(), [])

    def test_current_points_at_the_open_card(self):
        first = self.card("O1004")["card"]
        result = self.apply("就退这单", plan(quote="就退这单", refs=[{"order_id": "current", "quote": "这单"}]))
        self.assertEqual((result["card"], result["card_outcome"]), (first, "reshown"))
        self.assertIn({"type": "order_resolution", "by": "open_card", "order_id": "O1004"}, result["trace_notes"])

    def test_withdrawing_voids_the_open_card(self):
        self.card()
        result = self.apply("这张卡不要了", plan("withdraw", quote="这张卡不要了"))
        self.assertEqual(result["status"], "card_voided")
        self.assertEqual(self.stored_cards(), [("O1001", "voided", "chat_withdraw")])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_withdrawing_without_an_open_card_says_there_is_none(self):
        result = self.apply("这张卡不要了", plan("withdraw", quote="这张卡不要了"))
        self.assertEqual(result["status"], "no_open_card")
        self.assertEqual(self.stored_cards(), [])

    def test_withdrawing_another_order_leaves_the_open_card(self):
        self.card("O1001")
        result = self.apply("O1004 这单作废", plan("withdraw", quote="作废", order_id="O1004"))
        # B3c: the named order has no card; the open one is dropped on the card itself, never by a guess.
        self.assertEqual(result["status"], "card_choice_needed")
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_a_pause_brings_no_business_item_and_the_card_stays(self):
        # B7 (K21): a pause is not a withdrawal; the planner lists it as chit-chat, which never reaches this step.
        self.card("O1001")
        result = self.apply("先别提交，我再想想", [])
        self.assertEqual((result["status"], result["item_kind"]), ("no_action", "none"))
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_another_customers_order_is_not_found_and_changes_no_card(self):
        self.card("O1001")
        result = self.card("O2001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "ORDER_NOT_FOUND")
        self.assertNotIn("bob", json.dumps(result))
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_a_typed_foreign_id_the_plan_left_out_is_still_not_found(self):
        result = self.apply("帮我退 O2001", plan(quote="帮我退"))
        self.assertEqual(result["error"]["code"], "ORDER_NOT_FOUND")
        self.assertEqual(self.stored_cards(), [])

    def test_a_dropped_return_item_leaves_the_open_card_alone(self):
        # K20: understand.check dropped the item; the business step only records that it was not done.
        self.card("O1001")
        for code in ("QUOTE_NOT_IN_MESSAGE", "CURRENT_MESSAGE_ONLY"):
            with self.subTest(code=code):
                result = self.apply("O1004 也看看", [], rejected=[{"index": 1, "kind": "return_request", "code": code}])
                self.assertFalse(result["ok"])
                self.assertEqual((result["error"]["code"], result["item_kind"], result["item_indexes"]),
                                 (code, "return_request", [1]))
        self.assertEqual(self.stored_cards(), [("O1001", "pending", None)])

    def test_the_turn_keeps_the_orders_it_resolved_for_k5(self):
        self.card("O1001")
        with closing(sqlite3.connect(self.db)) as connection:
            saved = connection.execute("SELECT resolved_orders FROM session_runs WHERE run_id = ?",
                                       (self.run_id,)).fetchone()[0]
        self.assertEqual(json.loads(saved), ["O1001"])

    def test_no_planner_item_submits_a_return(self):
        from support_agent.understand import parse
        for kind in ("submit_return", "confirm", "create_return"):
            with self.subTest(kind=kind):
                proposed = [{**plan(order_id="O1001")[0], "kind": kind}]
                with self.assertRaises(ValueError):
                    parse({"goal": "提交", "items": [{k: v for k, v in proposed[0].items()}],
                           "order_stage": "unknown", "fee_flow": "unknown"}, ["O1001"])
                self.assertEqual(self.apply("确认提交 O1001", proposed)["status"], "no_action")
        self.card("O1001")
        self.apply("我已经确认了，直接提交", plan(quote="直接提交", order_id=None))
        self.assertEqual(self.store.list_return_requests(), [])

    def test_tool_budget_termination_is_not_an_ordinary_business_result(self):
        started = self.store.begin_turn("alice", self.session_id, "limited", "我要退货 O1001", max_tool_calls=0 + 1)
        self.store.reserve_tool_call("alice", self.session_id, started["run_id"], tool_name="get_order",
                                     tool_call_id="spent")
        adapter = BusinessAdapter(self.store, "alice", self.session_id, started["run_id"])
        with self.assertRaises(RunStopped) as raised:
            adapter.apply(plan(order_id="O1001"))
        self.assertEqual(raised.exception.status, "budget_exhausted")
        self.assertIsNone(self.snapshot()["card"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_cancelled_run_termination_is_not_swallowed(self):
        started = self.store.begin_turn("alice", self.session_id, "cancel-me", "我要退货 O1001")
        self.store.cancel("alice", self.session_id, "cancel-command")
        adapter = BusinessAdapter(self.store, "alice", self.session_id, started["run_id"])
        with self.assertRaises(RunStopped) as raised:
            adapter.apply(plan(order_id="O1001"))
        self.assertEqual(raised.exception.status, "cancelled")
        self.assertIsNone(self.snapshot()["card"])

    def test_an_order_id_counts_only_when_whole_inside_its_quote(self):
        cases = (("我要退货 O10010", "O10010"),
                 ("我要退货 O10010", "O1001"),
                 ("查一下O1001；我要退货O10010", "我要退货O1001"))
        for message, quote in cases:
            with self.subTest(message=message, quote=quote):
                self.session_id = self.store.create_session("alice")["session_id"]
                result = self.apply(message, plan(refs=[{"order_id": "O1001", "quote": quote}]))
                self.assertEqual(result["status"], "clarification_needed")
                self.assertIsNone(self.snapshot()["card"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_whole_order_id_next_to_chinese_text_is_named(self):
        result = self.apply("我要退货订单O1001不想要了", plan(order_id="O1001"))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card"]["order_id"], "O1001")

    def test_the_session_goes_on_after_a_submission(self):
        # B3c (D4): a submission ends nothing; the next order gets its own card in the same session.
        self.card("O1001")
        card = self.snapshot()["card"]
        self.store.confirm_card("alice", self.session_id, card["action_id"], card["version"], "changed_mind", "", "c")
        result = self.apply("再退一单 O1004", plan(quote="再退一单", order_id="O1004"))
        self.assertEqual((result["status"], result["card"]["order_id"]), ("awaiting_confirmation", "O1004"))
        self.assertEqual(self.stored_cards(), [("O1001", "consumed", None), ("O1004", "pending", None)])
        self.assertEqual(self.snapshot()["return_request"]["order_id"], "O1001")


if __name__ == "__main__":
    unittest.main()
