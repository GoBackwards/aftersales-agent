"""What carries across turns after B3a: the card and the orders each turn resolved (K5), never consent.

A card survives a reopened store and history loss; "the order I meant" comes from the latest completed
turn that resolved exactly one own order (K5) or from the open card, never from a planner pointer into
history; consent is always a quote from the current message (B7: understand.check drops a business item quoting
anything else); a failed turn does not count for K5.
"""
from itertools import count
from pathlib import Path
import tempfile
import unittest

from support_agent import understand
from support_agent.business import BusinessAdapter
from support_agent.sessions import SessionStore


def proposal(message, *, action="request_return", order=None, order_quote=None, history_index=None):
    """The business items of one request (B7): one item quoting ``message``, its order as the user's words name it."""
    kind = {"request_return": "return_request", "withdraw": "withdraw", "lookup_order": "own_order_status"}[action]
    return [{"kind": kind, "subkind": "none", "quote": message, "history_index": history_index,
             "order_quote": order_quote if order_quote is not None else (order or ""), "order_id": order or "none",
             "fee_subject": "none"}]


class GraphBusinessContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "continuity.sqlite3"
        self.store = SessionStore(self.path, clock=lambda: 1000)
        self.addCleanup(lambda: self.store.close())
        self.session = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def apply(self, text, plan, *, status="completed", rejected=()):
        run = self.store.begin_turn("alice", self.session, "turn-" + str(next(self.ids)), text)
        results = BusinessAdapter(self.store, "alice", self.session, run["run_id"]).apply(
            [{"index": index, **item} for index, item in enumerate(plan, 1)], rejected=rejected)
        self.store.finish_turn("alice", self.session, run["run_id"],
            [{"role": "user", "content": text}], {"reply": results[-1].get("reply", ""), "business": results,
                                                  "status": status})
        return results[0] if len(results) == 1 else results

    def lookup(self, text, order, quote):
        return self.apply(text, proposal(quote, action="lookup_order", order=order, order_quote=quote))

    def snapshot(self):
        return self.store.get_session("alice", self.session)

    def test_a_card_survives_a_reopened_store_without_chat_history(self):
        text = "请准备 O1004 的退货"
        card = self.apply(text, proposal(text, order="O1004"))["card"]
        self.store.close()
        self.store = SessionStore(self.path, clock=lambda: 1000)
        self.assertEqual(self.snapshot()["card"], card)
        confirmed = self.store.confirm_card("alice", self.session, card["action_id"], card["version"],
                                            "size_or_style", "", "after-reopen")
        self.assertEqual(confirmed["return_request"]["order_id"], "O1004")

    def test_k5_reads_the_latest_turn_that_resolved_one_order(self):
        self.lookup("保温杯那单能退吗", "O1001", "保温杯那单")
        self.lookup("收纳盒那单呢", "O1004", "收纳盒那单")
        result = self.apply("就退这个吧", proposal("退这个吧"))
        self.assertEqual(result["card"]["order_id"], "O1004")

    def test_k5_does_not_guess_after_a_turn_that_listed_several_orders(self):
        self.lookup("保温杯那单能退吗", "O1001", "保温杯那单")
        listing = self.apply("我名下有哪些订单", proposal("我名下有哪些订单", action="lookup_order", order="all_mine"))
        self.assertEqual(listing["binding"]["list_scope"], "all_owned")
        result = self.apply("退一单", proposal("退一单"))
        self.assertEqual(result["status"], "clarification_needed")
        self.assertIsNone(self.snapshot()["card"])

    def test_a_turn_without_orders_does_not_reset_k5(self):
        self.lookup("收纳盒那单能退吗", "O1004", "收纳盒那单")
        self.apply("运费一般谁出", [])
        result = self.apply("那就退吧", proposal("退吧"))
        self.assertEqual(result["card"]["order_id"], "O1004")

    def test_a_failed_turn_does_not_count_for_k5(self):
        self.lookup("保温杯那单能退吗", "O1001", "保温杯那单")
        failed = self.apply("收纳盒那单呢", proposal("收纳盒那单", action="lookup_order", order="O1004",
                                                order_quote="收纳盒那单"), status="model_error")
        self.assertEqual(failed["status"], "order_found")
        result = self.apply("退了吧", proposal("退了吧"))
        self.assertEqual(result["card"]["order_id"], "O1001")

    def test_history_cannot_supply_the_consent_quote(self):
        self.lookup("收纳盒那单能退吗", "O1004", "收纳盒那单")
        history = [{"role": "user", "content": "收纳盒那单能退吗"}, {"role": "assistant", "content": "可以退。"}]
        catalog = BusinessAdapter.order_catalog(self.store, "alice")
        for items, code in ((proposal("收纳盒那单能退吗"), "QUOTE_NOT_IN_MESSAGE"),
                            (proposal("收纳盒那单能退吗", history_index=0), "CURRENT_MESSAGE_ONLY")):
            with self.subTest(code=code):
                plan = understand.parse({"goal": "退货", "items": items, "order_stage": "unknown",
                                         "fee_flow": "unknown"}, ["O1001", "O1004"])
                kept, rejected = understand.check(plan, "好的", history, catalog, [])
                self.assertEqual((kept, [row["code"] for row in rejected]), ([], [code]))
                result = self.apply("好的", kept, rejected=rejected)
                self.assertEqual(result["error"]["code"], code)
                self.assertIsNone(self.snapshot()["card"])

    def test_a_planner_pointer_into_history_is_replaced_by_k5(self):
        self.lookup("收纳盒那单能退吗", "O1004", "收纳盒那单")
        # The plan names O1001 with words from an earlier turn; K5 decides instead.
        result = self.apply("退了吧", proposal("退了吧", order="O1001", order_quote="保温杯"))
        self.assertEqual(result["card"]["order_id"], "O1004")
        self.assertEqual(result["binding"]["ignored_refs"], 1)

    def test_a_read_still_needs_its_own_words(self):
        catalog = BusinessAdapter.order_catalog(self.store, "alice")
        item = proposal("帮我看一下", action="lookup_order", order="O1004", order_quote="收纳盒")[0]
        self.assertEqual(BusinessAdapter.read_problem(item, "帮我看一下", catalog, []), "ORDER_NOT_SOURCED")

    def test_a_return_with_nothing_to_go_on_offers_the_orders_and_prepares_nothing(self):
        text = "继续办理"
        result = self.apply(text, proposal(text))
        self.assertEqual(result["status"], "clarification_needed")
        self.assertIsNone(self.snapshot()["card"])

    def test_another_identity_order_stays_hidden_across_turns(self):
        self.lookup("收纳盒那单能退吗", "O1004", "收纳盒那单")
        result = self.apply("改退 O2001", proposal("改退", order="O2001"))
        self.assertEqual(result["error"]["code"], "ORDER_NOT_FOUND")
        self.assertIsNone(self.snapshot()["card"])

    def test_cancelling_without_a_running_turn_keeps_the_card_and_the_session_continues(self):
        # B3b: cancel controls a run only; the same card is shown again on the next request for that order.
        text = "请准备 O1004 的退货"
        card = self.apply(text, proposal(text, order="O1004"))["card"]
        self.store.cancel("alice", self.session, "cancel-it")
        self.assertEqual(self.snapshot()["card"], card)
        again = self.apply("还是退 O1004 吧", proposal("还是退", order="O1004"))
        self.assertEqual(again["card_outcome"], "reshown")
        self.assertEqual((again["card"]["action_id"], again["card"]["version"]), (card["action_id"], card["version"]))


if __name__ == "__main__":
    unittest.main()
