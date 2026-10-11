"""Card lifecycle of confirmation protocol v3 (B3b, plan §4.4, K6, K21), as behaviours.

A card changes only through a click (confirm, or "暂不提交" on the void endpoint), a chat withdrawal (B7: a pause
is none, K21), the rule failing at the click, or its expiry. A failed, interrupted, cancelled or timed
out turn changes no card and nothing holds one: the six hold reason codes are gone, and a click while a turn
runs is refused as BUSY by the session's busy lock. After an unfinished turn the page folds the open card
until the user expands it; the server state stays as it was. Includes the regressions K6 names: R24 in full (B7:
a pause keeps the card; after a withdrawal the old card cannot be confirmed; a read writes nothing) and R28 (after a
failure only the click carries a reason). Offline: SQLite, the real HTTP service and a scripted model stand-in. Engineering and
protocol evidence, not task success.
"""
from contextlib import closing
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest

from support_agent.return_cards import CARD_VOIDED_MESSAGE, ERRORS, VOID_REASONS
from support_agent.sessions import SessionError, SessionStore

APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
# Names of the deleted hold (K6, D19): its reason codes, its error, its event and its void reasons.
HOLD_NAMES = ("INTENT_PENDING", "INTENT_UNRESOLVED", "INTENT_RESOLVED", "EXPLICIT_RECONFIRMATION",
              "CONFIRMATION_HELD", "set_confirmation_hold", "get_confirmation_hold", "intent_confirmation_hold",
              "_introduced_hold", "run_cancelled", "run_interrupted")


class CardLifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "lifecycle.sqlite3"
        self.now = 5_000.0
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.sid = self.store.create_session("alice")["session_id"]
        self.card = self.store.request_card("alice", self.sid, "O1001", "button")["card"]

    def error(self, code, function, *args):
        with self.assertRaises(SessionError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def rows(self, table):
        return [tuple(row) for row in self.store._connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def confirm(self, request_id, reason_code="changed_mind", card=None):
        card = card or self.card
        return self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], reason_code, "",
                                       request_id)

    def void(self, request_id, card=None, customer="alice", session=None):
        card = card or self.card
        return self.store.request_void(customer, session or self.sid, card["action_id"], request_id)

    def snapshot(self):
        return self.store.get_session("alice", self.sid)

    def begin(self, request_id="turn", **limits):
        return self.store.begin_turn("alice", self.sid, request_id, "这张卡先放着，我问点别的", **limits)["run_id"]

    def test_a_failed_turn_changes_no_card_and_places_no_hold(self):
        run = self.begin()
        self.store.fail_turn("alice", self.sid, run, "这一轮没有完成。", response={"status": "model_error"})
        snapshot = self.snapshot()
        self.assertEqual(snapshot["card"], self.card)
        self.assertNotIn("confirmation_hold", snapshot)
        self.assertEqual(self.confirm("after-failure")["return_request"]["order_id"], "O1001")

    def test_an_interrupted_turn_changes_no_card(self):
        self.begin()
        recovered = self.store.recover("alice", self.sid, "recover")
        self.assertEqual(recovered["card"], self.card)
        saved = self.store.begin_turn("alice", self.sid, "turn", "这张卡先放着，我问点别的")["response"]
        self.assertEqual(saved["status"], "interrupted")
        self.assertIn("保持原样", saved["reply"])

    def test_a_cancelled_turn_changes_no_card(self):
        self.begin()
        cancelled = self.store.cancel("alice", self.sid, "cancel")
        self.assertFalse(cancelled["busy"])
        self.assertEqual(cancelled["card"], self.card)

    def test_a_turn_past_its_deadline_is_recovered_without_touching_the_card(self):
        self.begin(run_timeout=5)
        self.now += 6
        self.assertEqual([row["card"] for row in self.store.recover_expired_runs()], [self.card])

    def test_cancel_without_a_running_turn_writes_only_its_own_request_row(self):
        before = {table: self.rows(table) for table in ("return_cards", "return_requests", "session_messages",
                                                         "session_runs", "sessions")}
        result = self.store.cancel("alice", self.sid, "idle-cancel")
        self.assertEqual(result["card"], self.card)
        self.assertEqual({table: self.rows(table) for table in before}, before)
        self.assertEqual(self.store.cancel("alice", self.sid, "idle-cancel"), result)

    def test_clicks_while_a_turn_runs_are_busy_and_cache_nothing(self):
        run = self.begin()
        requests_before = self.rows("session_requests")
        self.error("BUSY", self.confirm, "click-busy")
        self.error("BUSY", self.void, "void-busy")
        self.assertEqual(self.rows("session_requests"), requests_before)
        self.assertEqual(self.snapshot()["card"], self.card)
        self.store.finish_turn("alice", self.sid, run, [{"role": "user", "content": "这张卡先放着，我问点别的"}],
                               {"status": "completed", "reply": "好的。"})
        self.assertEqual(self.void("void-busy")["card"]["status"], "voided")

    def test_the_not_now_click_voids_the_card_and_says_so_once(self):
        voided = self.void("not-now")
        self.assertEqual((voided["card"]["status"], voided["card"]["void_reason"]), ("voided", "user"))
        self.assertEqual(voided["last_reply"], CARD_VOIDED_MESSAGE)
        messages = [json.loads(row[2]) for row in self.rows("session_messages")]
        self.assertEqual(messages, [{"role": "assistant", "content": CARD_VOIDED_MESSAGE}])
        self.assertEqual(self.void("not-now"), voided)                       # same request_id replays
        self.error("REQUEST_CONFLICT", self.void, "not-now", {**self.card, "action_id": "ACT-other"})
        self.error("CONFIRMATION_INVALID", self.void, "not-now-again")      # nothing left to void
        self.error("CONFIRMATION_INVALID", self.confirm, "click-voided")
        self.assertEqual(self.rows("return_requests"), [])
        again = self.store.request_card("alice", self.sid, "O1001", "button-again")
        self.assertEqual((again["card_outcome"], again["card"]["version"]), ("created", 2))

    def test_the_void_click_is_bound_to_the_customer_and_the_session(self):
        self.error("SESSION_NOT_FOUND", self.void, "steal", None, "bob")
        other = self.store.create_session("alice")["session_id"]
        self.error("CONFIRMATION_INVALID", self.void, "elsewhere", None, "alice", other)
        self.assertEqual(self.snapshot()["card"], self.card)

    def test_a_submitted_card_cannot_be_voided(self):
        # Decision D4: no undo after a submission; only the platform's after-sales cancellation path exists.
        submitted = self.confirm("click")
        self.error("CONFIRMATION_INVALID", self.void, "too-late")
        self.assertEqual(self.snapshot()["card"]["status"], "consumed")
        self.assertEqual(self.snapshot()["return_request"], submitted["return_request"])

    def test_an_expired_card_is_marked_expired_by_the_void_click(self):
        self.now = self.card["expires_at"]
        self.error("CONFIRMATION_EXPIRED", self.void, "late-void")
        self.assertEqual(self.rows("return_cards")[0][5], "expired")

    def test_the_hold_is_gone_from_the_code(self):
        # B7: a switch to another order is a chat withdrawal plus a return, so nothing voids a card as "replaced".
        self.assertEqual(VOID_REASONS, ("user", "chat_withdraw", "eligibility_changed"))
        self.assertNotIn("CONFIRMATION_HELD", ERRORS)
        self.assertFalse(hasattr(SessionStore, "set_confirmation_hold"))
        found = []
        for path in sorted([*(APP / "support_agent").rglob("*.py"), *(APP / "support_agent" / "web").glob("*.js")]):
            text = path.read_text(encoding="utf-8")
            found += [(path.name, name) for name in HOLD_NAMES if name in text]
        self.assertEqual(found, [])
        self.assertNotIn("confirmation_hold", (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8"))


class NotNowButtonAndFoldSourceTests(unittest.TestCase):
    """Static checks of the page (T5 walks it in a browser)."""

    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def function(self, name):
        start = self.chat.index(f"function {name}(")
        return self.chat[start:self.chat.index("\n  }\n", start)]

    def test_not_now_calls_the_void_endpoint_and_never_sends_a_chat_message(self):
        self.assertIn('"/void"', self.function("voidAction"))
        self.assertIn("onclick: () => voidAction(card)", self.function("confirmCard"))
        self.assertNotIn('send("先不要提交")', self.chat)
        self.assertNotIn('"/cancel"', self.chat)    # the panel's cancel-the-card button is gone

    def test_an_unfinished_turn_folds_the_card_before_any_confirm_button_exists(self):
        folded = self.function("cardFolded")
        for status in ('"failed"', '"cancelled"', '"interrupted"'):
            self.assertIn(status, folded)
        self.assertIn('entry.entry === "card_prepare"', folded)
        card = self.function("confirmCard")
        self.assertLess(card.index("if (cardFolded(card))"), card.index("const submit"))
        folded_view = card[card.index("if (cardFolded(card))"):card.index("const order")]
        self.assertIn("展开这张卡", folded_view)
        self.assertNotIn("confirmAction", folded_view)
        self.assertIn("state.unfolded = null", self.function("send"))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class CardLifecycleHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.db = directory / "lifecycle.sqlite3"
        self.model = ScriptedModel([])
        self.server = create_server(db_path=self.db, credentials={"life-alice": "alice", "life-bob": "bob"}, port=0,
                                    model_factory=ScriptedFactory(self.model), log_path=directory / "log.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, token="life-alice"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def send(self, sid, message, request_id):
        return self.request("POST", f"/sessions/{sid}/messages", {"message": message, "request_id": request_id})

    def click(self, sid, card, request_id, reason_code="changed_mind"):
        return self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm",
                            {"version": card["version"], "reason_code": reason_code, "reason_text": "",
                             "request_id": request_id})

    def card_session(self):
        from tests.test_graph_agent import business, planned
        sid = self.request("POST", "/sessions", {})[1]["session_id"]
        self.model.outputs.append(planned(business(order="O1001", quote="退掉")))
        status, result = self.send(sid, "退掉 O1001", "card")
        self.assertEqual((status, result["confirmation"]["status"]), (200, "available"), result)
        return sid, result["session"]["card"]

    def count(self, table):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_failed_turn_declares_the_open_card_again_and_the_click_still_decides(self):
        from support_agent.models import ModelError
        sid, card = self.card_session()
        self.model.outputs.append(ModelError("脚本里的供应商故障"))
        status, failed = self.send(sid, "顺便问一下别的事", "fails")
        self.assertEqual((status, failed["status"]), (200, "model_error"))
        self.assertEqual(failed["session"]["card"], card)
        self.assertNotIn("confirmation_hold", failed["session"])
        self.assertEqual((failed["confirmation"]["status"], failed["confirmation"]["action_id"]),
                         ("available", card["action_id"]))
        # B7r: the waiting line is a block like every code sentence, failed turn or not (the page shows a failed turn's
        # reply string).
        self.assertEqual([block["type"] for block in failed["presentation"]["blocks"]], ["return_card", "text"])
        from support_agent import copy as code_copy
        reminder = code_copy.OPEN_CARDS_WAITING.format(order_ids="O1001")
        self.assertEqual(failed["presentation"]["blocks"][-1], {"type": "text", "author": "code", "text": reminder})
        self.assertIn("待确认：订单 O1001 的退货确认卡仍然有效，尚未提交。", failed["reply"])
        history = self.request("GET", f"/sessions/{sid}/history")[1]
        self.assertEqual([(entry["entry"], entry["status"]) for entry in history["entries"]],
                         [("turn", "completed"), ("turn", "failed")])   # what the page folds the card on
        self.assertEqual(self.count("return_requests"), 0)
        status, confirmed = self.click(sid, card, "click")
        self.assertEqual((status, confirmed["return_request"]["order_id"]), (200, "O1001"))

    def test_r28_after_a_failure_only_the_click_carries_a_reason(self):
        from support_agent.models import ModelError
        sid, card = self.card_session()
        self.model.outputs.append(ModelError("脚本里的供应商故障"))
        self.send(sid, "原因就写盒子压坏了", "reason-then-failure")
        status, refused = self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm",
                                       {"version": card["version"], "reason_code": "other", "reason_text": "",
                                        "request_id": "no-reason"})
        self.assertEqual((status, refused["error"]["code"]), (400, "INVALID_REASON"))
        self.assertEqual(self.click(sid, card, "with-reason", reason_code="quality_issue")[0], 200)
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute("SELECT reason_code, reason_text FROM return_requests").fetchall()
            everything = json.dumps([connection.execute(f"SELECT * FROM {table}").fetchall()
                                     for table in ("return_cards", "return_requests")], ensure_ascii=False)
        self.assertEqual(rows, [("quality_issue", None)])
        self.assertNotIn("盒子压坏了", everything)

    def test_r24_a_pause_keeps_the_card_a_withdrawn_card_cannot_be_confirmed_and_a_lookup_writes_nothing(self):
        from tests.test_graph_agent import answered, business, item, planned
        sid, card = self.card_session()
        # B7 (K21): a pause is chit-chat for the planner; the card stays exactly as it was.
        self.model.outputs.extend([planned([item("chit_chat", "先等等")]),
                                   answered("", disposition="answered", next_step="想好了再在卡上点按。")])
        status, paused = self.send(sid, "先等等", "pause")
        self.assertEqual((status, paused["session"]["card"], paused["confirmation"]["status"]),
                         (200, card, "available"))
        self.assertIn("待确认：订单 O1001 的退货确认卡仍然有效，尚未提交。", paused["reply"])
        self.model.outputs.append(planned(business("withdraw", quote="这张卡不要了")))
        status, withdrawn = self.send(sid, "这张卡不要了", "withdraw")
        self.assertEqual((withdrawn["session"]["card"]["status"], withdrawn["session"]["card"]["void_reason"]),
                         ("voided", "chat_withdraw"))
        status, refused = self.click(sid, card, "stale-click")
        self.assertEqual((status, refused["error"]["code"]), (409, "CONFIRMATION_INVALID"))
        cards = self.count("return_cards")
        self.model.outputs.append(planned(business("lookup_order", order="O1001")))
        status, looked = self.send(sid, "O1001 现在是什么状态", "lookup")
        self.assertEqual((status, looked["confirmation"]["status"]), (200, "unavailable"))
        self.assertEqual((self.count("return_cards"), self.count("return_requests")), (cards, 0))

    def test_the_void_endpoint_takes_only_a_request_id_and_calls_no_model(self):
        sid, card = self.card_session()
        calls = len(self.model.calls)
        path = f"/sessions/{sid}/cards/{card['action_id']}/void"
        self.assertEqual(self.request("POST", path, {"request_id": "v", "reason_code": "other"})[0], 400)
        self.assertEqual(self.request("GET", path)[0], 405)
        self.assertEqual(self.request("POST", path, {"request_id": "v"}, token="life-bob")[0], 404)
        status, voided = self.request("POST", path, {"request_id": "v"})
        self.assertEqual((status, voided["card"]["status"], voided["card"]["void_reason"]), (200, "voided", "user"))
        self.assertEqual(len(self.model.calls), calls)
        history = self.request("GET", f"/sessions/{sid}/history")[1]
        self.assertEqual([entry["entry"] for entry in history["entries"]], ["turn", "card_void"])
        self.assertEqual(history["entries"][-1]["response"]["reply"], CARD_VOIDED_MESSAGE)
        self.assertEqual(self.request("POST", path, {"request_id": "v2"})[1]["error"]["code"], "CONFIRMATION_INVALID")

    def test_the_hold_never_reaches_the_planning_context(self):
        from tests.test_graph_agent import answered, planned
        from support_agent.models import ModelError
        sid, card = self.card_session()
        self.model.outputs.append(ModelError("脚本里的供应商故障"))
        self.send(sid, "随便问问", "fails")
        self.model.outputs.extend([planned(), answered()])
        self.assertEqual(self.send(sid, "再问一次", "next")[1]["status"], "completed")
        plan_call = next(call for call in reversed(self.model.calls)
                         if call["tools"][0]["function"]["name"] == "plan_turn")
        context = json.loads(plan_call["messages"][1]["content"])
        # B7 rewrote the planning context (K14): the open card is there by its order id only, and no hold key is left.
        self.assertEqual(context["session_cards"], {"open_card_order_ids": [card["order_id"]], "submitted_order_ids": []})
        self.assertFalse(re.search(r"INTENT_|held|confirmation_hold", json.dumps(context)))


if __name__ == "__main__":
    unittest.main()
