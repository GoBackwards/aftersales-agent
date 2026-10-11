"""The failure policy (stage B4b; first-principles plan §6.1, §6.3, §8.6; standard HO4, HR5, W6, A4).

Behaviours: a failed turn writes nothing but its run and its reply, and the reply ends with exactly one next step.
One failure offers a retry (the page sends the same message again as a new request); the second failed turn in a
row, counted from the session's runs, opens the session's ticket (or appends to an open one) with the trigger
technical_failure in the same transaction and says so plainly, with the original platform's channel as its one next
step and no retry beside it; later failures of the same run append to the open ticket, and a ticket its owner
withdrew during the run is not reopened by it. A completed turn, an item-level degraded one included, and a
cancelled turn end a run of failures; clicks do not. No failure touches a card, and the per-failure record and its
sentence are gone. The run's own error stays in the response's error field, out of the reply and out of the next
turn's history. Offline: SQLite, the HTTP service with a scripted model stand-in, static checks of the page.
Engineering evidence, not task success; the wording here is written for these tests.
"""
import http.client
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

from support_agent.sessions import (FAILED_RUN_STATUSES, FAILED_TURN_TEXT, RETRY_STEP, SessionError,
                                    SessionStore)
from support_agent.tickets import PLATFORM_CHANNEL, TECHNICAL_FAILURE_LEAD, technical_failure_text

APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
OPENED = TECHNICAL_FAILURE_LEAD + "已提交人工工单 {}，当前状态：待处理。可以随时撤回。\n" + PLATFORM_CHANNEL
APPENDED = TECHNICAL_FAILURE_LEAD + "已追加到人工工单 {}，当前状态：待处理。可以随时撤回。\n" + PLATFORM_CHANNEL


class FailurePolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "failures.sqlite3"
        self.now = 7000.0
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.sid = self.store.create_session("alice")["session_id"]
        self.requests = 0

    def begin(self, text="先打听一件小事"):
        self.requests += 1
        return self.store.begin_turn("alice", self.sid, f"turn-{self.requests}", text)["run_id"]

    def failed_turn(self, status="model_error", message=FAILED_TURN_TEXT):
        run = self.begin()
        self.store.fail_turn("alice", self.sid, run, message, response={"status": status})
        return self.store.begin_turn("alice", self.sid, f"turn-{self.requests}", "先打听一件小事")["response"]

    def complete(self, response=None):
        run = self.begin()
        return self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "先打听一件小事"}],
                                      {"status": "completed", "reply": "这件事可以这样办。", **(response or {})})

    def count(self, sql, *args):
        return self.store._connection.execute(sql, args).fetchone()[0]

    def events(self):
        return [(row["kind"], row["trigger"]) for ticket in self.store.list_tickets("alice") for row in ticket["events"]]

    def test_one_failed_turn_offers_a_retry_and_records_nothing_but_its_run(self):
        before = {table: self.count(f"SELECT COUNT(*) FROM {table}")
                  for table in ("return_cards", "return_requests", "tickets", "ticket_events", "handoffs")}
        failed = self.failed_turn()
        self.assertEqual(failed["reply"], FAILED_TURN_TEXT + "\n" + RETRY_STEP)
        self.assertEqual(failed["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertNotIn("ticket_card", failed)
        self.assertNotIn("handoff", failed)
        self.assertEqual({table: self.count(f"SELECT COUNT(*) FROM {table}") for table in before}, before)
        self.assertEqual(self.count("SELECT COUNT(*) FROM session_runs WHERE status = 'model_error'"), 1)

    def test_the_second_failed_turn_in_a_row_opens_a_technical_failure_ticket_and_says_so(self):
        self.failed_turn()
        second = self.failed_turn("timed_out")
        self.assertEqual(second["next_step"], {"action": "ticket", "consecutive_failures": 2,
                                               "ticket_outcome": "opened"})
        self.assertEqual(second["reply"], FAILED_TURN_TEXT + "\n" + OPENED.format("H-0001"))
        [ticket] = self.store.list_tickets("alice")
        self.assertEqual((ticket["short_no"], ticket["status"], ticket["urgency"], ticket["session_id"]),
                         ("H-0001", "pending", "normal", self.sid))
        self.assertEqual(second["ticket_card"], {"type": "ticket", "ticket_id": ticket["ticket_id"], "short_no": "H-0001",
                                                 "status": "pending", "status_label": "待处理", "actions": ["withdraw"]})
        [event] = ticket["events"]
        self.assertEqual((event["kind"], event["trigger"]), ("opened", "technical_failure"))
        self.assertEqual(event["evidence"], {"source": "consecutive_failed_turns", "request_id": "turn-2",
                                             "run_status": "timed_out", "consecutive_failures": 2})
        self.assertEqual(ticket["summary"]["trigger"], [{"trigger": "technical_failure", "evidence": event["evidence"]}])
        self.assertEqual([item["request_id"] for item in ticket["summary"]["attempts"]], ["turn-1"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM handoffs"), 0)

    def test_each_failed_turn_states_exactly_one_next_step(self):
        # W6: a retry and a ticket never share one reply; the ticket reply's step is the platform channel.
        replies = [self.failed_turn()["reply"], self.failed_turn()["reply"], self.failed_turn()["reply"]]
        self.assertEqual([reply.count(RETRY_STEP) for reply in replies], [1, 0, 0])
        self.assertEqual([reply.count("人工工单") for reply in replies], [0, 1, 1])
        self.assertEqual([reply.count(PLATFORM_CHANNEL) for reply in replies], [0, 1, 1])
        self.assertTrue(all("重试" not in reply for reply in replies[1:]))
        self.assertEqual(replies[2], FAILED_TURN_TEXT + "\n" + APPENDED.format("H-0001"))
        self.assertEqual(self.events(), [("opened", "technical_failure"), ("appended", "technical_failure")])

    def test_a_completed_turn_ends_the_run_of_failures(self):
        self.failed_turn()
        self.complete()
        again = self.failed_turn()
        self.assertEqual(again["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertEqual(self.store.list_tickets("alice"), [])

    def test_a_cancelled_turn_ends_the_run_and_its_reply_adds_no_step(self):
        self.failed_turn()
        self.begin()
        cancelled = self.store.cancel("alice", self.sid, "cancel-1")
        self.assertNotIn("handoff", cancelled)
        saved = self.store.begin_turn("alice", self.sid, f"turn-{self.requests}", "先打听一件小事")["response"]
        self.assertEqual(saved["status"], "cancelled")
        self.assertNotIn("next_step", saved)
        self.assertNotIn(RETRY_STEP, saved["reply"])
        self.assertEqual(self.failed_turn()["next_step"],
                         {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertEqual(self.store.list_tickets("alice"), [])

    def test_an_interrupted_turn_is_a_failed_turn(self):
        self.failed_turn()
        self.begin()
        self.store.recover("alice", self.sid, "recover-1")
        saved = self.store.begin_turn("alice", self.sid, f"turn-{self.requests}", "先打听一件小事")["response"]
        self.assertEqual((saved["status"], saved["next_step"]["action"], saved["next_step"]["consecutive_failures"]),
                         ("interrupted", "ticket", 2))
        self.assertTrue(saved["reply"].endswith(OPENED.format("H-0001")))

    def test_clicks_between_failed_turns_do_not_end_the_run(self):
        self.failed_turn()
        opened = self.store.request_handoff("alice", self.sid, "button-1")   # the button opens the ticket first
        self.assertEqual(opened["ticket_outcome"], "opened")
        second = self.failed_turn()
        self.assertEqual(second["next_step"], {"action": "ticket", "consecutive_failures": 2,
                                               "ticket_outcome": "appended"})
        self.assertEqual(self.events(), [("opened", "user_request"), ("appended", "technical_failure")])
        self.assertEqual(len(self.store.list_tickets("alice")), 1)

    def test_a_ticket_withdrawn_during_a_run_is_not_reopened_by_it(self):
        self.failed_turn()
        ticket_id = self.failed_turn()["ticket_card"]["ticket_id"]
        self.store.withdraw_ticket("alice", self.sid, ticket_id, "withdraw-1")
        for expected in (3, 4):
            later = self.failed_turn()
            self.assertEqual(later["next_step"], {"action": "retry", "consecutive_failures": expected,
                                                  "offer_handoff": False})
            self.assertTrue(later["reply"].endswith(RETRY_STEP))
        self.assertEqual([ticket["status"] for ticket in self.store.list_tickets("alice")], ["cancelled"])
        # A new run of failures, after a completed turn, opens a new ticket at its second failure.
        self.complete()
        self.failed_turn()
        self.assertEqual(self.failed_turn()["ticket_card"]["short_no"], "H-0002")

    def test_failed_turns_leave_every_card_as_it_was_and_only_a_click_submits(self):
        run = self.begin()
        card = self.store.prepare_card("alice", self.sid, run, "O1001")["card"]
        self.store.fail_turn("alice", self.sid, run, FAILED_TURN_TEXT, response={"status": "model_error"})
        self.failed_turn()
        snapshot = self.store.get_session("alice", self.sid)
        self.assertEqual([(item["action_id"], item["version"], item["status"]) for item in snapshot["open_cards"]],
                         [(card["action_id"], card["version"], "pending")])
        self.assertEqual(self.count("SELECT COUNT(*) FROM return_requests"), 0)
        clicked = self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], "quality_issue", "",
                                          "click-1")
        self.assertEqual(clicked["card"]["status"], "consumed")
        self.assertEqual(self.count("SELECT COUNT(*) FROM return_requests"), 1)

    def test_only_the_store_states_a_next_step(self):
        completed = self.complete({"next_step": {"action": "retry"}, "ticket_card": {"type": "ticket"}})
        self.assertNotIn("next_step", completed)
        self.assertNotIn("ticket_card", completed)
        run = self.begin()
        self.store.fail_turn("alice", self.sid, run, FAILED_TURN_TEXT,
                             response={"status": "model_error", "next_step": {"action": "ticket"}})
        saved = self.store.begin_turn("alice", self.sid, f"turn-{self.requests}", "先打听一件小事")["response"]
        self.assertEqual(saved["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})

    def test_every_failure_status_counts_and_nothing_else_does(self):
        for status in sorted(FAILED_RUN_STATUSES):
            with self.subTest(status=status):
                self.sid = self.store.create_session("alice")["session_id"]
                self.assertEqual(self.failed_turn(status)["next_step"]["consecutive_failures"], 1)
                self.assertEqual(self.failed_turn(status)["next_step"]["action"], "ticket")
        self.assertNotIn("completed", FAILED_RUN_STATUSES)
        self.assertNotIn("cancelled", FAILED_RUN_STATUSES)



class FailureCopyTests(unittest.TestCase):
    def test_the_failure_copy_states_no_cause_detail_and_one_step(self):
        self.assertNotIn("重试", FAILED_TURN_TEXT)
        self.assertIn("“重试”", RETRY_STEP)
        text = technical_failure_text({"short_no": "H-0007", "status_label": "待处理"}, "opened")
        self.assertEqual(text, OPENED.format("H-0007"))
        self.assertNotIn("重试", text)
        self.assertEqual(technical_failure_text({"short_no": "H-0007", "status_label": "待处理"}, "appended"),
                         APPENDED.format("H-0007"))

    def test_the_per_failure_record_and_its_sentence_are_gone(self):
        sessions = (APP / "support_agent" / "sessions.py").read_text(encoding="utf-8")
        self.assertNotIn("HANDOFF_NOTICE", sessions)
        self.assertNotIn("INSERT OR IGNORE INTO handoffs", sessions)
        found = [path.name for path in (APP / "support_agent").rglob("*.py")
                 if re.search(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+handoffs\b", path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])
        for path in (APP / "support_agent").rglob("*.py"):
            self.assertNotIn("这一轮的处理记录已保存", path.read_text(encoding="utf-8"), path.name)


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class FailurePolicyHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.model = ScriptedModel([])
        self.server = create_server(db_path=directory / "failures.sqlite3", credentials={"fail-alice": "alice"},
                                    port=0, model_factory=ScriptedFactory(self.model), log_path=directory / "log.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.sid = self.request("POST", "/sessions", {})[1]["session_id"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer fail-alice"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def provider_fails(self, request_id, text="想顺带问个小问题"):
        from support_agent.models import ModelError
        self.model.outputs.append(ModelError("供应商接口返回 HTTP 503；请检查地址和额度。"))
        status, result = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": text, "request_id": request_id})
        self.assertEqual((status, result["status"]), (200, "model_error"))
        return result

    def test_the_provider_message_stays_in_error_and_out_of_the_reply_and_the_next_turns_history(self):
        from tests.test_graph_agent import answered, planned
        failed = self.provider_fails("f-1")
        self.assertEqual(failed["reply"], FAILED_TURN_TEXT + "\n" + RETRY_STEP)
        self.assertEqual(failed["presentation"]["short_answer"], failed["reply"])
        self.assertEqual(failed["error"], {"code": "GRAPH_MODEL_ERROR", "message": "供应商接口返回 HTTP 503；请检查地址和额度。"})
        # The page's "重试": the same message as a new request; the failed request keeps its saved result.
        self.model.outputs.extend([planned(), answered("这件事可以先看看页面上的说明。")])
        status, retried = self.request("POST", f"/sessions/{self.sid}/messages",
                                       {"message": "想顺带问个小问题", "request_id": "f-1-retry"})
        self.assertEqual((status, retried["status"]), (200, "completed"))
        self.assertNotIn("next_step", retried)
        history = json.loads(self.model.calls[-2]["messages"][1]["content"])["history"]
        self.assertIn(("assistant", FAILED_TURN_TEXT + "\n" + RETRY_STEP),
                      [(item["role"], item["content"]) for item in history])
        self.assertNotIn("503", json.dumps(history, ensure_ascii=False))
        status, replay = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": "想顺带问个小问题", "request_id": "f-1"})
        self.assertEqual((replay["replayed"], replay["status"], replay["reply"]), (True, "model_error", failed["reply"]))

    def test_the_history_declares_each_failed_turns_one_step_and_its_ticket_card(self):
        self.request("POST", f"/sessions/{self.sid}/cards", {"order_id": "O1001", "request_id": "card"})
        first = self.provider_fails("f-1")
        second = self.provider_fails("f-2")
        self.assertEqual(first["next_step"]["action"], "retry")
        self.assertEqual((second["next_step"]["action"], second["ticket_card"]["short_no"]), ("ticket", "H-0001"))
        self.assertIn("O1001", second["reply"][second["reply"].index("待确认："):])
        self.assertLess(second["reply"].index("待确认："), second["reply"].index(TECHNICAL_FAILURE_LEAD))
        entries = self.request("GET", f"/sessions/{self.sid}/history")[1]["entries"]
        turns = [entry["response"] for entry in entries if entry["entry"] == "turn"]
        self.assertEqual([turn["next_step"]["action"] for turn in turns], ["retry", "ticket"])
        self.assertNotIn("ticket_card", turns[0])
        self.assertEqual(turns[1]["ticket_card"], second["ticket_card"])
        self.assertTrue(all("handoff" not in turn for turn in turns))
        status, listed = self.request("GET", "/tickets")
        self.assertEqual([(event["kind"], event["trigger"]) for event in listed["tickets"][0]["events"]],
                         [("opened", "technical_failure")])


class FailurePageTests(unittest.TestCase):
    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def function(self, name):
        start = self.chat.index(f"function {name}(")
        return self.chat[start:self.chat.index("\n  }\n", start)]

    def test_a_failed_turn_shows_only_the_step_its_response_declares(self):
        message = self.function("assistantMessage")
        self.assertIn("failureStep(response, entry)", message)
        self.assertNotIn("callout.warn", message)
        self.assertNotIn("换个说法", self.chat)   # the old second, contradicting next step (W6)

    def test_the_ticket_card_comes_instead_of_the_retry_and_the_retry_resends_the_same_message(self):
        step = self.function("failureStep")
        self.assertLess(step.index("response.ticket_card"), step.index('"retry"'))
        self.assertIn("sessionTickets().get(block.ticket_id) === block ? ticketCard(block) : null", step)
        self.assertIn('text: "重试"', step)
        self.assertIn("send(entry.user_message)", step)   # send() makes a new request id
        self.assertIn("entry !== latest", step)            # only on the session's latest turn
        self.assertIn("requestId()", self.function("send"))


if __name__ == "__main__":
    unittest.main()
