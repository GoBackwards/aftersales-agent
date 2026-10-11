"""The guards run once per answered turn (B5d2, plan §5.4, §5.5, K23, K12; B5e; B5f).

Structure first (G9): a schema break goes back to ``respond`` once, then the turn fails and nothing is written. The
words next (G1–G10, piece by piece): B5f drops only the piece a guard hits, every choice (cards, orders shown, paths)
standing, and an answer item left with nothing gets the code's line for its disposition; no call rewrites them, so a
turn makes at most four structured calls by construction. A route turn never shows a note or a next step. With
PROSE_SLOT_ENABLED off no words are shown and the code's lines stand in. A claim that a record backs passes. Scripted
models over loopback HTTP and SQLite; synthetic wording; mechanics, not answer quality.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support_agent import copy as code_copy

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
PROMISE = "商家稍后会联系你处理这件事。"
CLEAN = "这件事可以在原订单页联系商家说明。"


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class GuardsWiredTests(unittest.TestCase):
    from tests import test_graph_agent as fixtures
    setUp = fixtures.GraphHTTPTests.setUp
    stop = fixtures.GraphHTTPTests.stop
    request = fixtures.GraphHTTPTests.request
    start = fixtures.GraphHTTPTests.start
    send = fixtures.GraphHTTPTests.send
    confirm = fixtures.GraphHTTPTests.confirm

    def types(self, result):
        return [row["type"] for row in result["trace"]]

    def counts(self):
        with self.server.store() as store:
            return {table: store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("return_requests", "return_cards", "tickets")}

    def test_clean_words_are_checked_once_and_shown_as_the_models(self):
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered(CLEAN, path="merchant_contact")])
        result = self.send(self.start(), "台灯的事找谁？")[1]
        self.assertEqual(self.types(result).count("guards"), 1)
        self.assertNotIn("revise_prose", self.types(result))
        self.assertEqual([(block["type"], block["author"]) for block in result["presentation"]["blocks"]],
                         [("text", "model"), ("path", "code")])
        self.assertTrue(result["answer_validation"]["prose_shown"])

    def test_a_hit_drops_only_that_piece_and_every_choice_stands(self):
        self.model.outputs.extend([self.fixtures.planned(),
                                   self.fixtures.answered(PROMISE, show=["O1001"], path="merchant_contact")])
        result = self.send(self.start(), "台灯的事有人管吗？")[1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.types(result).count("guards"), 1)
        self.assertNotIn("revise_prose", self.types(result))
        self.assertNotIn("fallback_used", self.types(result))
        self.assertEqual([hit["part"] for row in result["trace"] if row["type"] == "guards" for hit in row["hits"]],
                         ["note:1"])
        self.assertNotIn(PROMISE, result["reply"])
        # The gap item is left with nothing of its own: the code's line for a gap, its path; the turn only names a gap,
        # so the item comes before the order facts (S1).
        self.assertEqual([(block["type"], block["author"]) for block in result["presentation"]["blocks"]],
                         [("text", "code"), ("path", "code"), ("order_facts", "code")])
        self.assertEqual(result["presentation"]["blocks"][0]["text"], code_copy.ITEM_LINES["gap"])
        self.assertFalse(result["answer_validation"]["fallback"])
        self.assertEqual(result["graph"]["selection"]["show_order_ids"], ["O1001"])
        self.assertEqual(result["graph"]["selection"]["items"][0]["note"], "")

    def test_a_turn_makes_at_most_four_structured_calls(self):
        # Plan §0.2: a plan and its correction, an answer and its correction; a guard hit asks for nothing more.
        broken = self.fixtures.response("plan_support_request", {"capability": "clarification"})
        self.model.outputs.extend([broken, self.fixtures.planned(), self.fixtures.answered(PROMISE, path="nowhere"),
                                   self.fixtures.answered(PROMISE)])
        result = self.send(self.start(), "台灯的事有人管吗？")[1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.model.calls), 4)
        # B5g: the gap the guard emptied still shows where to check (the general entry: the item is the whole message).
        self.assertEqual(result["reply"], code_copy.ITEM_LINES["gap"] + "\n\n" + code_copy.PATH_LABEL
                         + code_copy.PATHS["platform_human"])

    def test_two_wrong_shapes_fail_the_turn_and_write_nothing(self):
        before = self.counts()
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered(CLEAN, path="nowhere"),
                                   self.fixtures.answered(CLEAN, path="nowhere")])
        result = self.send(self.start(), "台灯的事找谁？")[1]
        self.assertEqual(result["status"], "model_error")
        self.assertEqual([row["detail"] for row in result["trace"] if row["type"] == "structure_correction"],
                         ["G9_PATH_UNKNOWN", "G9_PATH_UNKNOWN"])
        self.assertEqual(self.counts(), before)

    def test_with_the_prose_slot_off_no_words_are_shown_and_the_codes_lines_stand_in(self):
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered(CLEAN, path="merchant_contact")])
        with patch("support_agent.respond.PROSE_SLOT_ENABLED", False):
            result = self.send(self.start(), "台灯的事找谁？")[1]
        self.assertNotIn(CLEAN, result["reply"])
        self.assertEqual([(block["type"], block["author"]) for block in result["presentation"]["blocks"]],
                         [("text", "code"), ("path", "code")])
        self.assertEqual(next(row["hits"] for row in result["trace"] if row["type"] == "guards"), [])
        self.assertEqual(next(row["reason"] for row in result["trace"] if row["type"] == "model_words_withheld"),
                         "prose_slot_disabled")

    def test_a_claim_a_record_backs_passes_and_the_same_claim_without_it_does_not(self):
        sid = self.start()
        status, clicked = self.request("POST", f"/sessions/{sid}/cards", {"order_id": "O1001", "request_id": "card"})
        self.assertEqual(status, 200, clicked)
        self.assertEqual(self.confirm(sid, clicked["card"])[0], 200)
        backed = "O1001 这单退货已经提交，处理进度可以在上面看到。"
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered(backed, show=["O1001"])])
        result = self.send(sid, "杯子那单怎么样了？")[1]
        self.assertEqual(next(row["hits"] for row in result["trace"] if row["type"] == "guards"), [])
        self.assertIn(backed, result["reply"])
        unbacked = "O1004 这单退货已经提交。"
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered(unbacked)])
        result = self.send(sid, "收纳盒那单呢？", "second")[1]
        hits = next(row["hits"] for row in result["trace"] if row["type"] == "guards")
        self.assertIn("G1_UNBACKED_COMPLETION", [hit["code"] for hit in hits])
        self.assertNotIn(unbacked, result["reply"])

    def test_an_order_id_of_another_account_in_the_prose_is_a_hit(self):
        self.model.outputs.extend([self.fixtures.planned(), self.fixtures.answered("O2001 这一单我看一下。")])
        result = self.send(self.start(), "帮我看看那一单")[1]
        first = next(row["hits"] for row in result["trace"] if row["type"] == "guards")
        self.assertIn("G6_FOREIGN_ORDER", [hit["code"] for hit in first])
        self.assertNotIn("O2001", result["reply"])


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class RouteTurnTests(unittest.TestCase):
    """K23: a turn with a route result shows the code's route block and no note or next step (B5f)."""

    def setUp(self):
        from support_agent.policy import PublicPolicyConfig
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        from tests.test_public_policy_bundles import bundle_fixture
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = PublicPolicyConfig(*bundle_fixture(root, route_rows=[
            ["广东", "北京", "12", "北京", "广东", "10", "广东", "湖南", "10"]]))
        self.model = ScriptedModel([])
        self.server = create_server(db_path=root / "graph.sqlite3", credentials={"local-token": "alice"}, port=0,
                                    public_policy_config=config, model_factory=ScriptedFactory(self.model),
                                    log_path=root / "requests.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_the_route_block_is_shown_and_the_notes_are_withheld(self):
        import http.client
        from tests.test_graph_agent import answered, planned

        def call(path, body):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
            connection.request("POST", path, json.dumps(body, ensure_ascii=False).encode(),
                               {"Authorization": "Bearer local-token", "Content-Type": "application/json"})
            data = json.loads(connection.getresponse().read())
            connection.close()
            return data
        prose = "从广东寄回北京大概要十二元。"
        self.model.outputs.extend([planned(queries=["退回运费补偿"]), answered(prose)])
        session = call("/sessions", {})["session_id"]
        result = call(f"/sessions/{session}/messages",
                      {"message": "东西从广东寄回北京，按运费表给我个参考标准。", "request_id": "route"})
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(prose, result["reply"])
        blocks = result["presentation"]["blocks"]
        self.assertIn(("route", "code"), [(block["type"], block["author"]) for block in blocks])
        self.assertFalse(any(block["type"] == "text" for block in blocks))   # no note, no code line, no fallback
        self.assertIn("route_prose_withheld", [row["type"] for row in result["trace"]])
        context = json.loads(self.model.calls[1]["messages"][1]["content"])
        self.assertEqual((context["route"]["shown_by_code"], context["route"]["prose_shown"]), (True, False))


if __name__ == "__main__":
    unittest.main()
