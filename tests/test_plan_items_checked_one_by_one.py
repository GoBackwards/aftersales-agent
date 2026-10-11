"""The planner's items are checked one by one (B7, plan §2.3 step 4, K20): a broken shape goes back once, an invalid
item is dropped on its own and the others go on; no item gets a correction call of its own.

Shape (understand.parse): exact fields, values from the enums (order_id from the account's own ids and three words),
the limits strict mode cannot send. Items (understand.check): the words are the user's (a business item quotes the
current message only, K5), a read names its order in those words. Pure functions: they read no store and change no
input. Through the agent: a dropped return or withdrawal leaves the code's one sentence and changes no card; a dropped
question leaves the message to the answer. Wording is written for these tests; mechanics, not model quality.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from support_agent import understand

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
OWN = ["O1001", "O1004"]
CATALOG = {"orders": [{"order_id": "O1001", "product_name": "学习用保温杯"},
                      {"order_id": "O1004", "product_name": "学习用收纳盒"}]}


def row(kind="policy_question", quote="运费谁出", **fields):
    return {"kind": kind, "subkind": "none", "quote": quote, "history_index": None, "order_quote": "",
            "order_id": "none", "fee_subject": "none", **fields}


def arguments(*items, **fields):
    return {"goal": "按用户原话列出的事项", "items": list(items), "order_stage": "unknown", "fee_flow": "unknown",
            **fields}


class ShapeTests(unittest.TestCase):
    def test_a_well_formed_plan_is_numbered_from_one_and_kept_whole(self):
        plan = understand.parse(arguments(row(), row("return_request", "退了吧", order_id="O1001")), OWN)
        self.assertEqual([item["index"] for item in plan["items"]], [1, 2])
        self.assertEqual(plan["items"][1]["order_id"], "O1001")
        self.assertEqual(set(plan), {"goal", "items", "order_stage", "fee_flow"})

    def test_a_broken_shape_is_a_value_error(self):
        broken = {
            "a missing field": {key: value for key, value in arguments(row()).items() if key != "fee_flow"},
            "an extra field": {**arguments(row()), "capability": "public_policy"},
            "an unknown kind": arguments(row("save_draft")),
            "an order outside the account": arguments(row("return_request", order_id="O2001")),
            "a history index out of range": arguments(row(history_index=16)),
            "a history index that is no number": arguments(row(history_index=True)),
            "an empty goal": arguments(row(), goal=" "),
            "a goal over the limit": arguments(row(), goal="长" * 601),
            "too many items": arguments(*[row()] * (understand.MAX_ITEMS + 1)),
            "a stage outside the enum": arguments(row(), order_stage="maybe"),
            "an item without its fields": arguments({"kind": "chit_chat", "quote": "你好"}),
            "an item with a reason": arguments({**row("return_request"), "reason": "不想要了"}),
        }
        for name, value in broken.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                understand.parse(value, OWN)

    def test_a_subkind_counts_only_for_the_kinds_that_read_it(self):
        plan = understand.parse(arguments(row(subkind="comparison"), row("safety", "冒烟了", subkind="product_hazard"),
                                          row("chit_chat", "你好", subkind="crisis"),
                                          row(subkind="money_dispute")), OWN)
        self.assertEqual([item["subkind"] for item in plan["items"]], ["comparison", "product_hazard", "none", "none"])

    def test_the_schema_offers_only_the_accounts_orders_and_no_reason_or_draft(self):
        schema = understand.schema(OWN)
        item = schema["properties"]["items"]["items"]["properties"]
        self.assertEqual(item["order_id"]["enum"], ["O1001", "O1004", "all_mine", "current", "none"])
        self.assertEqual(list(item), list(understand.ITEM_FIELDS))
        self.assertNotIn("reason", json.dumps(schema))
        self.assertEqual(schema["required"], ["goal", "items", "order_stage", "fee_flow"])

    def test_the_prompt_is_in_production_voice_without_dates_or_eligibility(self):
        for text in (understand.PROMPT, understand.TRUST_NOTE):
            for word in ("模拟", "演示", "教学", "本地", "京东", "签收日", "最后可退日", "第几天"):
                self.assertNotIn(word, text)
        self.assertLessEqual(len(understand.PROMPT), 3000)   # plan §8.6 B7


class ItemCheckTests(unittest.TestCase):
    def check(self, message, *items, history=(), open_cards=()):
        plan = understand.parse(arguments(*items), OWN)
        before = deepcopy(plan)
        kept, rejected = understand.check(plan, message, list(history), CATALOG, list(open_cards))
        self.assertEqual(plan, before)   # pure
        return kept, rejected

    def test_an_item_whose_words_are_not_the_users_is_dropped_and_the_others_stay(self):
        kept, rejected = self.check("运费谁出？顺便退了 O1001", row(quote="运费谁出"),
                                    row("return_request", "上次说的那单", order_quote="O1001", order_id="O1001"))
        self.assertEqual([item["index"] for item in kept], [1])
        self.assertEqual(rejected, [{"index": 2, "kind": "return_request", "code": "QUOTE_NOT_IN_MESSAGE"}])

    def test_a_business_item_quotes_the_current_message_only(self):
        history = [{"role": "user", "content": "O1001 帮我退了"}, {"role": "assistant", "content": "好的"}]
        kept, rejected = self.check("就按刚才的办", row("return_request", "O1001 帮我退了", history_index=0,
                                                        order_quote="O1001", order_id="O1001"),
                                    history=history)
        self.assertEqual((kept, [r["code"] for r in rejected]), ([], ["CURRENT_MESSAGE_ONLY"]))

    def test_a_question_may_go_on_from_an_earlier_user_message_of_this_session_only(self):
        history = [{"role": "user", "content": "赠品要一起寄回吗"}, {"role": "assistant", "content": "要看页面说明"},
                   {"role": "user", "content": "以前问过保修", "source_kind": "historical_external"}]
        kept, rejected = self.check("补充一下，是满减送的", row(quote="赠品要一起寄回吗", history_index=0),
                                    row(quote="要看页面说明", history_index=1), row(quote="不存在", history_index=5),
                                    row(quote="以前问过保修", history_index=2), history=history)
        self.assertEqual([item["index"] for item in kept], [1])
        self.assertEqual([r["code"] for r in rejected], ["HISTORY_NOT_A_USER_MESSAGE"] * 3)

    def test_a_read_must_name_its_order_in_the_users_words(self):
        message = "保温杯那单现在什么情况，还有 2001 那单，以及我名下全部订单"
        kept, rejected = self.check(
            message,
            row("own_order_status", "保温杯那单", order_quote="保温杯那单", order_id="O1001"),
            row("own_order_status", "现在什么情况", order_quote="收纳盒", order_id="O1004"),
            row("own_order_status", "2001 那单", order_quote="2001"),
            row("own_order_status", "我名下全部订单", order_id="all_mine"),
            row("own_order_status", "现在什么情况"),
            row("own_order_status", "现在什么情况", order_id="current"))
        self.assertEqual([item["index"] for item in kept], [1, 3, 4])
        self.assertEqual([(r["index"], r["code"]) for r in rejected],
                         [(2, "ORDER_NOT_SOURCED"), (5, "NO_ORDER"), (6, "NO_ORDER")])
        kept, _ = self.check("这张卡对应的订单再看看", row("own_order_status", "这张卡", order_id="current"),
                             open_cards=["O1001"])
        self.assertEqual(len(kept), 1)

    def test_a_list_of_all_orders_is_no_read_when_the_message_types_another_accounts_order(self):
        _, rejected = self.check("O2001 和我的全部订单", row("own_order_status", "我的全部订单", order_id="all_mine"))
        self.assertEqual([r["code"] for r in rejected], ["FOREIGN_ORDER_TYPED"])

    def test_a_return_target_the_words_do_not_name_is_kept_for_code_to_resolve(self):
        # K5: an unnamed or unsourced target is not an error; the open card, K5 or the candidates decide later.
        kept, rejected = self.check("那就退了吧", row("return_request", "退了吧", order_quote="保温杯", order_id="O1001"),
                                    row("withdraw", "退了吧"))
        self.assertEqual((len(kept), rejected), (2, []))

    def test_a_business_item_reads_no_fee_subject(self):
        kept, _ = self.check("退了吧，运费谁出", row("return_request", "退了吧", fee_subject="return_shipping"),
                             row(quote="运费谁出", fee_subject="return_shipping"))
        self.assertEqual([item["fee_subject"] for item in kept], ["none", "return_shipping"])


class FeeRuleTests(unittest.TestCase):
    def settle(self, message, fee_flow="unknown", subject="unknown"):
        plan = understand.parse(arguments(row(quote=message, fee_subject=subject), fee_flow=fee_flow), OWN)
        items = deepcopy(plan["items"])
        return understand.settle_fee_subjects(plan, items, message), plan, items

    def test_a_stated_return_flow_settles_an_unknown_fee_subject(self):
        settled, plan, items = self.settle("寄回去的运费怎么算", fee_flow="return_self_ship")
        self.assertEqual((settled["rule"], settled["note"]), ("planner_fee_flow", None))
        self.assertEqual(items[0]["fee_subject"], "return_shipping")

    def test_a_home_pickup_with_no_other_service_is_read_as_a_return_pickup(self):
        settled, plan, items = self.settle("叫人上门取件要花钱吗")
        self.assertEqual((settled["rule"], settled["note"], settled["fee_flow"], plan["fee_flow"]),
                         ("home_pickup_wording", "pickup_return_assumed", "return_home_pickup", "unknown"))
        self.assertEqual(items[0]["fee_subject"], "return_shipping")

    def test_repair_or_purchase_words_or_a_known_subject_settle_nothing(self):
        for message, subject in (("上门取件寄修要花钱吗", "unknown"), ("下单时上门取件费多少", "unknown"),
                                 ("上门取件要花钱吗", "purchase_shipping")):
            with self.subTest(message=message):
                settled, _, items = self.settle(message, subject=subject)
                self.assertIsNone(settled)
                self.assertEqual(items[0]["fee_subject"], subject)


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class DroppedItemsThroughTheAgentTests(unittest.TestCase):
    """ConversationAgent on a fresh store with a scripted model: what a dropped item leaves behind."""

    def setUp(self):
        from support_agent.sessions import SessionStore
        from tests.test_graph_agent import ScriptedModel
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / "plan.sqlite3")
        self.addCleanup(self.store.close)
        self.sid = self.store.create_session("alice")["session_id"]
        self.model = ScriptedModel([])

    def send(self, message, request_id, *outputs):
        from support_agent.turn import ConversationAgent
        self.model.outputs.extend(outputs)
        return ConversationAgent(self.model, self.store, "alice").send(self.sid, message, request_id)

    def test_a_dropped_return_says_so_once_changes_no_card_and_makes_no_extra_call(self):
        from support_agent import copy as code_copy
        from tests.test_graph_agent import answered, item, planned
        self.send("O1001 申请退货", "card", planned([item("return_request", "申请退货", order="O1001")]))
        before = self.store.get_session("alice", self.sid)["open_cards"]
        result = self.send("运费谁出？顺便把收纳盒退了", "dropped",
                           planned([item("policy_question", "运费谁出？", fee_subject="return_shipping"),
                                    item("return_request", "前面说好的", order="收纳盒", order_id="O1004")]),
                           answered("要看订单页上的服务说明。", disposition="gap"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reply"].count(code_copy.ITEM_DEGRADED), 1)
        self.assertEqual(self.store.get_session("alice", self.sid)["open_cards"], before)
        self.assertEqual([call["tools"][0]["function"]["name"] for call in self.model.calls],
                         ["plan_turn", "plan_turn", "respond"])
        context = json.loads(self.model.calls[-1]["messages"][1]["content"])
        self.assertEqual([row["kind"] for row in context["turn_items"]], ["policy_question"])
        self.assertEqual(context["verified"]["actions"], [{"kind": "request_not_done", "order_ids": []}])
        self.assertEqual([row["code"] for row in result["trace"] if row["type"] == "item_rejected"],
                         ["QUOTE_NOT_IN_MESSAGE"])

    def test_a_dropped_question_beside_a_card_leaves_the_message_to_the_answer(self):
        from tests.test_graph_agent import answered, item, planned
        result = self.send("O1001 申请退货，另外运费怎么算", "card-and-question",
                           planned([item("return_request", "申请退货", order="O1001"),
                                    item("policy_question", "别处的问题")]),
                           answered("运费要看订单页上的服务说明。", disposition="gap"))
        self.assertEqual(result["confirmation"]["status"], "available")
        context = json.loads(self.model.calls[-1]["messages"][1]["content"])
        self.assertEqual([row["kind"] for row in context["turn_items"]], ["return_request", "message"])

    def test_a_broken_shape_twice_fails_the_turn_and_writes_nothing(self):
        from tests.test_graph_agent import response
        result = self.send("在吗", "broken", response("plan_turn", arguments(row("save_draft", "在吗"))),
                           response("plan_turn", arguments(row("chit_chat", "在吗"), order_stage="later")))
        self.assertEqual(result["status"], "model_error")
        self.assertIsNone(self.store.get_session("alice", self.sid)["card"])
        self.assertEqual(self.store.list_return_requests(), [])


if __name__ == "__main__":
    unittest.main()
