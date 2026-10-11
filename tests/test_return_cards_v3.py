"""Confirmation protocol v3 (B3a, decision 3): the card, the click and the reason, as behaviours.

The return request has one writer (the click transaction); the reason comes only from the click, from the
server catalogue, and an "other" text stays in the request row; a card turn calls no answer model and shows
no reason question. The card lifecycle of B3b (no hold, the void click, failures that change nothing) is in
tests/test_card_lifecycle.py. Offline: a scripted model stand-in, the real HTTP service and SQLite.
Engineering and protocol evidence, not task success.
"""
import ast
from contextlib import closing
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from support_agent.return_cards import (ERRORS, OTHER_TEXT_LIMIT, RETURN_REASONS_V1, RETURN_REASONS_VERSION,
                                        reasons_payload, validate_reason)
from support_agent.sessions import SessionError, SessionStore

APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
OTHER_TEXT = "柜门把手一碰就晃"


class ReasonCatalogueTests(unittest.TestCase):
    def test_the_catalogue_is_the_d5_codes_with_one_label_each(self):
        self.assertEqual([code for code, _ in RETURN_REASONS_V1],
                         ["changed_mind", "size_or_style", "quality_issue", "wrong_or_missing_item",
                          "damaged_on_arrival", "other"])
        labels = [label for _, label in RETURN_REASONS_V1]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertTrue(all(label.strip() for label in labels))
        self.assertEqual(dict(RETURN_REASONS_V1)["other"], "其他")
        payload = reasons_payload()
        self.assertEqual((payload["version"], payload["other_text_limit"]), (RETURN_REASONS_VERSION, 200))

    def test_a_reason_is_valid_only_as_a_catalogue_code_with_the_right_text(self):
        self.assertEqual(validate_reason("quality_issue", ""), ("quality_issue", None))
        self.assertEqual(validate_reason("other", "  说明在这里  "), ("other", "说明在这里"))
        self.assertEqual(validate_reason("other", "字" * OTHER_TEXT_LIMIT)[1], "字" * OTHER_TEXT_LIMIT)
        for code, text in (("quality_issue", "多余的说明"), ("other", ""), ("other", "   "),
                           ("other", "字" * (OTHER_TEXT_LIMIT + 1)), ("other", "第一行\n第二行"),
                           ("other", "带\t制表符"), ("other", "c1\x85控制符"), ("other", "\x7f"),
                           ("free_text", ""), ("", ""), (None, ""), ("other", None), ("changed_mind", None)):
            with self.subTest(code=code, text=text):
                self.assertIsNone(validate_reason(code, text))

    def test_every_error_is_code_written_without_demo_or_legal_words(self):
        for code, text in ERRORS.items():
            with self.subTest(code=code):
                for word in ("模拟", "演示", "教学", "场景日", "法律", "法定", "法规", "权利"):
                    self.assertNotIn(word, text)


class SingleWriterTests(unittest.TestCase):
    """I1: the return request has exactly one writer, and no turn module can reach it."""

    def test_the_return_request_insert_exists_in_exactly_one_place(self):
        hits = []
        for path in sorted((APP / "support_agent").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and "INSERT INTO return_requests" in " ".join(node.value.split()):
                    hits.append(path.name)
        self.assertEqual(hits, ["return_cards.py"])

    def test_no_turn_module_names_the_click_transaction(self):
        for name in ("turn.py", "business.py", "decide.py", "demo_model.py", "respond.py", "assemble.py"):
            with self.subTest(module=name):
                self.assertNotIn("confirm_card", (APP / "support_agent" / name).read_text(encoding="utf-8"))


    def test_one_open_card_per_session_and_order_is_a_database_rule(self):
        # B3c (D4): the partial unique index covers (session_id, order_id); another order may have its own card.
        insert = ("INSERT INTO return_cards(action_id, customer_id, session_id, order_id, version, status, "
                  "created_at, expires_at) VALUES (?, 'alice', ?, ?, 9, 'pending', 1, 2)")
        with tempfile.TemporaryDirectory() as folder:
            with closing(SessionStore(Path(folder) / "cards.sqlite3", clock=lambda: 1000)) as store:
                sid = store.create_session("alice")["session_id"]
                store.request_card("alice", sid, "O1001", "first")
                with self.assertRaises(sqlite3.IntegrityError):
                    with store._connection:
                        store._connection.execute(insert, ("ACT-same-order", sid, "O1001"))
                with store._connection:
                    store._connection.execute(insert, ("ACT-other-order", sid, "O1004"))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class CardTurnHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.db = directory / "v3.sqlite3"
        self.model = ScriptedModel([])
        self.server = create_server(db_path=self.db, credentials={"v3-alice": "alice"}, port=0,
                                    model_factory=ScriptedFactory(self.model), log_path=directory / "log.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer v3-alice"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def session(self):
        return self.request("POST", "/sessions", {})[1]["session_id"]

    def send(self, sid, message, request_id):
        return self.request("POST", f"/sessions/{sid}/messages", {"message": message, "request_id": request_id})

    def test_a_card_turn_calls_only_the_planner(self):
        from tests.test_graph_agent import business, planned
        self.model.outputs.append(planned(business(order="O1001")))
        status, result = self.send(self.session(), "我要退货 O1001，颜色和图片不一样", "card")
        self.assertEqual(status, 200, result)
        self.assertEqual(len(self.model.calls), 1)
        # B6: the policy step (the catalogue, the route table) runs every turn; it is code, no model call.
        self.assertEqual(result["graph"]["nodes"], ["understand", "business", "policy", "answer"])
        self.assertEqual(result["confirmation"]["protocol"], "observed-action-v3")
        block = next(b for b in result["presentation"]["blocks"] if b["type"] == "return_card")
        self.assertEqual((block["action_id"], block["version"], block["order_id"]),
                         (result["confirmation"]["action_id"], result["confirmation"]["version"], "O1001"))
        self.assertNotIn("颜色和图片不一样", json.dumps(block, ensure_ascii=False))

    def test_a_card_turn_with_a_policy_question_never_asks_the_reason_in_chat(self):
        # B5d2: the option sets hold no reason (G8 structure) and words that ask for one are a guard hit: B5f drops
        # that note only and the gap item's code line stands in; the card is unchanged.
        from support_agent import copy as code_copy
        from tests.test_graph_agent import answered, business, item, planned
        plan = planned([*business(order="O1001"), item("policy_question", "运费谁出", fee_subject="return_shipping")])
        # (A recall gap noted in B5d2: the wording "是什么原因" is not one G8 detects; guards change in their own stage.)
        prose = "能说说退货的原因吗？"
        self.model.outputs.extend([plan, answered(prose), answered(prose)])
        status, result = self.send(self.session(), "我要退货 O1001，运费谁出", "mixed")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["run_status"], "completed", result.get("error"))
        self.assertEqual(result["confirmation"]["status"], "available")
        self.assertNotIn(prose, result["reply"])
        self.assertIn(code_copy.ITEM_LINES["gap"], result["reply"])
        self.assertNotIn(code_copy.FALLBACK, result["reply"])
        hits = next(row["hits"] for row in result["trace"] if row["type"] == "guards")
        self.assertIn("G8_REASON_ASKED", [hit["code"] for hit in hits])
        schema = self.model.calls[1]["tools"][0]["function"]["parameters"]
        self.assertNotIn("reason", " ".join(schema["properties"]["ask"]["properties"]["option_set_id"]["enum"]))


    def test_the_other_text_stays_in_the_request_row_only(self):
        from tests.test_graph_agent import business, planned
        sid = self.session()
        self.model.outputs.append(planned(business(order="O1004")))
        card = self.send(sid, "我要退货 O1004", "card")[1]["confirmation"]
        status, confirmed = self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm",
                                         {"version": card["version"], "reason_code": "other",
                                          "reason_text": OTHER_TEXT, "request_id": "click"})
        self.assertEqual(status, 200, confirmed)
        self.assertNotIn(OTHER_TEXT, json.dumps(confirmed, ensure_ascii=False))
        self.assertIn("原因：其他", confirmed["confirmation_message"])
        history = self.request("GET", f"/sessions/{sid}/history")[1]
        orders = self.request("GET", "/me/orders")[1]
        for value in (history, orders):
            self.assertNotIn(OTHER_TEXT, json.dumps(value, ensure_ascii=False))
        with closing(sqlite3.connect(self.db)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                rows = json.dumps(connection.execute(f"SELECT * FROM {table}").fetchall(), ensure_ascii=False)
                with self.subTest(table=table):
                    self.assertEqual(OTHER_TEXT in rows, table == "return_requests")
        # A later turn in another session reads this order: the model request never carries the text.
        from tests.test_graph_agent import planned as plan_only
        lookup = business("lookup_order", order="O1004")
        self.model.outputs.append(plan_only(lookup))
        status, later = self.send(self.session(), "查一下 O1004", "later")
        self.assertEqual(status, 200, later)
        self.assertNotIn(OTHER_TEXT, json.dumps(self.model.calls[-1]["messages"], ensure_ascii=False))
        self.assertNotIn(OTHER_TEXT, json.dumps(later, ensure_ascii=False))

    def test_the_order_button_opens_a_card_without_a_model_call_and_cards_need_an_owned_session(self):
        sid = self.session()
        status, opened = self.request("POST", f"/sessions/{sid}/cards", {"order_id": "O1001", "request_id": "b1"})
        self.assertEqual((status, opened["card_outcome"], opened["card"]["order_id"]), (200, "created", "O1001"))
        self.assertEqual(self.model.calls, [])
        status, again = self.request("POST", f"/sessions/{sid}/cards", {"order_id": "O1001", "request_id": "b2"})
        self.assertEqual((status, again["card_outcome"]), (200, "reshown"))
        self.assertEqual(again["card"]["action_id"], opened["card"]["action_id"])
        self.assertEqual(self.request("POST", "/sessions/SES-missing/cards",
                                      {"order_id": "O1001", "request_id": "b3"})[0], 404)


if __name__ == "__main__":
    unittest.main()
