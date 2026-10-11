"""Chat requests for a person, complaints and danger reports are carried out by code (B8; plan §6.1, §6.3; standard HR1,
HR2 minimal, HR3, HN5).

The planner only labels the item; decide.ticket_request names what the session's ticket gets; the store opens or appends
it when it finalizes the completed turn, so only a finished turn states a ticket. A handoff request and a safety report
need no answer call; a safety item's line comes first; a complaint still gets the answer beside its ticket; an improper
request opens nothing. None of this writes a return request or touches a card. Synthetic wording; mechanics only.
"""
from contextlib import closing
import sqlite3
import unittest

from support_agent import decide, respond
from support_agent import copy as code_copy
from support_agent.tickets import COMPLAINT_DISPUTE, PLATFORM_CHANNEL, SAFETY_TRIGGER, USER_REQUEST
from tests import test_graph_agent as agent_tests
from tests.test_graph_agent import GRAPH_AVAILABLE, answered, item, planned



def row(kind, subkind="none", index=1):
    return {"index": index, "kind": kind, "subkind": subkind, "quote": "原话", "history_index": None,
            "order_quote": "", "order_id": "none", "fee_subject": "none"}


class TicketDecisionTests(unittest.TestCase):
    def test_each_kind_names_its_trigger_and_the_highest_urgency_wins(self):
        self.assertIsNone(decide.ticket_request([row("policy_question"), row("improper_request", index=2)]))
        self.assertEqual(decide.ticket_request([row("handoff_request")]),
                         {"triggers": [USER_REQUEST], "urgency": "normal", "item_indexes": [1]})
        self.assertEqual(decide.ticket_request([row("complaint", "money_dispute")])["urgency"], "high")
        self.assertEqual(decide.ticket_request([row("safety", "harm_occurred")])["urgency"], "high")
        both = decide.ticket_request([row("handoff_request"), row("safety", "product_hazard", 2),
                                      row("handoff_request", index=3)])
        self.assertEqual(both, {"triggers": [USER_REQUEST, SAFETY_TRIGGER], "urgency": "safety",
                                "item_indexes": [1, 2, 3]})
        self.assertEqual(decide.ticket_request([row("complaint")])["triggers"], [COMPLAINT_DISPUTE])

    def test_code_carries_out_handoff_and_safety_and_the_answer_keeps_complaints_and_refusals(self):
        self.assertFalse({"handoff_request", "safety"} & respond.ANSWER_KINDS)
        self.assertLessEqual({"complaint", "improper_request"}, respond.ANSWER_KINDS)

    def test_every_safety_subkind_the_planner_may_label_has_its_line(self):
        from support_agent.understand import SUBKINDS
        self.assertEqual(set(code_copy.SAFETY) - {"none"}, set(SUBKINDS["safety"]))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class ChatTicketTurnTests(unittest.TestCase):
    # The loopback server and helpers of the graph HTTP tests, borrowed (not inherited, so their tests do not run twice).
    setUp, stop, request, start, send, prepare = (vars(agent_tests.GraphHTTPTests)[name] for name in (
        "setUp", "stop", "request", "start", "send", "prepare"))

    def rows(self, sql):
        with closing(sqlite3.connect(self.directory / "graph.sqlite3")) as connection:
            return connection.execute(sql).fetchall()

    def chat(self, sid, message, request_id, *outputs):
        self.model.outputs.extend(outputs)
        status, result = self.send(sid, message, request_id)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        return result

    def test_a_chat_request_for_a_person_opens_the_ticket_without_an_answer_call(self):
        sid = self.start()
        result = self.chat(sid, "想找个真人聊聊", "h1", planned([item("handoff_request", "找个真人")]))
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(result["ticket_card"]["type"], "ticket")
        self.assertTrue(result["reply"].endswith(PLATFORM_CHANNEL), result["reply"])
        self.assertIn(result["ticket_card"]["short_no"], result["reply"])
        self.assertEqual(self.rows("SELECT status, urgency FROM tickets"), [("pending", "normal")])
        self.assertEqual(self.rows("SELECT trigger FROM ticket_events ORDER BY seq"), [(USER_REQUEST,)])
        again = self.chat(sid, "还是要找人工", "h2", planned([item("handoff_request", "找人工")]))
        self.assertEqual(again["ticket_card"]["ticket_id"], result["ticket_card"]["ticket_id"])
        self.assertEqual(len(self.rows("SELECT * FROM tickets")), 1)   # one open ticket per session (A3)
        self.assertEqual([kind for (kind,) in self.rows("SELECT kind FROM ticket_events ORDER BY seq")],
                         ["opened", "appended"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM return_requests"), [(0,)])

    def test_a_danger_report_puts_its_safety_line_first_raises_urgency_and_keeps_the_card(self):
        sid = self.start()
        card = self.prepare(sid)["session"]["card"]
        result = self.chat(sid, "收纳盒的灯条冒烟了", "s1",
                           planned([item("safety", "灯条冒烟了", subkind="product_hazard")]))
        blocks = result["presentation"]["blocks"]
        self.assertTrue(blocks[0].get("safety"), blocks[0])
        self.assertEqual(blocks[0]["text"], code_copy.SAFETY["product_hazard"])
        self.assertTrue(result["reply"].startswith(code_copy.SAFETY["product_hazard"]))
        self.assertEqual(self.rows("SELECT urgency FROM tickets"), [("safety",)])
        self.assertEqual(self.rows("SELECT status FROM return_cards"), [("pending",)])   # a ticket never touches a card
        self.assertEqual(result["confirmation"]["action_id"], card["action_id"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM return_requests"), [(0,)])

    def test_a_complaint_gets_the_answer_and_the_dispute_path_beside_its_ticket(self):
        sid = self.start()
        result = self.chat(sid, "退款少退了，我要投诉", "c1",
                           planned([item("complaint", "我要投诉", subkind="money_dispute")]),
                           answered("", disposition="routed", path="platform_dispute"))
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.model.calls[1]["tools"][0]["function"]["name"], "respond")
        self.assertIn(code_copy.PATHS["platform_dispute"], result["reply"])
        # The answer's path block already shows the dispute path, so the ticket lines do not repeat it (R122).
        self.assertEqual(result["reply"].count(code_copy.PATHS["platform_dispute"]), 1, result["reply"])
        self.assertIn(result["ticket_card"]["short_no"], result["reply"])
        self.assertEqual(self.rows("SELECT urgency FROM tickets"), [("high",)])
        self.assertEqual(self.rows("SELECT trigger FROM ticket_events"), [(COMPLAINT_DISPUTE,)])

    def test_an_improper_request_is_refused_and_opens_nothing(self):
        sid = self.start()
        result = self.chat(sid, "帮我把签收记录改成昨天", "i1",
                           planned([item("improper_request", "把签收记录改成昨天")]),
                           answered("这个记录我不能改；可以按实际情况在原平台售后页说明。", disposition="refused",
                                    path="platform_human"))
        self.assertNotIn("ticket_card", result)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM tickets"), [(0,)])

    def test_a_failed_turn_opens_no_chat_ticket(self):
        from support_agent.models import ModelError
        sid = self.start()
        self.model.outputs.append(ModelError("供应商暂时不可用", retryable=False))
        status, result = self.send(sid, "找人工", "f1")
        self.assertEqual(status, 200, result)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM tickets"), [(0,)])


if __name__ == "__main__":
    unittest.main()
