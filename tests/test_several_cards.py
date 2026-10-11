"""Several return cards in one session and a session that goes on after a submission (B3c, decision D4).

Behaviours: a session holds at most one open card per order and may hold one for each eligible order; a
message that names several eligible orders gets a card for each (at most decide.MAX_AUTO_CARDS), the other
named orders say why not; every open card is declared by the response and confirmed by its own click only;
a chat withdrawal voids the named card, or the only one, and otherwise leaves the choice to the card's own
"暂不提交"; a switch to another order (B7: a withdrawal plus a return in one turn) voids the card the user
dropped; nothing ends the session, and a submitted request has no undo here: the reply and the void click name the
platform's after-sales cancellation path. The planning context lists the order ids of the open cards and of the
latest submission, nothing else (B7, K16). Offline: SQLite, the real HTTP service and a scripted model stand-in.
Engineering and protocol evidence, not task success.
"""
from contextlib import closing
from itertools import count
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest

from support_agent import decide
from support_agent.business import (CHOOSE_CARD_RESULT, NO_OPEN_CARD_RESULT, BusinessAdapter)
from support_agent import copy as code_copy
from support_agent.assemble import business_parts
from support_agent.return_cards import ERRORS, SUBMITTED_NO_UNDO
from support_agent.sessions import SCHEMA_VERSION, SessionError, SessionStore

def rendered(result):
    """The business outcome as the reply's code sentences word it (B5d2: assemble.py from copy.py)."""
    lead, lines, note = business_parts(result)
    return "\n".join(part for part in (lead, *lines, note) if part)


APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None


def plan(action="request_return", *, quote, orders=(), order_id=None, order_quote=""):
    """The business items of one request (B7): one per named order (its id as typed), or one without an order; a
    read quotes the order's words."""
    kind = {"request_return": "return_request", "withdraw": "withdraw", "lookup_order": "own_order_status"}[action]
    rows = [(order, order) for order in orders] or [(order_quote, order_id or "none")]
    return [{"kind": kind, "subkind": "none", "quote": words if kind == "own_order_status" else quote,
             "history_index": None, "order_quote": words, "order_id": order, "fee_subject": "none"}
            for words, order in rows]


class SeveralCardsStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "several.sqlite3"
        self.now = 9_000.0
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.sid = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def button(self, order_id, session=None):
        return self.store.request_card("alice", session or self.sid, order_id, f"button-{next(self.ids)}")

    def click(self, card, reason_code="changed_mind", session=None):
        return self.store.confirm_card("alice", session or self.sid, card["action_id"], card["version"], reason_code,
                                       "", f"click-{next(self.ids)}")

    def error(self, code, function, *args):
        with self.assertRaises(SessionError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def statuses(self):
        return [tuple(row) for row in self.store._connection.execute(
            "SELECT order_id, status, void_reason FROM return_cards ORDER BY rowid")]

    def test_the_schema_is_v7_with_one_open_card_per_session_and_order(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 7)   # B4a moved to v8 (tickets); the card index is unchanged
        sql = self.store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'one_open_card'").fetchone()[0]
        self.assertIn("(session_id, order_id)", sql)

    def test_cards_for_two_orders_sit_side_by_side_and_are_listed_oldest_first(self):
        cup = self.button("O1001")["card"]
        self.now += 1
        box = self.button("O1004")["card"]
        snapshot = self.store.get_session("alice", self.sid)
        self.assertEqual([card["action_id"] for card in snapshot["open_cards"]], [cup["action_id"], box["action_id"]])
        self.assertEqual(snapshot["card"], box)                     # the latest card, as before
        again = self.button("O1001")   # the response's card is the one this click re-showed, not the latest
        self.assertEqual((again["card_outcome"], again["card"]), ("reshown", cup))
        self.assertEqual(self.statuses(), [("O1001", "pending", None), ("O1004", "pending", None)])

    def test_an_expired_card_is_not_open_and_its_order_gets_a_new_version(self):
        cup = self.button("O1001")["card"]
        self.now += 1
        self.button("O1004")
        self.now = cup["expires_at"]
        self.assertEqual([card["order_id"] for card in self.store.get_session("alice", self.sid)["open_cards"]],
                         ["O1004"])
        renewed = self.button("O1001")
        self.assertEqual((renewed["card_outcome"], renewed["card"]["version"]), ("created", 2))
        self.assertEqual(self.statuses()[0], ("O1001", "expired", None))

    def test_each_card_is_submitted_by_its_own_click_with_its_own_reason(self):
        cup = self.button("O1001")["card"]
        box = self.button("O1004")["card"]
        first = self.click(cup, "quality_issue")
        self.assertEqual((first["card"]["action_id"], first["card"]["status"]), (cup["action_id"], "consumed"))
        self.assertEqual([card["order_id"] for card in first["open_cards"]], ["O1004"])
        self.assertEqual(first["return_request"]["order_id"], "O1001")
        second = self.click(box, "size_or_style")
        self.assertEqual(second["open_cards"], [])
        self.assertEqual(second["return_request"]["order_id"], "O1004")   # the newest submission of the session
        rows = [tuple(row) for row in self.store._connection.execute(
            "SELECT order_id, reason_code, source_action_id FROM return_requests ORDER BY rowid")]
        self.assertEqual(rows, [("O1001", "quality_issue", cup["action_id"]), ("O1004", "size_or_style", box["action_id"])])

    def test_nothing_ends_the_session_after_a_submission(self):
        cup = self.button("O1001")["card"]
        self.click(cup)
        turn = self.store.begin_turn("alice", self.sid, "after", "接着聊别的")
        self.assertFalse(turn["replayed"])
        self.store.finish_turn("alice", self.sid, turn["run_id"], [{"role": "user", "content": "接着聊别的"}],
                               {"status": "completed", "reply": "好的。"})
        self.assertEqual(self.button("O1004")["card_outcome"], "created")
        self.error("RETURN_ALREADY_REQUESTED", self.button, "O1001")   # a submitted order gets no new card
        self.assertNotIn("TASK_TERMINAL", ERRORS)
        self.assertFalse(hasattr(SessionStore, "_not_terminal"))

    def test_a_submitted_card_has_no_undo_and_the_refusal_names_the_aftersales_path(self):
        cup = self.button("O1001")["card"]
        self.click(cup)
        refused = self.error("CONFIRMATION_INVALID", self.store.request_void, "alice", self.sid, cup["action_id"],
                             "void-late")
        self.assertEqual(refused.message, SUBMITTED_NO_UNDO)
        self.assertIn("售后详情", SUBMITTED_NO_UNDO)
        self.assertEqual(self.statuses(), [("O1001", "consumed", None)])
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_a_prepare_call_that_fails_the_rule_writes_nothing_of_that_call(self):
        self.button("O1001")
        box = self.button("O1004")["card"]
        turn = self.store.begin_turn("alice", self.sid, "prepare", "盒子和饼干都退")
        prepared = self.store.prepare_cards("alice", self.sid, turn["run_id"], ["O1004"])
        self.assertEqual((prepared[0]["outcome"], prepared[0]["card"]), ("reshown", box))
        with self.assertRaises(SessionError):
            self.store.prepare_cards("alice", self.sid, turn["run_id"], ["O1002", "O1003"])
        # The food order fails the rule inside the transaction: nothing of that call is written.
        self.assertEqual(self.statuses(), [("O1001", "pending", None), ("O1004", "pending", None)])

    def test_another_session_keeps_its_own_card_for_the_same_order(self):
        other = self.store.create_session("alice")["session_id"]
        mine = self.button("O1001")["card"]
        theirs = self.button("O1001", session=other)["card"]
        self.assertNotEqual(mine["action_id"], theirs["action_id"])
        self.error("CONFIRMATION_INVALID", self.click, theirs, "changed_mind", self.sid)   # bound to its session
        self.click(mine)
        late = self.click(theirs, "quality_issue", other)
        self.assertEqual(late["confirmation_message"], "这一单已经有一条退货申请，没有再新增，仍以原有那条为准。")
        self.assertEqual([row["reason_code"] for row in self.store.list_return_requests()], ["changed_mind"])


class DecisionTableTests(unittest.TestCase):
    def test_every_named_order_is_a_target_and_an_ambiguous_mention_gives_candidates(self):
        own = {"O1", "O2", "O3"}
        common = dict(current=False, all_mine=False, withdrawn=(), open_orders=[], latest_resolved=[], own=own)
        self.assertEqual(decide.return_targets({"O2", "O1"}, set(), **common), (["O1", "O2"], decide.BY_MESSAGE, []))
        self.assertEqual(decide.return_targets({"O1"}, {"O2", "O3"}, **common), ([], None, ["O1", "O2", "O3"]))

    def test_current_means_the_only_open_card_and_never_one_of_several(self):
        own = {"O1", "O2", "O3"}
        one = decide.return_targets(set(), set(), current=True, all_mine=False, withdrawn=(), open_orders=["O2"],
                                    latest_resolved=[], own=own)
        self.assertEqual(one, (["O2"], decide.BY_OPEN_CARD, []))
        two = decide.return_targets(set(), set(), current=True, all_mine=False, withdrawn=(),
                                    open_orders=["O1", "O2"], latest_resolved=[], own=own)
        self.assertEqual(two, ([], None, ["O1", "O2", "O3"]))
        # B7: a switch whose new order is unnamed never goes back to the order it just dropped, by K5 or as a
        # candidate.
        switched = decide.return_targets(set(), set(), current=False, all_mine=False, withdrawn=["O1"],
                                         open_orders=["O2"], latest_resolved=["O1"], own=own)
        self.assertEqual(switched, ([], None, ["O2", "O3"]))

    def test_the_card_limit_is_the_eligible_named_orders_up_to_four(self):
        targets = [f"O{n}" for n in range(1, 8)]
        outcomes = {"O1": "prepare", "O2": "ineligible", "O3": "reshow", "O4": "already_requested",
                    "O5": "prepare", "O6": "prepare", "O7": "prepare"}
        self.assertEqual(decide.MAX_AUTO_CARDS, 4)
        self.assertEqual(decide.carded(targets, outcomes), ["O1", "O3", "O5", "O6"])
        self.assertEqual(decide.carded(["O2", "O4"], outcomes), [])

    def test_the_withdrawal_rows(self):
        table = (
            (({"O1"}, ["O1", "O2"], set()), ("void", ["O1"])),
            ((set(), ["O2"], set()), ("void", ["O2"])),
            ((set(), ["O1", "O2"], set()), ("choose", ["O1", "O2"])),
            (({"O3"}, ["O1"], set()), ("choose", ["O1"])),
            (({"O3"}, [], {"O3"}), ("submitted", ["O3"])),
            (({"O3"}, ["O1"], {"O3"}), ("submitted", ["O3"])),
            ((set(), [], {"O3"}), ("submitted", ["O3"])),
            ((set(), [], set()), ("no_open_card", [])),
            (({"O3"}, [], set()), ("no_open_card", [])),
        )
        for arguments, expected in table:
            with self.subTest(arguments=arguments):
                self.assertEqual(decide.withdraw_outcome(*arguments), expected)


class SeveralCardsTurnTests(unittest.TestCase):
    """The business boundary of a chat turn (no model): the decision table executed on the real store."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "turns.sqlite3"
        self.store = SessionStore(self.db, clock=lambda: 1_000)
        self.addCleanup(self.store.close)
        self.sid = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def apply(self, message, proposed):
        started = self.store.begin_turn("alice", self.sid, f"turn-{next(self.ids)}", message, mode="offline_graph")
        results = BusinessAdapter(self.store, "alice", self.sid, started["run_id"]).apply(
            [{"index": index, **item} for index, item in enumerate(proposed, 1)])
        self.store.finish_turn("alice", self.sid, started["run_id"], [{"role": "user", "content": message}],
                               {"reply": results[-1].get("reply", ""), "business": results})
        return results[0] if len(results) == 1 else results

    def open_orders(self):
        return [card["order_id"] for card in self.store.get_session("alice", self.sid)["open_cards"]]

    def statuses(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT order_id, status, void_reason FROM return_cards ORDER BY rowid").fetchall()

    def two_cards(self):
        return self.apply("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")))

    def test_three_named_orders_get_cards_for_the_eligible_ones_and_a_reason_for_the_other(self):
        result = self.apply("O1004、O1002、O1001 都退", plan(quote="都退", orders=("O1004", "O1002", "O1001")))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual([(row["order_id"], row["outcome"]) for row in result["return_outcomes"]],
                         [("O1001", "created"), ("O1002", "ineligible"), ("O1004", "created")])
        self.assertEqual(self.open_orders(), ["O1001", "O1004"])
        text = rendered(result)
        self.assertEqual(text.count(code_copy.SUBMIT_BY_CLICK), 2)   # one sentence per prepared card
        self.assertIn("订单 O1002：学习用帆布包", text)
        self.assertEqual(text.count(code_copy.QUALITY_PROBLEM_PATH), 1)   # O1002 is past the window (D6)
        self.assertEqual(self.store.list_return_requests(), [])
        # K5 sees several resolved orders, so the next unnamed request offers candidates instead of guessing.
        follow = self.apply("那就退吧", plan(quote="那就退吧"))
        self.assertEqual(follow["status"], "clarification_needed")

    def test_a_message_naming_only_ineligible_orders_prepares_nothing(self):
        result = self.apply("O1002 和 O1003 退掉", plan(quote="退掉", orders=("O1002", "O1003")))
        self.assertEqual(result["status"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(self.statuses(), [])

    def test_an_unnamed_withdrawal_with_two_cards_voids_nothing(self):
        self.two_cards()
        result = self.apply("有一张卡不要了", plan("withdraw", quote="有一张卡不要了"))
        self.assertEqual((result["status"], result["reply"]), ("card_choice_needed", CHOOSE_CARD_RESULT))
        self.assertEqual(self.open_orders(), ["O1001", "O1004"])

    def test_a_named_withdrawal_voids_only_that_card(self):
        self.two_cards()
        result = self.apply("O1001 那张不要了", plan("withdraw", quote="不要了", orders=("O1001",)))
        self.assertEqual((result["status"], result["voided_order_id"]), ("card_voided", "O1001"))
        self.assertEqual(self.statuses(), [("O1001", "voided", "chat_withdraw"), ("O1004", "pending", None)])

    def test_a_withdrawal_naming_both_cards_voids_neither_and_leaves_each_to_its_own_click(self):
        # B3e: a chat withdrawal voids at most one card; two named cards are dropped on the cards themselves.
        self.two_cards()
        result = self.apply("O1001 O1004 都不要了", plan("withdraw", quote="都不要了", orders=("O1001", "O1004")))
        self.assertEqual((result["status"], result["open_card_order_ids"]), ("card_choice_needed", ["O1001", "O1004"]))
        self.assertEqual(self.open_orders(), ["O1001", "O1004"])
        self.assertEqual(self.statuses(), [("O1001", "pending", None), ("O1004", "pending", None)])

    def test_a_withdrawal_of_a_submitted_request_answers_with_the_aftersales_path(self):
        self.two_cards()
        card = self.store.get_session("alice", self.sid)["open_cards"][0]
        self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], "changed_mind", "", "c1")
        named = self.apply("O1001 那条撤了", plan("withdraw", quote="撤了", orders=("O1001",)))
        self.assertEqual((named["status"], named["reply"]), ("submitted_no_undo", SUBMITTED_NO_UNDO))
        self.assertEqual(named["submitted_order_ids"], ["O1001"])
        self.assertEqual(self.open_orders(), ["O1004"])               # the other card is untouched
        self.assertEqual(len(self.store.list_return_requests()), 1)
        unnamed_with_card = self.apply("算了不要了", plan("withdraw", quote="算了不要了"))
        self.assertEqual(unnamed_with_card["status"], "card_voided")  # the only open card is the one meant
        unnamed = self.apply("算了都撤了", plan("withdraw", quote="算了都撤了"))
        self.assertEqual(unnamed["status"], "submitted_no_undo")
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_no_open_card_and_nothing_submitted_keeps_the_old_answer(self):
        result = self.apply("这张卡不要了", plan("withdraw", quote="这张卡不要了"))
        self.assertEqual((result["status"], result["reply"]), ("no_open_card", NO_OPEN_CARD_RESULT))

    def test_a_switch_voids_the_dropped_card_and_reshows_its_target(self):
        # B7: "不退那单，改退这单" is a withdraw item plus a return_request item; the withdrawal runs first.
        self.two_cards()
        box = self.store.get_session("alice", self.sid)["open_cards"][1]
        withdrawn, result = self.apply("保温杯不退了改退 O1004", [
            *plan("withdraw", quote="保温杯不退了", order_quote="保温杯", order_id="O1001"),
            *plan(quote="改退", orders=("O1004",))])
        self.assertEqual(withdrawn["voided_order_id"], "O1001")
        self.assertEqual((result["card"], result["card_outcome"]), (box, "reshown"))
        self.assertEqual(self.statuses(), [("O1001", "voided", "chat_withdraw"), ("O1004", "pending", None)])

    def test_a_plain_return_for_another_order_keeps_the_open_card(self):
        self.apply("退 O1001", plan(quote="退", orders=("O1001",)))
        result = self.apply("还有 O1004 也退", plan(quote="也退", orders=("O1004",)))
        self.assertEqual(result["card"]["order_id"], "O1004")
        self.assertEqual(self.open_orders(), ["O1001", "O1004"])

    def test_current_with_two_open_cards_is_not_guessed(self):
        self.two_cards()
        result = self.apply("就退这单", plan(quote="就退这单", order_quote="这单", order_id="current"))
        self.assertEqual(result["status"], "clarification_needed")
        self.assertEqual(self.open_orders(), ["O1001", "O1004"])


class SeveralOrdersRenderTests(unittest.TestCase):
    def test_an_eligible_order_past_the_card_limit_keeps_its_button(self):
        order = {"order_id": "O7", "product_name": "示例商品", "status": "delivered", "days_since_delivery": 1,
                 "return_eligible": True, "eligibility_code": "eligible", "opened": False, "category": "general",
                 "return_request": None}
        text = rendered({"ok": True, "status": "awaiting_confirmation", "reply": "",
                         "orders": [order], "return_outcomes": [{"order_id": "O7", "outcome": "not_prepared"}]})
        self.assertEqual(text, code_copy.CARD_LIMIT.format(limit=decide.MAX_AUTO_CARDS, order_id="O7"))
        self.assertIn("申请退货", code_copy.CARD_LIMIT)


class PageSourceTests(unittest.TestCase):
    """Static checks of the page (T5 walks it in a browser)."""

    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")
        self.css = (APP / "support_agent" / "web" / "chat.css").read_text(encoding="utf-8")

    def test_every_open_card_is_drawn_and_nothing_ends_the_session(self):
        self.assertIn("for (const card of openCards()) inner.append(confirmCard(card))", self.chat)
        self.assertIn("state.snapshot?.open_cards", self.chat)
        for gone in ("terminal()", "pendingAction()", "div.ended", "办理其他事项请新建咨询"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.chat)
        self.assertNotIn(".ended", self.css)
        composer = self.chat[self.chat.index("function renderComposer("):self.chat.index("function orderPick(")]
        # B4c: only a running turn disables the composer; the send button is also off above the message limit.
        self.assertIn("els.textarea.disabled = state.busy || !!state.snapshot?.busy;", composer)
        self.assertIn("els.send.disabled = els.textarea.disabled || length > limit;", composer)

    def function(self, name):
        start = self.chat.index(f"function {name}(")
        return self.chat[start:self.chat.index("\n  }\n", start)]

    def test_the_header_and_the_panel_read_one_status_whose_orders_belong_to_it(self):
        status = self.function("sessionStatus")
        self.assertIn("orders: [snapshot.return_request.order_id]", status)
        self.assertLess(status.index("open.length"), status.index("snapshot.return_request"))
        for name in ("renderHeader", "taskPanel"):
            with self.subTest(name=name):
                body = self.function(name)
                self.assertIn("sessionStatus(snapshot)", body)
                self.assertNotIn("CARD_STATUS.", body)       # no second reading of the status
                self.assertNotIn("snapshot.card", body)

    def test_a_folded_card_unfolds_only_for_a_request_for_that_card(self):
        self.assertIn("entry.response?.card?.action_id === card.action_id", self.function("cardFolded"))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class SeveralCardsHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.db = directory / "several.sqlite3"
        self.model = ScriptedModel([])
        self.server = create_server(db_path=self.db, credentials={"several-alice": "alice"}, port=0,
                                    model_factory=ScriptedFactory(self.model), log_path=directory / "log.jsonl")
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
        headers = {"Authorization": "Bearer several-alice"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def send(self, message, proposed, request_id):
        """One chat turn; a turn without a business action also gets the scripted answer it asks for."""
        from tests.test_graph_agent import answered, planned
        self.model.outputs.extend([planned(proposed)] if proposed else [planned(), answered()])
        status, result = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": message, "request_id": request_id})
        self.assertEqual(status, 200, result)
        return result

    def planning_context(self):
        call = next(call for call in reversed(self.model.calls)
                    if call["tools"][0]["function"]["name"] == "plan_turn")
        return json.loads(call["messages"][1]["content"])

    def test_the_response_declares_every_open_card_with_its_own_block(self):
        result = self.send("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")), "two")
        confirmation = result["confirmation"]
        self.assertEqual([card["order_id"] for card in confirmation["cards"]], ["O1001", "O1004"])
        self.assertEqual({key: confirmation[key] for key in ("action_id", "version", "order_id")},
                         {key: confirmation["cards"][-1][key] for key in ("action_id", "version", "order_id")})
        blocks = [block for block in result["presentation"]["blocks"] if block["type"] == "return_card"]
        self.assertEqual([(block["action_id"], block["version"]) for block in blocks],
                         [(card["action_id"], card["version"]) for card in confirmation["cards"]])
        self.assertFalse(any(key in json.dumps(confirmation) for key in ("reason_code", "reason_text")))
        self.assertEqual(len(self.model.calls), 1)                       # a pure card turn skips the answer model
        self.assertNotIn("待确认", result["reply"])                       # both were stated by this turn
        for card in result["session"]["open_cards"]:
            self.assertEqual(set(card), {"action_id", "version", "order_id", "status", "void_reason", "expires_at"})

    def test_the_reply_names_the_cards_this_turn_did_not_show(self):
        self.send("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")), "two")
        looked = self.send("O1002 现在怎样", plan("lookup_order", quote="", orders=("O1002",)), "look")
        self.assertTrue(looked["reply"].endswith("待确认：订单 O1001、O1004 的退货确认卡仍然有效，尚未提交。"))
        again = self.send("O1004 再出一次", plan(quote="再出一次", orders=("O1004",)), "again")
        self.assertIn("待确认：订单 O1001 的", again["reply"])
        self.assertNotIn("订单 O1001、O1004", again["reply"])

    def test_the_planning_context_lists_the_open_cards_and_the_latest_submission_by_order_id_only(self):
        # B7 (K3, K16): the planner sees which orders have an open card and which one was submitted last in this
        # session, nothing more: no action ids, versions, expiry or reasons.
        self.send("退 O1001", plan(quote="退", orders=("O1001",)), "one")
        self.send("随便看看", None, "plain-1")
        self.assertEqual(self.planning_context()["session_cards"],
                         {"open_card_order_ids": ["O1001"], "submitted_order_ids": []})
        two = self.send("O1004 也退", plan(quote="也退", orders=("O1004",)), "two")
        self.send("随便看看", None, "plain-2")
        self.assertEqual(self.planning_context()["session_cards"]["open_card_order_ids"], ["O1001", "O1004"])
        newest = two["session"]["open_cards"][-1]
        status, _ = self.request("POST", f"/sessions/{self.sid}/cards/{newest['action_id']}/confirm",
                                 {"version": newest["version"], "reason_code": "changed_mind", "reason_text": "",
                                  "request_id": "click"})
        self.assertEqual(status, 200)
        self.send("随便看看", None, "plain-3")
        context = self.planning_context()
        self.assertEqual(context["session_cards"], {"open_card_order_ids": ["O1001"], "submitted_order_ids": ["O1004"]})
        self.assertNotIn(newest["action_id"], json.dumps(context))
        self.assertFalse(re.search(r"reason_(code|text)|action_id|expires_at", json.dumps(context)))

    def test_a_two_card_turn_with_a_reason_question_completes_without_asking_for_the_reason(self):
        from tests.test_graph_agent import answered, item, planned
        proposal = planned([*plan(quote="一并退", orders=("O1001", "O1004")),
                            item("policy_question", "上门取件收钱吗", fee_subject="return_shipping")])
        # The words ask for the reason: G8 drops them (B5f: that piece only), the cards stand.
        self.model.outputs.extend([proposal, answered("先说说原因？"), answered("先说说原因？")])
        status, result = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": "O1001、O1004 一并退，上门取件收钱吗", "request_id": "mixed"})
        self.assertEqual((status, result["run_status"]), (200, "completed"), result.get("error"))
        self.assertEqual([card["order_id"] for card in result["confirmation"]["cards"]], ["O1001", "O1004"])
        self.assertNotIn("先说说原因", result["reply"])
        self.assertNotIn("next_step", result)   # B4b: only a failed turn declares a next step

    def test_a_click_on_one_card_leaves_the_other_and_the_session_answers_on(self):
        result = self.send("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")), "two")
        cup, box = result["confirmation"]["cards"]
        status, clicked = self.request("POST", f"/sessions/{self.sid}/cards/{cup['action_id']}/confirm",
                                       {"version": cup["version"], "reason_code": "quality_issue", "reason_text": "",
                                        "request_id": "click-cup"})
        self.assertEqual((status, [card["order_id"] for card in clicked["open_cards"]]), (200, ["O1004"]))
        later = self.send("O1004 呢", plan("lookup_order", quote="", orders=("O1004",)),
                          "after")
        self.assertEqual((later["status"], later["confirmation"]["order_id"]), ("completed", box["order_id"]))
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute("SELECT order_id, reason_code FROM return_requests").fetchall()
        self.assertEqual(rows, [("O1001", "quality_issue")])

    def listed(self):
        status, listing = self.request("GET", "/me/sessions")
        self.assertEqual(status, 200, listing)
        row = next(item for item in listing["sessions"] if item["session_id"] == self.sid)
        return row["card_status"], row["order_id"]

    def test_a_submission_stays_on_its_own_order_after_the_other_card_is_voided(self):
        result = self.send("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")), "two")
        cup, box = result["confirmation"]["cards"]
        self.assertEqual(self.listed(), ("pending", "O1004"))
        status, clicked = self.request("POST", f"/sessions/{self.sid}/cards/{cup['action_id']}/confirm",
                                       {"version": cup["version"], "reason_code": "changed_mind", "reason_text": "",
                                        "request_id": "click-cup"})
        self.assertEqual(status, 200, clicked)
        self.assertEqual(self.listed(), ("pending", "O1004"))               # an open card still comes first
        status, voided = self.request("POST", f"/sessions/{self.sid}/cards/{box['action_id']}/void",
                                      {"request_id": "void-box"})
        self.assertEqual((status, voided["card"]["order_id"], voided["card"]["status"]), (200, "O1004", "voided"))
        # The latest card is O1004's voided one; what the session submitted is O1001's request, on O1001.
        self.assertEqual((voided["open_cards"], voided["card"]["order_id"], voided["return_request"]["order_id"]),
                         ([], "O1004", "O1001"))
        self.assertEqual(self.listed(), ("consumed", "O1001"))

    def test_the_history_names_the_card_each_click_acted_on(self):
        from support_agent.models import ModelError
        result = self.send("O1001、O1004 一并退", plan(quote="一并退", orders=("O1001", "O1004")), "two")
        cup, box = result["confirmation"]["cards"]
        self.model.outputs.append(ModelError("脚本里的供应商故障"))
        status, failed = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": "顺便问点别的", "request_id": "fails"})
        self.assertEqual((status, failed["status"]), (200, "model_error"))
        status, again = self.request("POST", f"/sessions/{self.sid}/cards", {"order_id": "O1001", "request_id": "cup-again"})
        self.assertEqual((status, again["card_outcome"], again["card"]["action_id"]), (200, "reshown", cup["action_id"]))
        entries = self.request("GET", f"/sessions/{self.sid}/history")[1]["entries"]
        self.assertEqual([(entry["entry"], entry["status"]) for entry in entries],
                         [("turn", "completed"), ("turn", "failed"), ("card_prepare", "completed")])
        # chat.js folds O1004's card still: the request after the failure names O1001's card only.
        self.assertEqual(entries[-1]["response"]["card"]["action_id"], cup["action_id"])
        self.assertNotEqual(entries[-1]["response"]["card"]["action_id"], box["action_id"])
        self.assertNotIn("card", entries[0]["response"])                  # turns keep their own fields


if __name__ == "__main__":
    unittest.main()
