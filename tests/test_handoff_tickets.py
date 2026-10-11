"""Human-service tickets and the "找人工" button (stage B4a, first-principles plan §6.1–§6.5, standard §9.4–§9.6).

Behaviours: the button opens one ticket per session without a model call, also while a turn runs, and a later
press appends an event to the open ticket instead of opening another; the reply states the ticket's number and
status as recorded, that it can be withdrawn and a channel of the original platform that works now, promises no
time or follow-up and, with an open return card, says the card stays valid and still needs the customer's click;
the card itself is never touched; the owner can withdraw the open ticket once, and only the system changes a
status; tickets are numbered per customer, listed only to their owner and refused to anyone else on every
endpoint; the summary is written once at creation and holds no "other" reason text, no other customer's order
and no long digit run; one failed turn opens no ticket (two in a row do, B4b: tests/test_failure_next_step.py);
the T7 counts back a ticket sentence with the ticket row. Offline: SQLite, the HTTP service with no model, static checks of the page. Engineering
evidence, not task success. The wording here is written for these tests.
"""
from contextlib import closing
import http.client
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest

from support_agent.guards import A0_WORDS
from support_agent.service import create_server
from support_agent.sessions import SCHEMA_VERSION, SessionError, SessionStore
from support_agent.tickets import (CARD_STILL_VALID, PLATFORM_CHANNEL, STATUS_LABELS, TICKET_APPENDED, TICKET_OPENED,
                                   TICKET_WITHDRAWN)

APP = Path(__file__).resolve().parents[1]
# The follow-up promises a ticket sentence must not make (plan §6.4, G2), as in test_graph_production_voice.
FOLLOW_UP_PROMISES = re.compile(r"\d+\s*(?:小时|个工作日)内|尽快|稍后|会有人(?:联系|跟进)|排队")


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "tickets.sqlite3"
        self.now = 5000.0
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.sid = self.store.create_session("alice")["session_id"]

    def error(self, code, function, *args):
        with self.assertRaises(SessionError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def rows(self, sql, *args):
        return [tuple(row) for row in self.store._connection.execute(sql, args)]

    def card_turn(self, order_id="O1001", request="turn-card"):
        run = self.store.begin_turn("alice", self.sid, request, "这单想退回")["run_id"]
        card = self.store.prepare_card("alice", self.sid, run, order_id)["card"]
        self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "这单想退回"},
                                                        {"role": "assistant", "content": "确认卡在下面"}],
                               {"status": "completed", "reply": "确认卡在下面"})
        return card

    def test_the_schema_is_v8_with_one_open_ticket_per_session(self):
        self.assertEqual(SCHEMA_VERSION, 8)
        index = self.rows("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'one_open_ticket'")[0][0]
        self.assertIn("(session_id) WHERE status IN ('pending', 'assigned')", index)
        self.store.request_handoff("alice", self.sid, "button-1")
        insert = """INSERT INTO tickets(ticket_id, short_no, customer_id, session_id, status, urgency, summary,
                        summary_written, created_at, updated_at) VALUES (?, ?, 'alice', ?, ?, 'normal', '{}', 1, 0, 0)"""
        with self.assertRaises(sqlite3.IntegrityError):   # a second open ticket in the session, written directly
            self.store._connection.execute(insert, ("TKT-x", "H-0009", self.sid, "assigned"))
        self.store._connection.rollback()

    def test_the_button_opens_a_ticket_without_touching_anything_else(self):
        before = self.rows("SELECT * FROM return_cards") + self.rows("SELECT * FROM return_requests")
        result = self.store.request_handoff("alice", self.sid, "button-1")
        self.assertEqual((result["ticket_outcome"], result["ticket"]["short_no"], result["ticket"]["status"],
                          result["ticket"]["urgency"], result["ticket"]["summary_written"]),
                         ("opened", "H-0001", "pending", "normal", True))
        self.assertEqual(result["ticket_card"], {"type": "ticket", "ticket_id": result["ticket"]["ticket_id"],
                                                 "short_no": "H-0001", "status": "pending",
                                                 "status_label": STATUS_LABELS["pending"], "actions": ["withdraw"]})
        expected = TICKET_OPENED.format(short_no="H-0001", status="待处理") + "\n" + PLATFORM_CHANNEL
        self.assertEqual(result["last_reply"], expected)   # no open card, so no card sentence
        self.assertEqual(self.rows("SELECT kind, trigger FROM ticket_events"), [("opened", "user_request")])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM session_runs"), [(0,)])   # no turn, no model call
        self.assertEqual(self.rows("SELECT * FROM return_cards") + self.rows("SELECT * FROM return_requests"), before)
        # The code message joins the conversation like the card clicks' messages do.
        self.assertEqual(self.store._history(self.sid)[-1], {"role": "assistant", "content": expected})

    def test_the_same_request_id_replays_and_another_session_cannot_reuse_it(self):
        first = self.store.request_handoff("alice", self.sid, "button-1")
        self.now += 5
        self.assertEqual(self.store.request_handoff("alice", self.sid, "button-1"), first)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM ticket_events"), [(1,)])
        other = self.store.create_session("alice")["session_id"]
        self.error("REQUEST_CONFLICT", self.store.request_handoff, "alice", other, "button-1")

    def test_a_second_press_appends_to_the_open_ticket(self):
        opened = self.store.request_handoff("alice", self.sid, "button-1")
        again = self.store.request_handoff("alice", self.sid, "button-2")
        self.assertEqual((again["ticket_outcome"], again["ticket"]["ticket_id"]),
                         ("appended", opened["ticket"]["ticket_id"]))
        self.assertTrue(again["last_reply"].startswith(TICKET_APPENDED.format(short_no="H-0001", status="待处理")))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM tickets"), [(1,)])
        self.assertEqual(self.rows("SELECT seq, kind FROM ticket_events ORDER BY seq"), [(1, "opened"), (2, "appended")])

    def test_the_button_works_while_a_turn_runs_and_leaves_the_turn_alone(self):
        card = self.card_turn()
        run = self.store.begin_turn("alice", self.sid, "turn-busy", "再看看别的")["run_id"]
        self.assertTrue(self.store.get_session("alice", self.sid)["busy"])
        opened = self.store.request_handoff("alice", self.sid, "button-busy")
        self.assertEqual(opened["ticket_outcome"], "opened")
        self.assertIn(CARD_STILL_VALID, opened["last_reply"])
        # A click on the card is still refused while the turn runs; the ticket click is not.
        self.error("BUSY", self.store.confirm_card, "alice", self.sid, card["action_id"], card["version"],
                   "changed_mind", "", "click-busy")
        withdrawn = self.store.withdraw_ticket("alice", self.sid, opened["ticket"]["ticket_id"], "withdraw-busy")
        self.assertEqual(withdrawn["ticket"]["status"], "cancelled")
        finished = self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "再看看别的"},
                                                                   {"role": "assistant", "content": "好的"}],
                                          {"status": "completed", "reply": "好的"})
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(self.store.get_session("alice", self.sid)["open_cards"][0]["action_id"], card["action_id"])

    def test_with_an_open_card_the_reply_keeps_the_card_and_the_click_still_submits(self):
        card = self.card_turn()
        result = self.store.request_handoff("alice", self.sid, "button-1")
        self.assertEqual(result["last_reply"].split("\n"),
                         [TICKET_OPENED.format(short_no="H-0001", status="待处理"), PLATFORM_CHANNEL, CARD_STILL_VALID])
        self.assertEqual(result["open_cards"][0], card)                       # the card is as it was
        submitted = self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], "quality_issue", "",
                                            "click-1")
        self.assertEqual(submitted["return_request"]["order_id"], "O1001")
        self.assertEqual(self.rows("SELECT order_id, reason_code FROM return_requests"), [("O1001", "quality_issue")])
        self.assertEqual(self.rows("SELECT status FROM tickets"), [("pending",)])   # the click leaves the ticket

    def test_the_owner_withdraws_the_open_ticket_once(self):
        opened = self.store.request_handoff("alice", self.sid, "button-1")
        ticket_id = opened["ticket"]["ticket_id"]
        withdrawn = self.store.withdraw_ticket("alice", self.sid, ticket_id, "withdraw-1")
        self.assertEqual((withdrawn["ticket_outcome"], withdrawn["ticket"]["status"], withdrawn["ticket_card"]["actions"]),
                         ("withdrawn", "cancelled", []))
        self.assertEqual(withdrawn["last_reply"], TICKET_WITHDRAWN.format(short_no="H-0001"))
        self.assertEqual(self.store.withdraw_ticket("alice", self.sid, ticket_id, "withdraw-1"), withdrawn)   # replay
        self.error("TICKET_CLOSED", self.store.withdraw_ticket, "alice", self.sid, ticket_id, "withdraw-2")
        self.assertEqual(self.rows("SELECT kind FROM ticket_events ORDER BY seq"), [("opened",), ("withdrawn",)])
        # After a withdrawal the button opens a new ticket with the customer's next number.
        again = self.store.request_handoff("alice", self.sid, "button-2")
        self.assertEqual((again["ticket_outcome"], again["ticket"]["short_no"]), ("opened", "H-0002"))

    def test_numbers_are_per_customer_and_nobody_reaches_another_customers_ticket(self):
        alice = self.store.request_handoff("alice", self.sid, "button-a")["ticket"]
        bob_sid = self.store.create_session("bob")["session_id"]
        bob = self.store.request_handoff("bob", bob_sid, "button-b")["ticket"]
        self.assertEqual((alice["short_no"], bob["short_no"]), ("H-0001", "H-0001"))
        self.error("SESSION_NOT_FOUND", self.store.request_handoff, "alice", bob_sid, "steal-button")
        self.error("SESSION_NOT_FOUND", self.store.withdraw_ticket, "alice", bob_sid, bob["ticket_id"], "steal-1")
        self.error("TICKET_NOT_FOUND", self.store.withdraw_ticket, "alice", self.sid, bob["ticket_id"], "steal-2")
        own_other_session = self.store.create_session("alice")["session_id"]
        self.error("TICKET_NOT_FOUND", self.store.withdraw_ticket, "alice", own_other_session, alice["ticket_id"],
                   "wrong-session")
        self.assertEqual([item["short_no"] for item in self.store.list_tickets("alice")], ["H-0001"])
        self.assertEqual({item["customer_id"] for item in self.store.list_tickets("bob")}, {"bob"})
        self.assertEqual(self.rows("SELECT status FROM tickets ORDER BY rowid"), [("pending",), ("pending",)])

    def test_the_summary_is_written_once_at_creation_from_records_only(self):
        card = self.card_turn()
        self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], "other", "外盒写着样品勿售",
                                "click-other")
        run = self.store.begin_turn("alice", self.sid, "turn-goal", "这笔再核对一下")["run_id"]
        self.store.record_resolved_orders("alice", self.sid, run, ["O1004", "O2001"])   # another customer's order too
        self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "这笔再核对一下"},
                                                        {"role": "assistant", "content": "好的"}],
                               {"status": "completed", "reply": "好的",
                                "graph": {"plan": {"goal": "核对付款卡 6222 0212 3456 7890 对应的那笔"},
                                          "selection": {"items": [{"index": 1, "claims": [{"card_id": "c1", "outcome_index": 0}]}]}}})
        failed = self.store.begin_turn("alice", self.sid, "turn-failed", "接着说")["run_id"]
        self.store.fail_turn("alice", self.sid, failed, "这一轮没有完成。",
                             response={"status": "model_error", "error": {"code": "GRAPH_MODEL_ERROR"}})
        opened = self.store.request_handoff("alice", self.sid, "button-1")
        listed = self.store.list_tickets("alice")[0]
        summary = listed["summary"]
        self.assertEqual(summary["trigger"], [{"trigger": "user_request",
                                               "evidence": {"source": "handoff_button", "request_id": "button-1"}}])
        self.assertEqual(summary["user_need"], {"text": "核对付款卡 ****7890 对应的那笔", "note": "自动摘要，未核实"})
        self.assertEqual([order["order_id"] for order in summary["verified_facts"]["orders"]], ["O1001", "O1004"])
        self.assertEqual({order["rule"] for order in summary["verified_facts"]["orders"]}, {"eligible"})
        self.assertEqual(summary["verified_facts"]["cited_card_outcomes"], [{"card_id": "c1", "outcome_index": 0}])
        self.assertEqual(summary["task_state"]["return_requests"],
                         [{"order_id": "O1001", "reason_code": "other", "status": "pending"}])
        self.assertEqual(summary["assistant_statements"], ["turn-card", "turn-goal", "turn-failed"])
        self.assertEqual(summary["attempts"], [{"request_id": "turn-failed", "status": "model_error",
                                                "error_code": "GRAPH_MODEL_ERROR"}])
        text = json.dumps(summary, ensure_ascii=False)   # the summary only: generated ids are random hex
        for absent in ("外盒写着样品勿售", "O2001", "6222", "3456"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, text)
        # Written once: a later press appends an event and leaves the summary as it was.
        self.store.request_handoff("alice", self.sid, "button-2")
        self.assertEqual(self.store.list_tickets("alice")[0]["summary"], summary)
        self.assertTrue(opened["ticket"]["summary_written"])

    def test_one_failed_turn_opens_no_ticket_and_writes_no_failure_record(self):
        # B4b: a ticket needs two failed turns in a row (tests/test_failure_next_step.py); the per-failure log is gone.
        run = self.store.begin_turn("alice", self.sid, "turn-1", "问一下")["run_id"]
        self.store.fail_turn("alice", self.sid, run, "这一轮没有完成。", response={"status": "model_error"})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM tickets"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM handoffs"), [(0,)])



class TicketCopyTests(unittest.TestCase):
    def test_ticket_sentences_state_the_record_and_promise_no_follow_up(self):
        for text in (TICKET_OPENED, TICKET_APPENDED, TICKET_WITHDRAWN, PLATFORM_CHANNEL, CARD_STILL_VALID,
                     *STATUS_LABELS.values()):
            with self.subTest(text=text):
                self.assertIsNone(FOLLOW_UP_PROMISES.search(text))
                self.assertEqual({word for word in A0_WORDS if word in text}, set())
                self.assertNotIn("京东", text)   # the assistant names no real platform (decision D8)
        self.assertIn("{short_no}", TICKET_OPENED)
        self.assertIn("当前状态：{status}", TICKET_OPENED)
        self.assertIn("撤回", TICKET_OPENED)
        self.assertIn("原平台", PLATFORM_CHANNEL)
        self.assertIn("本人点按", CARD_STILL_VALID)

    def test_only_tickets_py_writes_the_ticket_tables(self):
        writers = {}
        for path in sorted((APP / "support_agent").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            hits = len(re.findall(r"(?:INSERT\s+INTO|UPDATE)\s+(?:tickets|ticket_events)\b", source))
            if hits:
                writers[path.name] = hits
        self.assertEqual(set(writers), {"tickets.py"})


class TicketHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db_path = Path(self.folder.name) / "service.sqlite3"
        self.server = create_server(db_path=self.db_path, credentials={"t-alice": "alice", "t-bob": "bob"}, port=0)
        self.server.model_factory = lambda: self.fail("a ticket click must not construct a model")
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, token="t-alice"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, None if body is None else json.dumps(body).encode("utf-8"), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_endpoints_take_only_a_request_id_and_declare_the_ticket_card(self):
        _, session = self.call("POST", "/sessions", {})
        sid = session["session_id"]
        status, refused = self.call("POST", f"/sessions/{sid}/handoff", {"request_id": "b-1", "status": "resolved"})
        self.assertEqual((status, refused["error"]["code"]), (400, "INVALID_ARGUMENTS"))
        status, opened = self.call("POST", f"/sessions/{sid}/handoff", {"request_id": "b-1"})
        self.assertEqual((status, opened["ticket_card"]["type"], opened["ticket_card"]["short_no"]), (200, "ticket", "H-0001"))
        ticket_id = opened["ticket"]["ticket_id"]
        status, refused = self.call("POST", f"/sessions/{sid}/tickets/{ticket_id}/withdraw",
                                    {"request_id": "w-1", "status": "resolved"})
        self.assertEqual((status, refused["error"]["code"]), (400, "INVALID_ARGUMENTS"))
        status, wrong_method = self.call("GET", f"/sessions/{sid}/tickets/{ticket_id}/withdraw")
        self.assertEqual((status, wrong_method["error"]["code"]), (405, "METHOD_NOT_ALLOWED"))
        status, withdrawn = self.call("POST", f"/sessions/{sid}/tickets/{ticket_id}/withdraw", {"request_id": "w-1"})
        self.assertEqual((status, withdrawn["ticket"]["status"]), (200, "cancelled"))
        status, closed = self.call("POST", f"/sessions/{sid}/tickets/{ticket_id}/withdraw", {"request_id": "w-2"})
        self.assertEqual((status, closed["error"]["code"]), (409, "TICKET_CLOSED"))
        status, foreign = self.call("POST", f"/sessions/{sid}/handoff", {"request_id": "b-x"}, token="t-bob")
        self.assertEqual((status, foreign["error"]["code"]), (404, "SESSION_NOT_FOUND"))
        # History keeps each ticket click with the card it declared; the page draws the latest one live.
        _, history = self.call("GET", f"/sessions/{sid}/history")
        entries = [(entry["entry"], entry["status"], (entry["response"].get("ticket_card") or {}).get("status"))
                   for entry in history["entries"]]
        self.assertEqual(entries, [("ticket_handoff", "completed", "pending"), ("ticket_withdraw", "completed", "cancelled"),
                                   ("ticket_withdraw", "failed", None)])
        self.assertEqual(history["entries"][0]["response"]["reply"], opened["last_reply"])
        status, listed = self.call("GET", "/tickets")
        self.assertEqual([(item["short_no"], item["status"]) for item in listed["tickets"]], [("H-0001", "cancelled")])
        self.assertEqual(self.call("GET", "/tickets", token="t-bob")[1], {"tickets": []})
        self.assertEqual(self.call("GET", "/handoffs")[0], 404)


class TicketPageTests(unittest.TestCase):
    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def test_the_handoff_button_sits_in_the_top_bar_and_waits_for_no_running_turn(self):
        shell = self.chat[self.chat.index("function renderShell"):self.chat.index("function toggle(")]
        self.assertIn('ticketAction("handoff")', shell)
        self.assertLess(shell.index("els.handoff = "), shell.index('h("header.topbar"'))
        self.assertIn("els.handoff,", shell[shell.index('h("header.topbar"'):])
        action = self.chat[self.chat.index("async function ticketAction"):]
        action = action[:action.index("await api(")]
        self.assertNotIn("state.busy", action)            # usable while a message or click is in flight
        self.assertIn("if (state.ticketBusy) return;", action)

    def test_the_ticket_card_is_drawn_from_its_declaration_only(self):
        card = self.chat[self.chat.index("function ticketCard"):self.chat.index("async function recoverRun")]
        for field in ("block.short_no", "block.status_label", "block.actions"):
            self.assertIn(field, card)
        self.assertNotIn("state.orders", card)
        self.assertIn("response?.ticket_card", self.chat)
        self.assertIn('entry.entry === "ticket_handoff" || entry.entry === "ticket_withdraw"', self.chat)


if __name__ == "__main__":
    unittest.main()
