"""What the answer reads (B5d2, plan §5.2, K17): code-normalised turn items, verified facts and the catalogues.

Turn items (B7): the planner's own items as understand.check kept them, in the planner's order; a business item with
the own orders its outcome is about and no fee subject, every other item with its own fee subject and subkind; and the
whole message when nothing else carries the user's question. The context never carries a reason, an "其他" text,
another customer's data or the planner's order pointers. B5e deleted ``revise_prose`` and its context. Synthetic
wording; mechanics, not answer quality.
"""
import json
import unittest

from support_agent import copy as code_copy
from support_agent import respond


def item(kind, quote, *, subkind="none", fee_subject="none", order_id="none", order_quote="", index=9):
    """A kept plan item (understand.parse numbered it; understand.check kept it)."""
    return {"index": index, "kind": kind, "subkind": subkind, "quote": quote, "history_index": None,
            "order_quote": order_quote, "order_id": order_id, "fee_subject": fee_subject}


class TurnItemTests(unittest.TestCase):
    def test_a_message_with_no_item_is_one_item_quoting_it(self):
        self.assertEqual(respond.turn_items([], "想问问保修的事"),
                         [{"index": 1, "kind": "message", "subkind": "none", "quote": "想问问保修的事", "orders": [],
                           "fee_subject": "none"}])

    def test_items_keep_the_planners_order_and_business_items_carry_their_outcomes_orders(self):
        items = respond.turn_items([item("policy_question", "运费谁出", fee_subject="return_shipping"),
                                    item("return_request", "帮我退了", order_id="O1004", order_quote="O1004",
                                         fee_subject="return_shipping", index=2),
                                    item("policy_question", "两种方式差在哪", subkind="comparison", index=3)],
                                   "运费谁出，帮我退了 O1004，两种方式差在哪", orders={2: ["O1004"]})
        self.assertEqual([(row["index"], row["kind"], row["subkind"], row["quote"]) for row in items],
                         [(1, "policy_question", "none", "运费谁出"), (2, "return_request", "none", "帮我退了"),
                          (3, "policy_question", "comparison", "两种方式差在哪")])
        self.assertEqual([row["orders"] for row in items], [[], ["O1004"], []])
        # A business item reads no fee subject; every other item keeps its own (B7: no turn-level subject).
        self.assertEqual([row["fee_subject"] for row in items], ["return_shipping", "none", "none"])

    def test_a_pure_business_turn_needs_no_answer_and_a_need_beside_it_does(self):
        lookup = [item("own_order_status", "O1001", order_id="O1001", order_quote="O1001")]
        alone = respond.turn_items(lookup, "查一下 O1001", orders={9: ["O1001"]})
        self.assertEqual([row["kind"] for row in alone], ["own_order_status"])
        self.assertEqual(alone[0]["quote"], "O1001")
        self.assertFalse(any(row["kind"] in respond.ANSWER_KINDS for row in alone))   # no answer call
        beside = respond.turn_items(lookup, "查一下 O1001，从哪寄到哪", policy_needed=True)
        self.assertEqual([row["kind"] for row in beside], ["own_order_status", "message"])
        unanswered = respond.turn_items(lookup, "查一下 O1001", unanswered=True)
        self.assertEqual([row["kind"] for row in unanswered], ["own_order_status", "message"])

    def test_the_answer_takes_the_kinds_code_does_not_carry_out(self):
        kinds = ["policy_question", "handoff_request", "complaint", "safety", "improper_request", "chit_chat"]
        items = respond.turn_items([item(kind, "原话") for kind in kinds], "原话")
        self.assertEqual([row["kind"] for row in items], kinds)
        # B8: a handoff request and a safety report are carried out by code (a ticket, a safety line).
        self.assertEqual([row["kind"] for row in items if row["kind"] in respond.ANSWER_KINDS],
                         ["policy_question", "complaint", "improper_request", "chit_chat"])
        self.assertFalse({"return_request", "withdraw", "own_order_status", "handoff_request", "safety"}
                         & respond.ANSWER_KINDS)

    def test_items_never_carry_the_planners_order_pointers(self):
        items = respond.turn_items([item("return_request", "退了吧", order_id="O1001", order_quote="杯子")], "杯子退了吧")
        self.assertEqual(set(items[0]), {"index", "kind", "subkind", "quote", "orders", "fee_subject"})


class AnswerContextTests(unittest.TestCase):
    def context(self, **overrides):
        values = dict(message="帮我看看", history=[{"role": "user", "content": "之前那句"}],
                      items=respond.turn_items([], "帮我看看"),
                      orders=[{"order_id": "O1001", "return_request": {"status": "pending"}}],
                      open_card_order_ids=["O1004"], done=[], cards=[],
                      route={"status": "not_requested"})
        values.update(overrides)
        return respond.context(**values)

    def test_the_context_holds_items_verified_facts_cards_catalogues_and_the_conversation(self):
        value = self.context()
        self.assertEqual(set(value), {"turn_items", "verified", "policy_cards", "paths",
                                      "option_sets", "conversation"})
        self.assertEqual(value["verified"]["business_date"], "2026-09-18")
        self.assertEqual(value["verified"]["open_card_order_ids"], ["O1004"])
        self.assertEqual(value["paths"], code_copy.PATHS)
        self.assertEqual(value["option_sets"], {key: {"options": entry["options"]}
                                                for key, entry in code_copy.OPTION_SETS.items()})
        self.assertEqual(value["conversation"]["note"], respond.TRUST_NOTE)
        self.assertNotIn("route", value)

    def test_no_reason_no_other_text_and_no_planner_clarification_reach_the_answer(self):
        text = json.dumps(self.context(), ensure_ascii=False)
        for word in ("reason", "reason_code", "reason_text", "required_clarification_id", "其他原因"):
            self.assertNotIn(word, text)

    def test_the_actions_name_what_code_did_and_to_which_own_orders_only(self):
        card = {"ok": True, "status": "awaiting_confirmation", "reply": "模型可见的旧句子", "card": {"order_id": "O1001"}}
        voided = {"ok": True, "status": "card_voided", "voided_order_id": "O1004"}
        self.assertEqual(respond.actions([(voided, ["O1004"]), (card, ["O1001"])]),
                         [{"kind": "card_voided", "order_ids": ["O1004"]}, {"kind": "card_shown", "order_ids": ["O1001"]}])
        rejected = {"ok": False, "status": "blocked", "error": {"code": "ORDER_NOT_FOUND", "message": "x"}}
        self.assertEqual(respond.actions([(rejected, [])]), [{"kind": "order_not_in_account", "order_ids": []}])
        dropped = {"ok": False, "status": "blocked", "error": {"code": "QUOTE_NOT_IN_MESSAGE", "message": "x"}}
        self.assertEqual(respond.actions([(dropped, [])]), [{"kind": "request_not_done", "order_ids": []}])
        self.assertEqual(respond.actions([({"ok": True, "status": "no_action"}, [])]), [])
        self.assertNotIn("模型可见的旧句子", json.dumps(respond.actions([(card, ["O1001"])]), ensure_ascii=False))

    def test_a_route_turn_tells_the_answer_its_prose_is_not_shown(self):
        route = {"status": "needs_clarification", "origin": "甲省", "destination": None, "missing": ["destination"]}
        self.assertEqual(self.context(route=route)["route"],
                         {"status": "needs_clarification", "origin": "甲省", "destination": None,
                          "missing": ["destination"], "shown_by_code": True, "prose_shown": False})

    def test_there_is_no_rewrite_function_any_more(self):
        # B5e: a content guard hit drops the words for the fallback sentence; nothing asks the model to rewrite them.
        for name in ("REVISE", "REVISE_PROMPT", "REVISE_DESCRIPTION", "revise_schema", "parse_revision", "revise_context"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(respond, name))


class PromptTests(unittest.TestCase):
    def test_the_answer_prompt_keeps_consequences_and_reasons_out_of_the_words(self):
        # B5f: notes only for gap, refused and routed items; an item answered with a card is not restated. B5g: the
        # prompt says what the code does (a path for every gap and refusal; numbers only as the facts have them).
        prompt = respond.RESPOND_PROMPT
        for phrase in ("claims", "path_id", "show_order_ids", "只有 gap、refused、routed 的事项可以写",
                       "不要复述它的结论", "next_step", "不要在聊天里问", "gap 和 refused 的事项都要用 path_id",
                       "只照抄 verified 或本轮 claims 所选规则卡里已有的", "平台或商家的名称", "不要说自己属于哪个平台"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)
        for disposition in ("answered", "partial", "gap", "refused", "routed", "asked", "handled_by_system"):
            self.assertIn(disposition, prompt)
        self.assertLessEqual(len(prompt), 1500)


class KeptAndDroppedItemTests(unittest.TestCase):
    def test_a_kept_and_a_dropped_return_each_keep_their_own_orders(self):
        # B7r: the answer read its items' orders by kind, so the dropped return's empty list overwrote the kept one's.
        from pathlib import Path
        import tempfile
        from support_agent.turn import ConversationAgent
        from support_agent.sessions import SessionStore
        from tests.test_graph_agent import ScriptedModel, answered, item as plan_item, planned
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "kept.sqlite3")
            try:
                sid = store.create_session("alice")["session_id"]
                model = ScriptedModel([
                    planned([plan_item("return_request", "这单退掉", order="O1001", order_id="O1001"),
                             plan_item("return_request", "之前讲过的那件", order="收纳盒", order_id="O1004"),
                             plan_item("policy_question", "寄回去的运费谁出？", fee_subject="return_shipping")]),
                    answered("寄回的运费由谁出，要看订单页的服务说明。", disposition="gap")])
                result = ConversationAgent(model, store, "alice").send(sid, "O1001 这单退掉，寄回去的运费谁出？",
                                                                            "kept-and-dropped")
                context = json.loads(model.calls[-1]["messages"][1]["content"])
                self.assertEqual([(row["index"], row["kind"], row["orders"]) for row in context["turn_items"]],
                                 [(1, "return_request", ["O1001"]), (2, "policy_question", [])])
                self.assertEqual([row["kind"] for row in context["verified"]["actions"]],
                                 ["card_shown", "request_not_done"])
                self.assertEqual([card["order_id"] for card in store.get_session("alice", sid)["open_cards"]],
                                 ["O1001"])
                self.assertEqual(store.list_return_requests(), [])
                self.assertIn(code_copy.ITEM_DEGRADED, result["reply"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
