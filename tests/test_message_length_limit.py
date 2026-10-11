"""The message length limit (stage B4c; first-principles plan §2.1 T2, §2.3 step 1, §8.6 B4c, D15).

Behaviours: a chat message longer than the service's limit (2000 characters, counted as code points) is refused
with HTTP 400 and one code-written sentence before anything else happens: no model is built or called, no turn,
run or request row is written, the request id stays free, and the refusal is not a failed turn, so it neither
counts towards nor ends a run of failures. A message of exactly the limit is answered whole, whatever its byte
length. The page reads the limit from /client-info, counts the trimmed text it sends the same way, shows the count
and keeps the send button off above the limit; nothing else in the product decides a message length.
Offline: the HTTP service on loopback, SQLite and a scripted model stand-in; static checks of the page.
Engineering evidence, not task success; the wording here is written for these tests.
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

from support_agent.service import MESSAGE_LIMIT, MESSAGE_TOO_LONG_TEXT, create_server
from support_agent.sessions import SessionStore
from tests.test_graph_agent import GRAPH_AVAILABLE, ScriptedFactory, ScriptedModel, answered, planned

APP = Path(__file__).resolve().parents[1]
TOKEN = "limit-alice-token"
OVER = "长" * (MESSAGE_LIMIT + 1)


class _Service(unittest.TestCase):
    model_factory = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "limit.sqlite3"
        self.server = create_server(db_path=self.db_path, credentials={TOKEN: "alice"}, port=0,
                                    model_factory=self.make_factory(), log_path=Path(temporary.name) / "log.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.sid = self.request("POST", "/sessions", {})[1]["session_id"]

    def make_factory(self):
        raise NotImplementedError

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer " + TOKEN}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode("utf-8")
                               if body is not None else None, headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def send(self, message, request_id):
        return self.request("POST", f"/sessions/{self.sid}/messages", {"message": message, "request_id": request_id})

    def rows(self, table):
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class MessageLimitRefusalTests(_Service):
    """No model and no graph runtime are needed: the refusal comes before both."""

    def make_factory(self):
        self.built = 0

        def factory():
            self.built += 1
            raise AssertionError("an over-long message must not build a model")
        return factory

    def test_client_info_hands_the_page_the_limit_of_two_thousand_characters(self):
        status, info = self.request("GET", "/client-info")
        self.assertEqual((status, info["message_limit"]), (200, MESSAGE_LIMIT))
        self.assertEqual(MESSAGE_LIMIT, 2000)

    def test_one_character_over_the_limit_is_refused_before_any_turn_or_model(self):
        before = self.request("GET", f"/sessions/{self.sid}")[1]
        status, refused = self.send(OVER, "too-long")
        self.assertEqual(status, 400)
        self.assertEqual(refused, {"ok": False, "error": {"code": "MESSAGE_TOO_LONG", "message": MESSAGE_TOO_LONG_TEXT}})
        self.assertIn(str(MESSAGE_LIMIT), MESSAGE_TOO_LONG_TEXT)
        self.assertEqual(self.built, 0)
        for table in ("session_requests", "session_runs", "tickets", "return_cards", "return_requests"):
            with self.subTest(table=table):
                self.assertEqual(self.rows(table), 0)
        self.assertEqual(self.request("GET", f"/sessions/{self.sid}")[1], before)
        # Characters, not bytes: far below the 65,536-byte body limit, the message is still too long.
        self.assertLess(len(json.dumps({"message": OVER}, ensure_ascii=False).encode("utf-8")), 65536)

    def test_a_blank_message_is_still_refused_as_empty_whatever_its_length(self):
        status, refused = self.send(" " * (MESSAGE_LIMIT + 5), "blank")
        self.assertEqual((status, refused["error"]["code"]), (400, "INVALID_ARGUMENTS"))

    def test_a_refusal_neither_counts_towards_nor_ends_a_run_of_failures(self):
        with closing(SessionStore(self.db_path)) as store:
            def fail(request_id):
                run = store.begin_turn("alice", self.sid, request_id, "先问一件小事")["run_id"]
                store.fail_turn("alice", self.sid, run, "这一轮没有处理完。", response={"status": "model_error"})
                return store.begin_turn("alice", self.sid, request_id, "先问一件小事")["response"]

            first = fail("fail-1")
            self.assertEqual((first["next_step"]["action"], first["next_step"]["consecutive_failures"]), ("retry", 1))
            for index in range(2):
                self.assertEqual(self.send(OVER, f"over-{index}")[1]["error"]["code"], "MESSAGE_TOO_LONG")
            self.assertEqual((self.rows("session_runs"), self.rows("tickets")), (1, 0))
            second = fail("fail-2")
        self.assertEqual((second["next_step"]["action"], second["next_step"]["consecutive_failures"]), ("ticket", 2))
        self.assertEqual(self.rows("tickets"), 1)


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class MessageLimitTurnTests(_Service):
    def make_factory(self):
        self.model = ScriptedModel([])
        return ScriptedFactory(self.model)

    def planned_message(self, call=0):
        return json.loads(self.model.calls[call]["messages"][1]["content"])["current_message"]

    def test_a_message_of_exactly_the_limit_is_answered_and_planned_whole(self):
        message = "长" * MESSAGE_LIMIT
        self.model.outputs.extend([planned(), answered("收到，这么长的说明我会按顺序看。")])
        status, result = self.send(message, "exact")
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(self.planned_message(), message)

    def test_characters_outside_the_basic_plane_count_once(self):
        # 2,000 such characters are 8,000 bytes in UTF-8 and 4,000 UTF-16 units; the limit counts 2,000.
        message = "\U0001F600" * MESSAGE_LIMIT
        self.model.outputs.extend([planned(), answered("收到。")])
        self.assertEqual(self.send(message, "astral")[0], 200)
        self.assertEqual(self.planned_message(), message)
        self.assertEqual(self.send(message + "\U0001F600", "astral-over")[1]["error"]["code"], "MESSAGE_TOO_LONG")
        self.assertEqual(len(self.model.calls), 2)

    def test_a_refused_request_id_stays_free_for_the_shortened_message(self):
        self.assertEqual(self.send(OVER, "shorten")[0], 400)
        self.model.outputs.extend([planned(), answered("收到。")])
        status, result = self.send("删短以后再发一次", "shorten")
        self.assertEqual((status, result["status"], result["replayed"]), (200, "completed", False))
        self.assertEqual(self.planned_message(), "删短以后再发一次")


class MessageLimitPageTests(unittest.TestCase):
    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def function(self, name):
        start = self.chat.index(f"function {name}(")
        return self.chat[start:self.chat.index("\n  }\n", start)]

    def test_the_page_counts_the_sent_text_as_the_server_does_and_keeps_send_off_above_the_limit(self):
        count = self.function("renderCount")
        self.assertIn("state.info.message_limit", count)
        self.assertIn("Array.from(els.textarea.value.trim()).length", count)   # code points of what is sent
        self.assertIn("els.send.disabled = els.textarea.disabled || length > limit", count)
        self.assertIn("renderCount()", self.function("renderComposer"))
        self.assertIn("if (!text || els.send.disabled) return;", self.function("submitComposer"))
        self.assertIn('addEventListener("input", () => { autosize(); renderCount(); })', self.chat)

    def test_the_composer_does_not_cut_a_pasted_message_silently(self):
        composer = next(line for line in self.chat.splitlines() if "els.textarea = h(" in line)
        self.assertNotIn("maxlength", composer)

    def test_no_other_place_decides_a_message_length(self):
        self.assertIsNone(re.search(r"\b[24]000\b", self.chat))
        for name in ("turn.py", "demo_model.py"):
            source = (APP / "support_agent" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                # The plan's goal keeps its own schema bound (600); only message lengths are meant here.
                self.assertNotRegex(source, r"\[:[24]000\]|planner_goal|len\(state\['message'\]\)")


if __name__ == "__main__":
    unittest.main()
