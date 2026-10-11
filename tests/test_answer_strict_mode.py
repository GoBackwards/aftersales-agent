"""B5a, carried into B5d2: the answer call (now ``respond``) goes through strict tool mode (plan §8.6 B5a, §5.2).

Strict mode makes the provider write every field a function offers and cannot carry length limits. So the
answer offers only the lists that have something to choose from this turn, states in text the limits the model
reads nowhere else, and the code still checks every limit (guards.G9; B5f: an id outside the turn's lists goes back
for one correction, a list past its limit is trimmed and recorded instead). An answer without the lists that were not
offered reads exactly like one that carries them empty. No request reaches a real provider. Offline engineering evidence only: not task success, not
semantic accuracy.
"""
from contextlib import ExitStack
import http.client
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
FREE_FIELDS = {"items", "next_step", "ask"}
ITEM_FIELDS = {"index", "disposition", "path_id", "note"}
# Keywords a strict schema may carry (the DeepSeek strict subset the planner already uses, plus nothing new).
STRICT_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "description",
                   "pattern", "minimum", "maximum", "anyOf"}


def card(card_id, *, claimable=True):
    """A synthetic two-branch card; without branch premises no outcome can be claimed."""
    value = {"id": card_id, "title": "合成规则" + card_id, "scope": "仅用于工程测试", "conditions": ["测试条件"],
             "outcomes": ["测试结果甲。", "测试结果乙。"], "exceptions": [], "evidence_ids": ["example-source"]}
    if claimable:
        value["outcome_premises"] = [["测试前提甲"], ["测试前提乙"]]
    return value


def items(count):
    return [{"index": index, "kind": "question", "quote": "测试问项", "orders": [], "fee_subject": "none"}
            for index in range(1, count + 1)]


def walk(schema, path="$"):
    """(path, object) for every schema object inside a schema."""
    if isinstance(schema, dict):
        yield path, schema
        for key, value in schema.items():
            if key == "properties":
                for name, item in value.items():
                    yield from walk(item, f"{path}.{name}")
            elif isinstance(value, (dict, list)):
                yield from walk(value, f"{path}/{key}")
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            yield from walk(item, f"{path}[{index}]")


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class AnswerOfferTests(unittest.TestCase):
    def schema(self, cards=(), count=1, orders=()):
        from support_agent.respond import respond_schema
        return respond_schema(items(count), list(cards), list(orders))

    def test_a_turn_without_cards_or_orders_offers_the_items_the_next_step_and_the_ask(self):
        schema = self.schema()
        self.assertEqual(set(schema["properties"]), FREE_FIELDS)
        self.assertEqual(set(schema["properties"]["items"]["items"]["properties"]), ITEM_FIELDS)

    def test_each_list_is_offered_only_when_the_turn_has_something_to_choose(self):
        self.assertEqual(set(self.schema([card("a")])["properties"]["items"]["items"]["properties"]),
                         ITEM_FIELDS | {"claims"})
        self.assertEqual(set(self.schema([card("a", claimable=False)])["properties"]["items"]["items"]["properties"]),
                         ITEM_FIELDS)
        self.assertEqual(set(self.schema(orders=["O1001"])["properties"]), FREE_FIELDS | {"show_order_ids"})

    def test_offered_lists_keep_exactly_this_turn_choices(self):
        from support_agent import copy as code_copy
        properties = self.schema([card("a"), card("b", claimable=False)], count=2, orders=["O1001"])["properties"]
        item = properties["items"]["items"]["properties"]
        self.assertEqual(item["index"], {"type": "integer", "enum": [1, 2]})
        self.assertEqual(item["claims"]["items"]["properties"]["card_id"]["enum"], ["a"])
        self.assertEqual(item["path_id"]["enum"], ["none", *code_copy.PATHS])
        self.assertEqual(properties["show_order_ids"]["items"]["enum"], ["O1001"])
        self.assertEqual(properties["ask"]["properties"]["option_set_id"]["enum"], ["none", *code_copy.OPTION_SETS])

    def test_every_strict_form_uses_only_what_strict_mode_accepts(self):
        from support_agent.models import strict_schema
        variants = {"none": self.schema(), "cards": self.schema([card("a")]),
                    "unclaimable": self.schema([card("a", claimable=False)]),
                    "orders": self.schema(orders=["O1001"]), "both": self.schema([card("a"), card("b")], 3, ["O1001"])}
        for label, schema in variants.items():
            sent = strict_schema(schema)
            for path, node in walk(sent):
                with self.subTest(variant=label, path=path):
                    self.assertLessEqual(set(node) - {"properties"}, STRICT_KEYWORDS | {"properties"})
                    self.assertNotEqual(node.get("enum"), [], "an empty choice set cannot be sent")
                    self.assertFalse("enum" in node and "pattern" in node, "enum and pattern together")
                    if node.get("type") == "object":
                        self.assertEqual(node["required"], list(node["properties"]))
                        self.assertIs(node["additionalProperties"], False)
                    if node.get("type") == "array":
                        self.assertIn("items", node)

    def test_the_claim_limits_are_written_where_strict_mode_can_send_them_and_checked_by_the_code(self):
        from support_agent import guards
        from support_agent.models import strict_schema
        sent = strict_schema(self.schema([card("a")]))["properties"]["items"]["items"]["properties"]["claims"]
        self.assertNotIn("maxItems", sent)
        self.assertIn(f"至多 {guards.CLAIMS_PER_ITEM} 个", sent["description"])
        self.assertIn(f"至多 {guards.CLAIMS_PER_ANSWER} 个", sent["description"])
        four = [{"card_id": name, "outcome_index": index} for name in ("a", "b") for index in (0, 1)]
        answer = {"items": [{"index": 1, "disposition": "answered", "claims": four, "path_id": "none", "note": ""}],
                  "show_order_ids": [], "next_step": "", "ask": {"text": "", "option_set_id": "none"}}
        problems, tidied, dropped = guards.check_structure(answer, {"items": [{"index": 1}],
                                                                           "cards": {"a": card("a"), "b": card("b")}})
        self.assertEqual(problems, [])
        self.assertIn("G9_CLAIMS_PER_ITEM", {row["code"] for row in dropped})
        self.assertLessEqual(len(tidied["items"][0]["claims"]), guards.CLAIMS_PER_ITEM)

    def test_an_answer_without_the_unoffered_lists_reads_like_one_with_them_empty(self):
        from support_agent.respond import parse
        schema = self.schema()
        strict_shaped = {"items": [{"index": 1, "disposition": "gap", "path_id": "none", "note": "好的。"}],
                         "next_step": "", "ask": {"text": "", "option_set_id": "none"}}
        self.assertEqual(parse(strict_shaped, schema),
                         {**strict_shaped, "items": [{**strict_shaped["items"][0], "claims": []}], "show_order_ids": []})


def provider_reply(name, arguments):
    return io.BytesIO(json.dumps({"model": "deepseek-flash", "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                                                      "total_tokens": 7},
                                  "choices": [{"finish_reason": "tool_calls", "message": {
                                      "role": "assistant", "content": None, "tool_calls": [{
                                          "id": "call-" + name, "type": "function", "function": {
                                              "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}]},
                                 ensure_ascii=False).encode("utf-8"))


class Factory:
    mode = "test_double_provider"

    def __init__(self, model):
        self.model = model

    def __call__(self):
        return self.model


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class AnswerStrictRequestTests(unittest.TestCase):
    """The real adapter and service, with the provider's HTTP endpoint replaced by canned replies."""

    def run_turn(self, *, strict_tools, base_url, answer):
        from support_agent.models import APIConfig, ChatCompletionsModel
        from support_agent.service import create_server
        opener = Mock()
        opener.open.side_effect = [provider_reply("plan_turn", {"goal": "了解能办理的事", "items": [],
                                                                "order_stage": "unknown", "fee_flow": "unknown"}),
                                   provider_reply("respond", answer)]
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(APIConfig(base_url, "deepseek-flash", "sk-test-only", provider="deepseek",
                                                   strict_tools=strict_tools))
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(db_path=Path(directory) / "s.sqlite3", credentials={"t": "alice"}, port=0,
                                   model_factory=Factory(model), log_path=Path(directory) / "r.jsonl")
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                def call(path, body):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
                    connection.request("POST", path, json.dumps(body, ensure_ascii=False).encode(),
                                       {"Authorization": "Bearer t", "Content-Type": "application/json"})
                    data = json.loads(connection.getresponse().read())
                    connection.close()
                    return data
                session = call("/sessions", {})["session_id"]
                result = call(f"/sessions/{session}/messages", {"message": "你好，想先问问。", "request_id": "m1"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        bodies = [json.loads(call_args.args[0].data) for call_args in opener.open.call_args_list]
        return result, bodies

    ANSWER = {"items": [{"index": 1, "disposition": "asked", "path_id": "none", "note": ""}], "show_order_ids": [],
              "next_step": "", "ask": {"text": "你想先解决哪件事？", "option_set_id": "none"}}

    def test_the_provider_receives_both_functions_in_strict_form(self):
        result, bodies = self.run_turn(strict_tools=True, base_url="https://api.deepseek.com/beta", answer=self.ANSWER)
        self.assertEqual((result["status"], result["reply"]), ("completed", "你想先解决哪件事？"))
        self.assertEqual([body["tools"][0]["function"]["name"] for body in bodies], ["plan_turn", "respond"])
        for body in bodies:
            self.assertIs(body["tools"][0]["function"]["strict"], True)
        parameters = bodies[1]["tools"][0]["function"]["parameters"]
        # B5b: the customer has orders, so the answer also offers show_order_ids (an enum of her own order ids).
        self.assertEqual(parameters["required"], ["items", "show_order_ids", "next_step", "ask"])
        self.assertIs(parameters["additionalProperties"], False)
        sent = json.dumps(parameters, ensure_ascii=False)
        for keyword in ("maxItems", "minItems", "maxLength", "minLength", "default"):
            self.assertNotIn(f'"{keyword}"', sent)

    def test_without_strict_mode_the_answer_function_is_sent_without_the_strict_flag(self):
        result, bodies = self.run_turn(strict_tools=False, base_url="https://api.deepseek.com", answer=self.ANSWER)
        self.assertEqual(result["status"], "completed")
        for body in bodies:
            self.assertNotIn("strict", body["tools"][0]["function"])
        self.assertEqual(bodies[1]["tools"][0]["function"]["parameters"]["required"],
                         ["items", "show_order_ids", "next_step", "ask"])


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class AnswerLimitCorrectionTests(unittest.TestCase):
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
        stack.enter_context(patch("support_agent.policy.PolicyAdapter.source_cards",
                                  return_value=[{"id": "example-source"}]))

    def test_an_over_long_claim_list_is_trimmed_in_order_without_a_second_call(self):
        # B5f (spec point 4): only an id outside the turn's lists goes back; a list past its limit is untidy, so the
        # first outcomes are kept, the rest dropped and recorded (B5a sent it back once; R109 lost turns to that).
        cards = [card(name) for name in ("a", "b", "c", "d")]
        self.cards_available(cards)
        too_many = [{"card_id": name, "outcome_index": 0} for name in ("a", "b", "c", "d")]
        self.model.outputs.extend([self.fixtures.planned(queries=["合成规则的适用结果"]),
                                   self.fixtures.answered("", claims=too_many)])
        status, result = self.send(self.start(), "请说明合成规则的结果。")
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotIn("structure_correction", [entry["type"] for entry in result["trace"]])
        tidied = next(entry for entry in result["trace"] if entry["type"] == "structure_tidied")
        self.assertEqual(tidied["dropped"], [{"code": "G9_CLAIMS_PER_ITEM", "part": "claim:1"}])
        self.assertEqual(result["graph"]["selection"]["items"][0]["claims"], too_many[:3])
        self.assertEqual([block["card_id"] for block in result["presentation"]["blocks"]], ["a", "b", "c"])
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class DemoModelStrictShapeTests(unittest.TestCase):
    """The offline demo stands in for a strict provider: each proposal carries exactly the offered fields."""

    def tools(self, name, parameters):
        from support_agent.models import ToolDefinitions
        return ToolDefinitions([{"type": "function", "function": {"name": name, "parameters": parameters}}],
                               tool_choice={"type": "function", "function": {"name": name}}, strict_functions={name})

    def ask(self, model, name, parameters, context):
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        return model.complete(messages, self.tools(name, parameters)).tool_calls[0].arguments

    def test_both_offline_demos_return_exactly_the_offered_answer_fields(self):
        from support_agent.demo_model import DemoModel, KeywordDemoModel
        from support_agent.respond import respond_schema
        context = {"turn_items": items(1), "policy_cards": [], "conversation": {"current_user_message": "你好"}}
        for model in (KeywordDemoModel(), DemoModel()):
            with self.subTest(model=type(model).__name__):
                arguments = self.ask(model, "respond", respond_schema(items(1), [], []), context)
                self.assertEqual(set(arguments), FREE_FIELDS)
                self.assertEqual(set(arguments["items"][0]), ITEM_FIELDS)
        policy = self.ask(KeywordDemoModel(), "respond", respond_schema(items(1), [card("a")], ["O1001"]),
                          {**context, "policy_cards": [card("a")]})
        self.assertEqual(set(policy), FREE_FIELDS | {"show_order_ids"})
        self.assertEqual(set(policy["items"][0]), ITEM_FIELDS | {"claims"})

    def test_the_demo_plan_still_carries_every_planning_field(self):
        from support_agent import understand
        from support_agent.demo_model import KeywordDemoModel
        schema = understand.schema(["O1001"])
        context = understand.context("查一下 O1001", [], [{"order_id": "O1001", "product_name": "杯子"}],
                                     {"open_cards": [], "return_request": None})
        arguments = self.ask(KeywordDemoModel(), understand.PLAN_TURN, schema, context)
        self.assertEqual(set(arguments), set(schema["properties"]))
        self.assertEqual(set(arguments["items"][0]), set(understand.ITEM_FIELDS))
        self.assertEqual(arguments["items"][0]["kind"], "own_order_status")


if __name__ == "__main__":
    unittest.main()
