"""What the planner's proposal goes through before the answer (B7: plan_turn items).

A fee object the plan or the wording settles (a stated return flow; a home pickup with no repair or purchase words)
becomes the returned item's shipping, and for the wording rule the reply says so in one code sentence; a settled fee
question still reaches the cards; a plan over the item limit is a broken shape that goes back once (B7 deleted the
fee-topic conflict check and the advisory lists with the fields they checked). B5d2 moved these checks here from the
fix-named tests it deleted. Scripted models, synthetic wording; mechanics, not answer quality.
"""
from contextlib import ExitStack
import importlib.util
import json
import unittest
from unittest.mock import patch

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None


def synthetic_card(domain="return_shipping_benefit"):
    return {"id": "synthetic_fee_rule", "title": "合成运费规则", "scope": "仅用于工程测试", "effect_domain": domain,
            "conditions": ["测试条件"], "outcomes": ["按合成权益核对退回费用。"], "exceptions": [],
            "evidence_ids": ["example-source"]}


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class PlanChecksHTTPTests(unittest.TestCase):
    from tests import test_graph_agent as fixtures
    setUp = fixtures.GraphHTTPTests.setUp
    stop = fixtures.GraphHTTPTests.stop
    request = fixtures.GraphHTTPTests.request
    start = fixtures.GraphHTTPTests.start
    send = fixtures.GraphHTTPTests.send

    def cards_available(self, cards):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("support_agent.policy.PolicyAdapter.catalogue_cards", return_value=cards))
        stack.enter_context(patch("support_agent.policy.PolicyAdapter.source_cards", return_value=[]))

    def plan(self, *items, **fields):
        plan = self.fixtures.planned(list(items))
        plan.tool_calls[0].arguments.update(fields)
        return plan

    def test_a_stated_return_flow_settles_an_unknown_fee_subject_before_the_cards_are_read(self):
        card = synthetic_card()
        self.cards_available([card])
        message = "自己寄回去的话，这笔费用权益怎样核对？"
        question = self.fixtures.item("policy_question", message, fee_subject="unknown")
        self.model.outputs.extend([self.plan(question, fee_flow="return_self_ship"),
                                   self.fixtures.answered("", claims=[{"card_id": card["id"], "outcome_index": 0}])])
        result = self.send(self.start(), message)[1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.model.calls), 2)
        resolved = next(row for row in result["trace"] if row["type"] == "fee_object_resolved")
        self.assertEqual((resolved["rule"], resolved["note"], resolved["items"]), ("planner_fee_flow", None, [1]))
        self.assertIn(card["outcomes"][0], result["reply"])
        context = json.loads(self.model.calls[1]["messages"][1]["content"])
        self.assertEqual({item["fee_subject"] for item in context["turn_items"]}, {"return_shipping"})

    def test_a_home_pickup_fee_question_is_read_as_a_return_pickup_and_the_reply_says_so(self):
        from support_agent import copy as code_copy
        message = "叫快递员来家里上门取货，费用怎么算"
        self.model.outputs.extend([self.plan(self.fixtures.item("policy_question", message, fee_subject="unknown")),
                                   self.fixtures.answered("")])
        result = self.send(self.start(), message)[1]
        resolved = next(row for row in result["trace"] if row["type"] == "fee_object_resolved")
        self.assertEqual((resolved["rule"], resolved["fee_flow"]), ("home_pickup_wording", "return_home_pickup"))
        # The plan stays the model's proposal; the code's settlement is its own trace entry.
        self.assertEqual(result["graph"]["plan"]["items"][0]["fee_subject"], "unknown")
        context = json.loads(self.model.calls[1]["messages"][1]["content"])
        self.assertEqual([item["fee_subject"] for item in context["turn_items"]], ["return_shipping"])
        self.assertEqual(result["reply"].count(code_copy.ASSUMED_RETURN_PICKUP), 1)
        note = next(block for block in result["presentation"]["blocks"] if block["text"] == code_copy.ASSUMED_RETURN_PICKUP)
        self.assertEqual(note["author"], "code")

    def test_a_settled_fee_question_still_reaches_the_cards_and_a_fee_conclusion_in_a_note_never_shows(self):
        from support_agent import copy as code_copy
        message = "快递员上门取货，运费归谁付"
        # (Recall gap noted in B5d2: wordings like "需要另外付费" or "要收费" are not G3 hits; guards change in their
        # own stage, principle 8. This test uses a wording G3 detects.)
        self.model.outputs.extend([self.plan(self.fixtures.item("policy_question", message, fee_subject="unknown")),
                                   self.fixtures.answered("上门取件的运费由您承担。")])
        result = self.send(self.start(), message)[1]
        self.assertEqual(next(row for row in result["trace"] if row["type"] == "fee_object_resolved")["rule"],
                         "home_pickup_wording")
        self.assertIn("policy", result["graph"]["nodes"])
        self.assertEqual(result["policy_status"], "insufficient_evidence")
        # A fee conclusion in a note is a guard hit (G3): it never reaches the reply (B5f: the gap item's code line
        # stands in for that note, no whole-reply fallback).
        self.assertNotIn("由您承担", result["reply"])
        self.assertIn("G3_COST", [hit["code"] for row in result["trace"] if row["type"] == "guards"
                                  for hit in row["hits"]])
        self.assertIn(code_copy.ITEM_LINES["gap"], result["reply"])
        self.assertFalse(result["answer_validation"]["fallback"])

    def test_a_plan_over_the_item_limit_is_a_broken_shape_that_goes_back_once(self):
        from support_agent.understand import MAX_ITEMS
        message = "签收以后多久内能退"
        question = self.fixtures.item("policy_question", message)
        self.model.outputs.extend([self.plan(*[question] * (MAX_ITEMS + 1)), self.plan(question),
                                   self.fixtures.answered("")])
        result = self.send(self.start(), message)[1]
        self.assertEqual(result["status"], "completed")
        corrections = [row for row in result["trace"] if row["type"] == "structure_correction"]
        self.assertEqual([row["node"] for row in corrections], ["understand"])
        self.assertEqual(len(next(row for row in result["trace"] if row["type"] == "plan")["plan"]["items"]), 1)
        self.assertNotIn("advisory_list_trimmed", [row["type"] for row in result["trace"]])


if __name__ == "__main__":
    unittest.main()
