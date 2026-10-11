"""Ticket ownership, failure-card offers, withdrawal suppression, recovery, and page declarations.

All records are synthetic; checks exercise the current store, HTTP presentation and static client.
"""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

from support_agent.product_api import ENVIRONMENT_NOTICE
from support_agent.service import _browser_history
from support_agent.sessions import FAILED_TURN_TEXT, RETRY_STEP, SessionError, SessionStore
from support_agent.tickets import (FAILURE_APPENDED, TECHNICAL_FAILURE_LEAD, TICKET_APPENDED, TICKET_OPENED,
                                   technical_failure_text)

APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 9000.0
        self.store = SessionStore(Path(self.folder.name) / "b4d.sqlite3", clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.sid = self.store.create_session("alice")["session_id"]
        self.requests = 0

    def request_id(self, kind):
        self.requests += 1
        return f"{kind}-{self.requests}"

    def failed_turn(self, status="model_error"):
        request_id = self.request_id("turn")
        run = self.store.begin_turn("alice", self.sid, request_id, "随口问一件事")["run_id"]
        self.store.fail_turn("alice", self.sid, run, FAILED_TURN_TEXT, response={"status": status})
        return self.store.begin_turn("alice", self.sid, request_id, "随口问一件事")["response"]

    def completed_turn(self, goal=None, resolved=()):
        request_id = self.request_id("turn")
        run = self.store.begin_turn("alice", self.sid, request_id, "随口问一件事")["run_id"]
        if resolved:
            self.store.record_resolved_orders("alice", self.sid, run, list(resolved))
        response = {"status": "completed", "reply": "这件事这样看。"}
        if goal is not None:
            response["graph"] = {"plan": {"goal": goal}}
        return self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "随口问一件事"}], response)

    def handoff(self):
        return self.store.request_handoff("alice", self.sid, self.request_id("button"))

    def withdraw(self, ticket_id):
        return self.store.withdraw_ticket("alice", self.sid, ticket_id, self.request_id("withdraw"))

    def statuses(self):
        return [(ticket["short_no"], ticket["status"]) for ticket in self.store.list_tickets("alice")]


class TicketSummaryOwnOrdersTests(StoreCase):
    def test_the_need_masks_order_ids_that_are_not_the_customers_own(self):
        self.completed_turn(goal="对照 O1001 与 O2001、o1003、Z7777 的售后进度，另记一笔 2001 元的付款")
        self.handoff()
        summary = self.store.list_tickets("alice")[0]["summary"]
        # Own ids stay; another customer's id, a differently written one and an unknown one are masked; a bare
        # number is no order id (B3e) and stays as written.
        self.assertEqual(summary["user_need"]["text"], "对照 O1001 与 ****、****、**** 的售后进度，另记一笔 2001 元的付款")
        self.assertNotIn("O2001", json.dumps(summary, ensure_ascii=False))

    def test_the_same_id_stays_in_its_owners_summary(self):
        bob = self.store.create_session("bob")["session_id"]
        run = self.store.begin_turn("bob", bob, "bob-turn", "随口问一件事")["run_id"]
        self.store.finish_turn("bob", bob, run, [{"role": "user", "content": "随口问一件事"}],
                               {"status": "completed", "reply": "好的。", "graph": {"plan": {"goal": "查看 O2001 的进度"}}})
        self.store.request_handoff("bob", bob, "bob-button")
        self.assertEqual(self.store.list_tickets("bob")[0]["summary"]["user_need"]["text"], "查看 O2001 的进度")

    def test_the_verified_facts_list_only_the_customers_own_orders(self):
        self.completed_turn(goal="对照几笔订单", resolved=["O1004", "O2001"])
        self.handoff()
        facts = self.store.list_tickets("alice")[0]["summary"]["verified_facts"]
        self.assertEqual([order["order_id"] for order in facts["orders"]], ["O1004"])

    def test_the_module_states_what_the_summary_holds(self):
        source = (APP / "support_agent" / "tickets.py").read_text(encoding="utf-8")
        self.assertNotIn("another customer's order, a prompt", source)   # the old, unqualified claim
        self.assertIn("ORDER_TOKEN", source)
        # One definition of a typed order id in the product (the offline demo model keeps its own stand-in copy).
        defined = [path.name for path in (APP / "support_agent").rglob("*.py")
                   if re.search(r"^_?ORDER_TOKEN = re\.compile", path.read_text(encoding="utf-8"), re.M)]
        self.assertEqual(defined, ["domain.py"])


class FailureCardOfferTests(StoreCase):
    def test_a_single_failure_offers_the_handoff_click_beside_the_retry(self):
        failed = self.failed_turn()
        self.assertEqual(failed["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertEqual(failed["reply"], FAILED_TURN_TEXT + "\n" + RETRY_STEP)   # the copy is unchanged
        # The card's "找人工" is the button's click: a user request that opens the ticket ...
        opened = self.handoff()
        self.assertEqual(opened["ticket_outcome"], "opened")
        # ... and the next failure of the run appends the technical failure to it.
        second = self.failed_turn()
        self.assertEqual(second["next_step"], {"action": "ticket", "consecutive_failures": 2,
                                               "ticket_outcome": "appended"})
        events = [(event["kind"], event["trigger"]) for event in self.store.list_tickets("alice")[0]["events"]]
        self.assertEqual(events, [("opened", "user_request"), ("appended", "technical_failure")])

    def test_no_offer_while_a_ticket_is_open(self):
        self.handoff()
        self.assertEqual(self.failed_turn()["next_step"],
                         {"action": "retry", "consecutive_failures": 1, "offer_handoff": False})

    def test_a_ticket_step_carries_no_offer(self):
        self.failed_turn()
        second = self.failed_turn()
        self.assertEqual(second["next_step"]["action"], "ticket")
        self.assertNotIn("offer_handoff", second["next_step"])


class WithdrawalDuringFailuresTests(StoreCase):
    def test_a_ticket_withdrawn_between_two_failures_is_not_reopened_by_the_second(self):
        self.failed_turn()
        ticket_id = self.handoff()["ticket"]["ticket_id"]
        self.withdraw(ticket_id)
        for failures in (2, 3):
            later = self.failed_turn()
            self.assertEqual(later["next_step"], {"action": "retry", "consecutive_failures": failures,
                                                  "offer_handoff": False})
            self.assertNotIn("ticket_card", later)
            self.assertTrue(later["reply"].endswith(RETRY_STEP))
        self.assertEqual(self.statuses(), [("H-0001", "cancelled")])
        # A completed turn ends the run: the next failure offers the click again, the one after opens a ticket.
        self.completed_turn()
        self.assertTrue(self.failed_turn()["next_step"]["offer_handoff"])
        self.assertEqual(self.failed_turn()["ticket_card"]["short_no"], "H-0002")

    def test_asking_again_after_the_withdrawal_lets_the_run_append(self):
        self.failed_turn()
        self.withdraw(self.handoff()["ticket"]["ticket_id"])
        self.failed_turn()
        self.assertEqual(self.handoff()["ticket"]["short_no"], "H-0002")   # the customer asks again
        third = self.failed_turn()
        self.assertEqual((third["next_step"]["action"], third["next_step"]["ticket_outcome"],
                          third["ticket_card"]["short_no"]), ("ticket", "appended", "H-0002"))

    def test_a_withdrawal_before_the_run_began_does_not_hold_it_back(self):
        self.withdraw(self.handoff()["ticket"]["ticket_id"])
        self.assertTrue(self.failed_turn()["next_step"]["offer_handoff"])
        second = self.failed_turn()
        self.assertEqual((second["next_step"]["ticket_outcome"], second["ticket_card"]["short_no"]),
                         ("opened", "H-0002"))

    def test_a_withdrawal_while_the_first_failing_turn_runs_belongs_to_its_run(self):
        run = self.store.begin_turn("alice", self.sid, "turn-busy", "随口问一件事")["run_id"]
        self.withdraw(self.handoff()["ticket"]["ticket_id"])   # both clicks are allowed while the turn runs
        self.store.fail_turn("alice", self.sid, run, FAILED_TURN_TEXT, response={"status": "timed_out"})
        first = self.store.begin_turn("alice", self.sid, "turn-busy", "随口问一件事")["response"]
        self.assertEqual(first["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": False})
        self.assertEqual(self.failed_turn()["next_step"]["action"], "retry")
        self.assertEqual(self.statuses(), [("H-0001", "cancelled")])

    def test_a_refused_withdrawal_does_not_count(self):
        self.failed_turn()
        ticket_id = self.failed_turn()["ticket_card"]["ticket_id"]
        self.withdraw(ticket_id)
        self.completed_turn()
        self.failed_turn()
        with self.assertRaises(SessionError) as refused:
            self.withdraw(ticket_id)   # already closed: refused, saved as a failed click
        self.assertEqual(refused.exception.code, "TICKET_CLOSED")
        self.assertEqual(self.failed_turn()["ticket_card"]["short_no"], "H-0002")


class FailureTicketCopyTests(unittest.TestCase):
    def test_the_lead_states_no_count_and_the_failure_append_names_no_request(self):
        self.assertIsNone(re.search(r"[0-9０-９两二三四五六七八九十几]|连续", TECHNICAL_FAILURE_LEAD))
        view = {"short_no": "H-0005", "status_label": "待处理"}
        appended = technical_failure_text(view, "appended")
        self.assertNotIn("请求", appended)
        self.assertTrue(appended.startswith(TECHNICAL_FAILURE_LEAD + FAILURE_APPENDED.format(short_no="H-0005",
                                                                                            status="待处理")))
        self.assertTrue(technical_failure_text(view, "opened").startswith(
            TECHNICAL_FAILURE_LEAD + TICKET_OPENED.format(short_no="H-0005", status="待处理")))
        # The button's own append still answers the customer's request.
        self.assertEqual(TICKET_APPENDED, "这次请求" + FAILURE_APPENDED)


class RecoveryNextStepTests(StoreCase):
    def interrupt(self):
        request_id = self.request_id("turn")
        self.store.begin_turn("alice", self.sid, request_id, "随口问一件事")
        recovered = self.store.recover("alice", self.sid, self.request_id("recover"))
        saved = self.store.begin_turn("alice", self.sid, request_id, "随口问一件事")["response"]
        return recovered, saved

    def test_recovery_returns_the_interrupted_turns_next_step_and_ticket_card(self):
        recovered, saved = self.interrupt()
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(recovered["next_step"], saved["next_step"])
        self.assertEqual(recovered["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertNotIn("ticket_card", recovered)
        recovered, saved = self.interrupt()
        self.assertEqual((recovered["next_step"], recovered["ticket_card"]), (saved["next_step"], saved["ticket_card"]))
        self.assertEqual(recovered["ticket_card"]["short_no"], "H-0001")
        self.assertEqual(recovered["last_reply"], saved["reply"])

    def test_the_history_keeps_the_step_with_the_interrupted_turn(self):
        self.failed_turn()
        self.interrupt()
        entries = _browser_history(self.store, "alice", self.sid)["entries"]
        self.assertEqual([entry["entry"] for entry in entries], ["turn", "turn", "recover"])
        self.assertEqual(entries[1]["response"]["ticket_card"]["short_no"], "H-0001")
        self.assertNotIn("ticket_card", entries[2]["response"])
        self.assertNotIn("next_step", entries[2]["response"])

    def test_a_cancelled_turn_still_returns_no_step(self):
        self.store.begin_turn("alice", self.sid, "turn-c", "随口问一件事")
        cancelled = self.store.cancel("alice", self.sid, "cancel-c")
        self.assertNotIn("next_step", cancelled)
        self.assertNotIn("ticket_card", cancelled)


class NoticeTests(unittest.TestCase):
    def test_the_notice_names_only_records_the_page_shows(self):
        self.assertIn("账号、订单、退货申请和人工工单都是模拟数据", ENVIRONMENT_NOTICE)
        self.assertIn("不会有真人跟进或联系你", ENVIRONMENT_NOTICE)
        self.assertNotIn("处理记录", ENVIRONMENT_NOTICE)
        for name in ("chat.js", "chat.html"):
            self.assertNotIn("处理记录", (APP / "support_agent" / "web" / name).read_text(encoding="utf-8"))






class FailureCardPageTests(unittest.TestCase):
    def setUp(self):
        chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")
        start = chat.index("function failureStep(")
        self.step = chat[start:chat.index("\n  }\n", start)]

    def test_the_card_draws_the_declared_offer_with_the_buttons_own_click(self):
        self.assertIn("step.offer_handoff === true", self.step)
        self.assertIn('text: "找人工"', self.step)
        self.assertIn('ticketAction("handoff")', self.step)          # the same /handoff click as the top bar
        self.assertIn("state.entries[state.entries.length - 1]", self.step)   # gone once a click follows the turn
        self.assertLess(self.step.index('text: "重试"'), self.step.index('text: "找人工"'))


if __name__ == "__main__":
    unittest.main()
