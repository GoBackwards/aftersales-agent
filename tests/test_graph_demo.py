"""Offline keyword stand-in: proposals are schema-valid and quote the user verbatim.

This checks the demo transport only; it is not language understanding and its
behaviour is never reported as model accuracy. B7: the stand-in plans ``plan_turn`` items like the real planner
(a withdrawal word drops an open card, a pause word keeps it). B7r: a switch in one message is a withdrawal plus a
return, so the demo can show it end to end.
"""
import importlib.util
import json
import unittest

ALICE = [{"order_id": "O1003", "product_name": "学习用饼干礼盒"}, {"order_id": "O1001", "product_name": "学习用保温杯"},
         {"order_id": "O1004", "product_name": "学习用收纳盒"}, {"order_id": "O1002", "product_name": "学习用帆布包"}]


@unittest.skipUnless(importlib.util.find_spec("pydantic") is not None, "optional graph runtime not installed")
class KeywordDemoModelTests(unittest.TestCase):
    def setUp(self):
        from support_agent import demo_model
        self.demo = demo_model

    def plan(self, message, history=None, open_cards=()):
        return self.demo.plan_for({"current_message": message, "history": history or [], "account_orders": ALICE,
                                   "session_cards": {"open_card_order_ids": list(open_cards),
                                                     "submitted_order_ids": []}})

    def assert_valid_and_verbatim(self, plan, message):
        from support_agent import understand
        parsed = understand.parse(plan, [row["order_id"] for row in ALICE])
        for item in parsed["items"]:
            for words in (item["quote"], item["order_quote"]):
                if words:
                    self.assertIn(words, message)
        return parsed["items"]

    def test_with_several_questions_each_cites_only_rules_sharing_its_wording(self):
        cards = [{"id": "gift", "title": "赠品怎么退回", "scope": "赠品", "outcomes": ["赠品一并退回"],
                  "outcome_premises": [["主商品退货"]]},
                 {"id": "clock", "title": "时限怎么计算", "scope": "时限", "outcomes": ["时限从次日计算"],
                  "outcome_premises": [["京东配送"]]}]
        items = [{"index": 1, "kind": "policy_question", "quote": "时限怎么计算"},
                 {"index": 2, "kind": "policy_question", "quote": "赠品怎么退回"}]
        answer = self.demo.answer_for({"turn_items": items, "policy_cards": cards})
        self.assertEqual([[claim["card_id"] for claim in item["claims"]] for item in answer["items"]],
                         [["clock"], ["gift"]])
        self.assertEqual({item["disposition"] for item in answer["items"]}, {"answered"})

    def test_order_lookup_matches_whole_order_ids_only(self):
        message = "帮我查一下订单 O1001 和 O1002，XO1003 与 O10045 不是订单号"
        items = self.assert_valid_and_verbatim(self.plan(message), message)
        self.assertEqual([(item["kind"], item["order_id"]) for item in items],
                         [("own_order_status", "O1001"), ("own_order_status", "O1002")])

    def test_another_accounts_id_stays_in_the_words(self):
        message = "我要退货 O2001"
        items = self.assert_valid_and_verbatim(self.plan(message), message)
        self.assertEqual([(item["kind"], item["order_quote"], item["order_id"]) for item in items],
                         [("return_request", "O2001", "none")])

    def test_return_request_quotes_intent_and_order_and_sends_no_reason(self):
        message = "我要退货 O1001，原因：尺寸不合适"
        items = self.assert_valid_and_verbatim(self.plan(message), message)
        self.assertEqual([(item["kind"], item["quote"], item["order_quote"], item["order_id"]) for item in items],
                         [("return_request", "我要退货", "O1001", "O1001")])
        # B3a: the reason is picked on the card; B7: a plan has no field for one.
        self.assertNotIn("尺寸不合适", json.dumps(self.plan(message)["items"], ensure_ascii=False))

    def test_a_follow_up_with_only_an_order_continues_the_requested_return(self):
        history = [{"role": "user", "content": "我要退货"},
                   {"role": "assistant", "content": "请选择要退货的订单："}]
        items = self.plan("O1004", history=history)["items"]
        self.assertEqual([(item["kind"], item["quote"]) for item in items], [("return_request", "O1004")])
        self.assertEqual([item["kind"] for item in self.plan("O1004")["items"]], ["own_order_status"])

    def test_a_withdrawal_word_drops_a_card_and_a_pause_keeps_it(self):
        withdrawn = self.plan("这张不退了", open_cards=["O1001"])["items"]
        self.assertEqual([(item["kind"], item["quote"]) for item in withdrawn], [("withdraw", "不退了")])
        paused = self.plan("先不要提交", open_cards=["O1001"])["items"]   # K21: chit-chat, the card stays
        self.assertEqual([(item["kind"], item["quote"]) for item in paused], [("chit_chat", "先不要提交")])
        self.assertEqual(self.plan("不退了")["items"], [])                # nothing to withdraw
        answer = self.demo.answer_for({"turn_items": [{"index": 1, "kind": "chit_chat", "quote": "先不要提交"}],
                                       "policy_cards": [{"id": "x", "title": "提交", "scope": "提交",
                                                         "outcomes": ["先不要提交"], "outcome_premises": [["前提"]]}]})
        self.assertEqual(answer["items"], [{"index": 1, "disposition": "answered", "claims": [], "path_id": "none",
                                            "note": ""}])

    def test_a_switch_in_one_message_is_a_withdrawal_of_one_order_and_a_return_of_the_other(self):
        message = "O1001 那张作废，改退 O1004"
        items = self.assert_valid_and_verbatim(self.plan(message, open_cards=["O1001"]), message)
        self.assertEqual([(item["kind"], item["order_id"]) for item in items],
                         [("withdraw", "O1001"), ("return_request", "O1004")])
        unnamed = self.assert_valid_and_verbatim(self.plan("这张不退了，改退别的", open_cards=["O1001"]), "这张不退了，改退别的")
        self.assertEqual([(item["kind"], item["order_id"]) for item in unnamed],
                         [("withdraw", "none"), ("return_request", "none")])

    def test_a_withdrawal_by_product_words_names_that_card_and_never_the_one_asked_for(self):
        message = "收纳盒这张卡就不退了"
        items = self.assert_valid_and_verbatim(self.plan(message, open_cards=["O1004"]), message)
        self.assertEqual([(item["kind"], item["order_quote"], item["order_id"]) for item in items],
                         [("withdraw", message, "O1004")])
        switch = "收纳盒这张卡就不退了，改退保温杯"
        items = self.assert_valid_and_verbatim(self.plan(switch, open_cards=["O1004"]), switch)
        self.assertEqual([(item["kind"], item["order_id"]) for item in items],
                         [("withdraw", "O1004"), ("return_request", "none")])
        backwards = "改退保温杯，收纳盒这张卡就不退了"
        items = self.assert_valid_and_verbatim(self.plan(backwards, open_cards=["O1004"]), backwards)
        self.assertEqual([(item["kind"], item["order_id"]) for item in items],
                         [("withdraw", "none"), ("return_request", "none")])

    def test_policy_questions_and_fee_subjects(self):
        message = "京东自营的赠品需要和主商品一起退回吗？"
        items = self.assert_valid_and_verbatim(self.plan(message), message)
        self.assertEqual([(item["kind"], item["quote"]) for item in items], [("policy_question", message)])
        shipping = self.plan("我自己寄回的退货运费谁承担？")
        self.assertEqual([item["fee_subject"] for item in shipping["items"]], ["return_shipping"])
        self.assertEqual(shipping["fee_flow"], "return_self_ship")
        self.assertEqual([item["fee_subject"] for item in self.plan("运费怎么算？")["items"]], ["unknown"])
        self.assertEqual([item["fee_subject"] for item in self.plan("维修寄修的运费谁出？")["items"]],
                         ["repair_shipping"])
        mixed = "我要退货 O1001，原因：质量问题。赠品要一起退回吗？"
        items = self.assert_valid_and_verbatim(self.plan(mixed), mixed)
        self.assertEqual([item["kind"] for item in items], ["return_request", "policy_question"])
        self.assertEqual(self.plan("你好")["items"], [])

    def test_answer_answers_every_item_once_and_writes_no_words(self):
        cards = [{"id": "gift", "title": "主商品退货时的赠品", "scope": "赠品随主商品退回", "outcomes": ["赠品应一并退回"],
                  "outcome_premises": [["带赠品的主商品退货"]]},
                 {"id": "fee", "title": "退货运费承担", "scope": "运费", "outcomes": ["按责任承担运费"],
                  "outcome_premises": [["退货"]]},
                 {"id": "unmapped", "title": "赠品退回未映射", "scope": "赠品退回", "outcomes": ["赠品退回"],
                  "outcome_premises": [[]]}]
        items = [{"index": 1, "kind": "return_request", "quote": "我要退货"},
                 {"index": 2, "kind": "policy_question", "quote": "赠品要退回吗？"}]
        answer = self.demo.answer_for({"turn_items": items, "policy_cards": cards})
        self.assertEqual(answer["items"], [
            {"index": 1, "disposition": "handled_by_system", "claims": [], "path_id": "none", "note": ""},
            {"index": 2, "disposition": "answered", "claims": [{"card_id": "gift", "outcome_index": 0}],
             "path_id": "none", "note": ""}])
        self.assertEqual((answer["next_step"], answer["ask"]), ("", {"text": "", "option_set_id": "none"}))
        empty = self.demo.answer_for({"turn_items": items, "policy_cards": []})
        # B5e: a question no card matches gets no path (the script cannot know the right one); B5f: and no words, the
        # code's line for a gap stands in for it.
        self.assertEqual(empty["items"][1], {"index": 2, "disposition": "gap", "claims": [], "path_id": "none",
                                             "note": ""})
        self.assertEqual((empty["next_step"], empty["ask"]), ("", {"text": "", "option_set_id": "none"}))
        self.assertNotIn("asked", {item["disposition"] for item in answer["items"] + empty["items"]})

    def test_model_wrapper_returns_the_forced_tool_call_without_token_usage(self):
        from support_agent import understand
        model = self.demo.KeywordDemoModel()
        context = understand.context("查一下 O1001", [], ALICE, {"open_cards": [], "return_request": None})
        # B5a: the demo returns the fields the call's schema offers, as a strict provider does.
        response = model.complete([{"role": "system", "content": "x"}, {"role": "user", "content": json.dumps(context)}],
                                  [{"type": "function", "function": {"name": understand.PLAN_TURN,
                                                                     "parameters": understand.schema(["O1001"])}}])
        self.assertEqual(model.mode, "offline_keyword_demo")
        self.assertEqual(response.tool_calls[0].name, "plan_turn")
        self.assertEqual([item["kind"] for item in response.tool_calls[0].arguments["items"]], ["own_order_status"])
        self.assertEqual(response.usage["total_tokens"], 0)

    def test_the_scripted_transport_demo_plans_no_item(self):
        from support_agent import understand
        response = self.demo.DemoModel().complete(
            [{"role": "system", "content": "x"}, {"role": "user", "content": "{}"}],
            [{"type": "function", "function": {"name": understand.PLAN_TURN, "parameters": understand.schema([])}}])
        self.assertEqual(understand.parse(response.tool_calls[0].arguments, [])["items"], [])


class KeywordDemoSwitchTurnTests(unittest.TestCase):
    def test_the_offline_demo_switches_a_card_to_another_order_in_one_turn(self):
        from pathlib import Path
        import tempfile
        from support_agent.turn import ConversationAgent
        from support_agent.demo_model import KeywordDemoModel
        from support_agent.sessions import SessionStore
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "switch.sqlite3")
            try:
                sid = store.create_session("alice")["session_id"]
                agent = ConversationAgent(KeywordDemoModel(), store, "alice")
                agent.send(sid, "O1001 申请退货", "demo-1")
                result = agent.send(sid, "O1001 那张作废，改退 O1004", "demo-2")
                self.assertEqual(result["status"], "completed")
                self.assertEqual([card["order_id"] for card in store.get_session("alice", sid)["open_cards"]],
                                 ["O1004"])
                voided = store._connection.execute(
                    "SELECT order_id, void_reason FROM return_cards WHERE status = 'voided'").fetchall()
                self.assertEqual([tuple(row) for row in voided], [("O1001", "chat_withdraw")])
                self.assertEqual(store.list_return_requests(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
