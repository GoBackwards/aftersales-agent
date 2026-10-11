"""B5b (kept by B5d2): the answer reads every own order as verified facts and may show some; the code renders them.

Before the answer call the code reads all of the customer's orders with their return-rule facts (receipt day,
condition, the rule outcome and its code, the last return date, an existing request by status) into the
``verified`` part of the respond context; the planner never sees them (plan K3). ``show_order_ids`` may name only
the customer's own orders (an enum; another id is a structure error, G9), and the code renders a read-only facts
block for them, after the turn's action blocks and before the model's prose, without repeating an order the
business block already lists. Nothing here writes an order, a card, a request or a ticket. Offline engineering
evidence only: not task success, not semantic accuracy.
"""
import importlib.util
import json
from pathlib import Path
import unittest

from support_agent import copy as code_copy

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
APP = Path(__file__).resolve().parents[1]
ALICE_ORDERS = ["O1003", "O1001", "O1004", "O1002"]   # newest delivery first
FACT_KEYS = {"order_id", "product_name", "category", "status", "delivered_at", "days_since_delivery", "condition",
             "return_eligible", "eligibility_code", "last_return_date", "return_request"}
# Words only the verified facts carry; the planner's context must contain none of them (K3).
FACT_WORDS = ("customer_orders", "eligibility_code", "last_return_date", "days_since_delivery", "return_eligible")


def order_line_head(order_id):
    """The start of the code-written order line (assemble.order_line) for one seed order."""
    from support_agent.domain import SEED_ORDERS
    order = next(order for order in SEED_ORDERS if order.order_id == order_id)
    return f"订单 {order.order_id}：{order.product_name}，"


class OrderFactsTests(unittest.TestCase):
    """domain.order_facts: one order's verified facts, the rule decided in one place."""

    def facts(self, order_id, request=None):
        from support_agent.domain import SEED_ORDERS, order_facts
        return order_facts(next(order for order in SEED_ORDERS if order.order_id == order_id), request)

    def test_each_seed_order_carries_receipt_day_condition_outcome_and_last_return_date(self):
        expected = {"O1001": (3, "sealed", True, "eligible", "2026-09-22"),
                    "O1002": (17, "sealed", False, "outside_window", "2026-09-08"),
                    "O1003": (1, "inspected_only", False, "food_opened", "2026-09-24"),
                    "O1004": (7, "sealed", True, "eligible", "2026-09-18"),
                    "O2001": (2, "sealed", True, "eligible", "2026-09-23")}
        for order_id, (days, condition, eligible, code, last) in expected.items():
            with self.subTest(order=order_id):
                facts = self.facts(order_id)
                self.assertEqual(set(facts), FACT_KEYS)
                self.assertEqual((facts["days_since_delivery"], facts["condition"], facts["return_eligible"],
                                  facts["eligibility_code"], facts["last_return_date"], facts["return_request"]),
                                 (days, condition, eligible, code, last, None))

    def test_the_facts_agree_with_the_rule_the_cards_and_order_panel_use(self):
        from support_agent import domain, return_cards, tickets
        from support_agent.domain import SEED_ORDERS, order_view
        # One place for the rule's last day and outcome: the card module and the ticket summary read the same ones.
        self.assertIs(return_cards.last_return_date, domain.last_return_date)
        self.assertIs(tickets.order_rule_code, domain.order_rule_code)
        for order in SEED_ORDERS:
            with self.subTest(order=order.order_id):
                facts, view = domain.order_facts(order), order_view(order)
                self.assertEqual(facts["eligibility_code"], view["eligibility_code"])
                self.assertEqual(facts["return_eligible"], view["return_eligible"])
                self.assertEqual(facts["last_return_date"], view["last_return_date"])
                self.assertEqual(facts["days_since_delivery"], view["days_since_delivery"])

    def test_an_existing_request_is_carried_by_its_status_only(self):
        request = {"id": "RET-O1001", "order_id": "O1001", "status": "pending", "created_at": "2026-10-07T00:00:00Z",
                   "reason_code": "other", "reason_text": "不应出现的说明"}
        self.assertEqual(self.facts("O1001", request)["return_request"], {"status": "pending"})


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class AnswerOrderChoiceSchemaTests(unittest.TestCase):
    def test_the_choice_is_an_enum_of_the_customers_own_order_ids_and_absent_without_orders(self):
        from support_agent.respond import respond_schema
        offered = respond_schema([{"index": 1}], [], ALICE_ORDERS)["properties"]["show_order_ids"]
        self.assertEqual(offered["items"]["enum"], ALICE_ORDERS)
        self.assertEqual(offered["type"], "array")
        self.assertNotIn("show_order_ids", respond_schema([{"index": 1}], [], [])["properties"])

    def test_the_strict_form_requires_the_choice_and_sends_no_length_keyword(self):
        from support_agent.models import strict_schema
        from support_agent.respond import respond_schema
        sent = strict_schema(respond_schema([{"index": 1}], [], ["O2001"]))
        self.assertIn("show_order_ids", sent["required"])
        self.assertEqual(sent["properties"]["show_order_ids"]["items"], {"type": "string", "enum": ["O2001"]})
        self.assertFalse({"maxItems", "minItems", "default"} & set(sent["properties"]["show_order_ids"]))

    def test_an_answer_without_the_choice_offered_reads_as_showing_nothing(self):
        from support_agent.respond import parse, respond_schema
        schema = respond_schema([{"index": 1}], [], [])
        value = parse({"items": [{"index": 1, "disposition": "gap", "path_id": "none", "note": "好的。"}],
                       "next_step": "", "ask": {"text": "", "option_set_id": "none"}}, schema)
        self.assertEqual((value["show_order_ids"], value["items"][0]["claims"]), ([], []))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class OrderFactsInAnswerTests(unittest.TestCase):
    from tests import test_graph_agent as fixtures
    setUp = fixtures.GraphHTTPTests.setUp
    stop = fixtures.GraphHTTPTests.stop
    request = fixtures.GraphHTTPTests.request
    start = fixtures.GraphHTTPTests.start
    send = fixtures.GraphHTTPTests.send
    confirm = fixtures.GraphHTTPTests.confirm

    def answer(self, prose, **choices):
        return self.fixtures.answered(prose, **choices)

    def context(self, call):
        return json.loads(self.model.calls[call]["messages"][1]["content"])

    def counts(self):
        with self.server.store() as store:
            return {table: store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("return_requests", "return_cards", "tickets")}

    def start_as(self, token):
        status, result = self.request("POST", "/sessions", {}, token=token)
        self.assertEqual(status, 201, result)
        return result["session_id"]

    def test_the_answer_context_carries_every_own_order_as_verified_facts(self):
        self.model.outputs.extend([self.fixtures.planned(), self.answer("你想先了解哪一件？")])
        status, result = self.send(self.start(), "随便聊聊，先不急。")
        self.assertEqual((status, result["status"]), (200, "completed"))
        verified = self.context(1)["verified"]
        self.assertEqual(verified["business_date"], "2026-09-18")
        self.assertEqual([row["order_id"] for row in verified["customer_orders"]], ALICE_ORDERS)
        self.assertTrue(all(set(row) == FACT_KEYS for row in verified["customer_orders"]))
        by_id = {row["order_id"]: row for row in verified["customer_orders"]}
        self.assertEqual((by_id["O1004"]["days_since_delivery"], by_id["O1004"]["last_return_date"]), (7, "2026-09-18"))
        self.assertEqual((by_id["O1003"]["return_eligible"], by_id["O1003"]["eligibility_code"]), (False, "food_opened"))
        # The answer may choose among exactly these orders.
        schema = self.model.calls[1]["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["show_order_ids"]["items"]["enum"], ALICE_ORDERS)

    def test_the_planner_never_sees_the_facts(self):
        self.model.outputs.extend([self.fixtures.planned(), self.answer("你想先了解哪一件？")])
        self.send(self.start(), "随便聊聊，先不急。")
        planner = self.model.calls[0]["messages"][1]["content"]
        for word in FACT_WORDS:
            self.assertNotIn(word, planner)
        context = json.loads(planner)
        self.assertNotIn("backend_observation", context)   # B7: the planner context has no backend block at all
        self.assertEqual({tuple(sorted(row)) for row in context["account_orders"]}, {("order_id", "product_name")})
        self.assertNotIn("show_order_ids", json.dumps(self.model.calls[0]["tools"], ensure_ascii=False))

    def test_another_customer_reads_and_may_show_only_their_own_orders(self):
        # A non-strict provider naming someone else's order is a structure error (G9): one correction, never shown.
        self.model.outputs.extend([self.fixtures.planned(), self.answer("你的订单在上面。", show=["O1001", "O2001"]),
                                   self.answer("你的订单在上面。", show=["O2001"])])
        status, result = self.send(self.start_as("graph-local-bob"), "我名下的东西还能退吗", token="graph-local-bob")
        self.assertEqual((status, result["status"]), (200, "completed"))
        answer_request = self.model.calls[1]["messages"][1]["content"]
        self.assertEqual([row["order_id"] for row in self.context(1)["verified"]["customer_orders"]], ["O2001"])
        for other in ALICE_ORDERS:
            self.assertNotIn(other, answer_request)
            self.assertNotIn(other, result["reply"])
        schema = self.model.calls[1]["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["show_order_ids"]["items"]["enum"], ["O2001"])
        self.assertEqual(next(row["detail"] for row in result["trace"] if row["type"] == "structure_correction"),
                         "G9_ORDER_NOT_OWN")
        facts = [block for block in result["presentation"]["blocks"] if block["type"] == "order_facts"]
        self.assertEqual([[row["order_id"] for row in block["orders"]] for block in facts], [["O2001"]])

    def test_a_chosen_order_is_rendered_by_code_before_the_words_and_writes_nothing(self):
        before = self.counts()
        prose = "这一单的情况列在上面。"   # B5f: an answered item carries no note; the turn's next step may say this
        self.model.outputs.extend([self.fixtures.planned(),
                                   self.answer("", disposition="answered", show=["O1004"], next_step=prose)])
        status, result = self.send(self.start(), "那个盒子还来得及吗")
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(len(self.model.calls), 2)
        lines = result["reply"].split("\n")
        self.assertTrue(lines[0].startswith(order_line_head("O1004")), lines)
        self.assertTrue(lines[0].endswith(code_copy.RETURNABLE_LAST_DAY))
        self.assertEqual(lines[1:], [code_copy.APPLY_HINT, "", prose])
        blocks = result["presentation"]["blocks"]
        self.assertEqual([(block["type"], block["author"]) for block in blocks],
                         [("order_facts", "code"), ("text", "model")])
        self.assertEqual([row["order_id"] for row in blocks[0]["orders"]], ["O1004"])
        self.assertEqual((blocks[0]["note"], blocks[0]["text"]), (code_copy.APPLY_HINT, "\n".join(lines[:2])))
        self.assertEqual(result["graph"]["selection"]["show_order_ids"], ["O1004"])
        self.assertEqual(result["confirmation"]["status"], "unavailable")
        self.assertEqual(self.counts(), before)

    def test_without_a_choice_no_order_facts_are_shown(self):
        prose = "你想先了解哪一件？"
        self.model.outputs.extend([self.fixtures.planned(), self.answer(prose)])
        status, result = self.send(self.start(), "随便问问。")
        # B5g: the scripted gap names no path, so the code adds the general verification entry; no order facts.
        self.assertEqual((status, result["reply"]),
                         (200, prose + "\n\n" + code_copy.PATH_LABEL + code_copy.PATHS["platform_human"]))
        self.assertEqual([block["type"] for block in result["presentation"]["blocks"]], ["text", "path"])
        self.assertFalse(any(row["type"] == "order_facts_shown" for row in result["trace"]))

    def test_an_order_the_business_block_lists_is_not_shown_twice_nor_its_next_step(self):
        lookup = self.fixtures.business("lookup_order", order="O1004")
        plan = self.fixtures.planned([*lookup, self.fixtures.item("policy_question", "过了第七天还行吗")])
        answer = self.answer("我这边的资料没写到过了期限以后怎么办。", path="policy_page", show=["O1004", "O1001"])
        self.model.outputs.extend([plan, answer])
        status, result = self.send(self.start(), "O1004 过了第七天还行吗")
        self.assertEqual((status, result["status"]), (200, "completed"), result)
        blocks = result["presentation"]["blocks"]
        self.assertEqual([block["type"] for block in blocks][:2], ["business", "order_facts"])
        self.assertEqual([row["order_id"] for row in blocks[1]["orders"]], ["O1001"])
        self.assertEqual(blocks[1]["note"], "")
        self.assertEqual(result["reply"].count("O1004："), 1)
        self.assertEqual(result["reply"].count(code_copy.APPLY_HINT), 1)
        self.assertLess(result["reply"].index("O1004："), result["reply"].index("O1001："))

    def test_a_submitted_request_reaches_the_answer_by_status_and_its_reason_never_does(self):
        sid = self.start()
        other = "杯盖颜色跟页面上那张图对不上"
        status, clicked = self.request("POST", f"/sessions/{sid}/cards", {"order_id": "O1001", "request_id": "card"})
        self.assertEqual(status, 200, clicked)
        status, submitted = self.confirm(sid, clicked["card"], reason_code="other", reason_text=other)
        self.assertEqual(status, 200, submitted)
        self.model.outputs.extend([self.fixtures.planned(), self.answer("状态见上面。", show=["O1001"])])
        status, result = self.send(sid, "杯子那单现在什么状态")
        self.assertEqual((status, result["status"]), (200, "completed"))
        by_id = {row["order_id"]: row for row in self.context(1)["verified"]["customer_orders"]}
        self.assertEqual(by_id["O1001"]["return_request"], {"status": "pending"})
        for call in self.model.calls:
            text = json.dumps(call["messages"], ensure_ascii=False)
            self.assertNotIn(other, text)
            self.assertNotIn("reason_code", text)
        self.assertIn("已经有一条退货申请，当前状态：待处理。", result["reply"])
        self.assertNotIn(other, result["reply"])

    def test_a_pure_business_turn_still_skips_the_answer(self):
        lookup = self.fixtures.business("lookup_order", order="O1001")
        self.model.outputs.append(self.fixtures.planned(lookup))
        status, result = self.send(self.start(), "查一下 O1001")
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(any(row["type"] == "order_facts_shown" for row in result["trace"]))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class DemoModelOrderChoiceTests(unittest.TestCase):
    """The offline keyword demo shows the own orders a message names; the scripted demo shows none."""

    def ask(self, model, message, order_ids):
        from support_agent.domain import SEED_ORDERS, order_facts
        from support_agent.models import ToolDefinitions
        from support_agent.respond import RESPOND, respond_schema
        orders = [order_facts(order) for order in SEED_ORDERS if order.order_id in order_ids]
        items = [{"index": 1, "kind": "message", "quote": message, "orders": [], "fee_subject": "none"}]
        context = {"turn_items": items, "policy_cards": [], "verified": {"customer_orders": orders},
                   "conversation": {"current_user_message": message}}
        tools = ToolDefinitions([{"type": "function", "function": {
            "name": RESPOND, "parameters": respond_schema(items, [], [row["order_id"] for row in orders])}}],
            tool_choice={"type": "function", "function": {"name": RESPOND}}, strict_functions={RESPOND})
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        return model.complete(messages, tools).tool_calls[0].arguments

    def test_the_keyword_demo_shows_the_own_orders_a_message_names_by_id_or_product(self):
        from support_agent.demo_model import KeywordDemoModel
        model, alice = KeywordDemoModel(), ["O1001", "O1002", "O1003", "O1004"]
        self.assertEqual(self.ask(model, "O1004 和 O2001 能看看吗", alice)["show_order_ids"], ["O1004"])
        self.assertEqual(self.ask(model, "那只保温杯怎么样了", alice)["show_order_ids"], ["O1001"])
        self.assertEqual(self.ask(model, "你好", alice)["show_order_ids"], [])
        self.assertNotIn("show_order_ids", self.ask(model, "O1004 呢", []))

    def test_the_scripted_demo_writes_the_offered_choice_empty(self):
        from support_agent.demo_model import DemoModel
        self.assertEqual(self.ask(DemoModel(), "O1004 呢", ["O1004"])["show_order_ids"], [])


class PageDrawsDeclaredOrderFactsTests(unittest.TestCase):
    def test_the_chat_page_draws_an_order_facts_block_with_the_order_cards(self):
        script = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")
        self.assertIn('case "order_facts": return orderFactsBlock(block);', script)
        body = script[script.index("function orderFactsBlock"):script.index("function cardBlock")]
        self.assertIn("orderCard(order, { compact: true, notes: true, apply: true })", body)
        self.assertNotIn("innerHTML", body)


if __name__ == "__main__":
    unittest.main()
