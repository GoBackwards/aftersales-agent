"""Turn orchestration and business invariants over real loopback HTTP and SQLite.

Models are deterministic local fixtures; these checks do not measure language quality.
"""
from copy import deepcopy
from unittest.mock import patch
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from support_agent.models import ModelResponse, ToolCall
from support_agent.models import ModelError
from support_agent.service import create_server


GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
ALICE = "graph-local-alice"
BOB = "graph-local-bob"


def item(kind, quote, *, order="", order_id="none", subkind="none", history_index=None, fee_subject="none"):
    """One plan_turn item (B7); ``order`` is the user's words naming the order (order_quote)."""
    return {"kind": kind, "subkind": subkind, "quote": quote, "history_index": history_index, "order_quote": order,
            "order_id": order_id, "fee_subject": fee_subject}


def business(action="request_return", *, quote="我要退货", order=None, order_id="none"):
    """The plan items of one business request (B7): a return (request_return), a withdrawal (withdraw) or a read
    (lookup_order: its quote is the order's words). The order is the user's words as typed (order_quote); with order_id
    "none" the code reads the order id the words type, as it does for any id outside the account's enum."""
    kind = {"request_return": "return_request", "withdraw": "withdraw", "lookup_order": "own_order_status"}[action]
    return [item(kind, order if action == "lookup_order" else quote, order=order or "", order_id=order_id)]


def planned(items=(), *, queries=None, goal="完成用户当前诉求"):
    """A scripted ``plan_turn`` reply (B7). ``queries``: a rules consultation, one policy question quoting the whole
    current message (then the reply is built from the call's context)."""
    def value(message):
        rows = list(items) + ([item("policy_question", message)] if queries else [])
        return {"goal": goal, "items": rows, "order_stage": "unknown", "fee_flow": "unknown"}
    if not queries:
        return response("plan_turn", value(""))
    return lambda messages, tools: response("plan_turn", value(json.loads(messages[1]["content"])["current_message"]))


def respond_value(context, note="", *, disposition=None, claims=(), path="none", ask=None, show=(), items=None,
                  next_step=""):
    """A ``respond`` result built from the call's own turn items (B5d2): a business item is handled by the system;
    the first item that needs the answer gets ``claims``, ``path``, ``disposition`` (default: answered with
    claims, else gap) and ``note`` (B5f); later ones are gaps without a note."""
    from support_agent.respond import ANSWER_KINDS
    rows, first = [], True
    for item in context["turn_items"]:
        if item["kind"] not in ANSWER_KINDS:
            rows.append({"index": item["index"], "disposition": "handled_by_system", "claims": [], "path_id": "none",
                         "note": ""})
            continue
        mine = list(claims) if first else []
        rows.append({"index": item["index"], "disposition": (disposition if first and disposition else
                                                             "answered" if mine else "gap"),
                     "claims": mine, "path_id": path if first else "none", "note": note if first else ""})
        first = False
    return {"items": rows if items is None else items, "show_order_ids": list(show), "next_step": next_step,
            "ask": ask or {"text": "", "option_set_id": "none"}}


def offered(value, tools):
    """Only the fields the call's schema offers, as a strict provider returns them (B5a)."""
    properties = tools[0]["function"]["parameters"]["properties"]
    value = {key: item for key, item in value.items() if key in properties}
    if "items" in value and "claims" not in properties["items"]["items"]["properties"]:
        value["items"] = [{key: item for key, item in row.items() if key != "claims"} for row in value["items"]]
    return value


def answered(note="可以在原平台订单的售后详情里看当前进度。", **choices):
    """A scripted answer: ``respond`` over whatever turn items the call's context lists."""
    def reply(messages, tools):
        context = json.loads(messages[1]["content"])
        return response(tools[0]["function"]["name"], offered(respond_value(context, note, **choices), tools))
    return reply


def response(name, value):
    return ModelResponse(tool_calls=[ToolCall("script-" + name, name, value)],
                         model_id="local-graph-test", usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})


class ScriptedModel:
    mode = "local_scripted_test"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete_with_timeout(self, messages, tools, *, timeout):
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(list(tools)), "timeout": timeout})
        if not self.outputs:
            raise AssertionError("unexpected extra model call")
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result(messages, tools) if callable(result) else result


class ScriptedFactory:
    mode = ScriptedModel.mode

    def __init__(self, model):
        self.model = model
        self.created = 0

    def __call__(self):
        self.created += 1
        return self.model


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class GraphHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.model = ScriptedModel([])
        self.factory = ScriptedFactory(self.model)
        self.server = create_server(db_path=self.directory / "graph.sqlite3", credentials={ALICE: "alice", BOB: "bob"},
                                    port=0, model_factory=self.factory,
                                    log_path=self.directory / "requests.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, *, token=ALICE):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer " + token}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, encoded, headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def start(self):
        status, result = self.request("POST", "/sessions", {})
        self.assertEqual(status, 201, result)
        return result["session_id"]

    def send(self, sid, message, request_id="m1", *, token=ALICE):
        return self.request("POST", f"/sessions/{sid}/messages", {"message": message, "request_id": request_id}, token=token)

    def confirm(self, sid, card, request_id="c1", *, token=ALICE, reason_code="changed_mind", reason_text=""):
        return self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm", {
            "version": card["version"], "reason_code": reason_code, "reason_text": reason_text,
            "request_id": request_id}, token=token)

    def prepare(self, sid):
        self.model.outputs.append(planned(business(order="O1001")))
        status, result = self.send(sid, "我要退货 O1001，不想要了")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["conversation_version"], "graph")
        return result

    def test_prepare_confirm_and_replay_create_one_business_row(self):
        sid = self.start()
        result = self.prepare(sid)
        self.assertEqual(result["confirmation"]["status"], "available")
        self.assertIsNone(result["session"]["return_request"])
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])
        action = result["session"]["card"]
        status, submitted = self.confirm(sid, action)
        self.assertEqual(status, 200, submitted)
        self.assertEqual(submitted["return_request"]["id"], "RET-O1001")
        self.assertEqual(self.confirm(sid, action)[1], submitted)
        self.assertEqual(self.confirm(sid, action, "c2")[1]["return_request"], submitted["return_request"])
        with self.server.store() as store:
            self.assertEqual(len(store.list_return_requests()), 1)
        self.assertEqual(len(self.model.calls), 1)

    def test_a_return_without_an_order_lists_candidates_and_the_next_order_gets_a_card(self):
        sid = self.start()
        self.model.outputs.extend([
            planned(business("request_return")),
            planned(business(quote="O1001", order="O1001"))])
        status, first = self.send(sid, "我要退货，不想要了", "first")
        self.assertEqual(status, 200, first)
        self.assertIsNone(first["session"]["card"])
        block = next(b for b in first["presentation"]["blocks"] if b["type"] == "business")
        self.assertEqual(sorted(row["order_id"] for row in block["candidate_orders"]),
                         ["O1001", "O1002", "O1003", "O1004"])
        self.assertFalse(any(row["type"] == "reason_ignored" for row in first["trace"]))   # B7: no reason field at all
        status, second = self.send(sid, "O1001", "second")
        self.assertEqual(status, 200, second)
        self.assertEqual(second["session"]["card"]["order_id"], "O1001")
        self.assertNotIn("reason", second["session"]["card"])
        context = json.loads(self.model.calls[1]["messages"][1]["content"])
        self.assertEqual(context["session_cards"], {"open_card_order_ids": [], "submitted_order_ids": []})
        self.assertIn("我要退货，不想要了", [m["content"] for m in context["history"]])

    def test_cross_user_session_read_message_and_confirm_are_rejected_before_model(self):
        sid = self.start()
        prepared = self.prepare(sid)
        action = prepared["session"]["card"]
        self.assertEqual(self.request("GET", f"/sessions/{sid}", token=BOB)[0], 404)
        self.assertEqual(self.send(sid, "看看这个订单", token=BOB)[0], 404)
        self.assertEqual(self.confirm(sid, action, token=BOB)[0], 404)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.factory.created, 1)
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])

    def test_the_answer_context_holds_items_verified_facts_and_catalogues_only(self):
        # B5d2 (plan §5.2): the answer reads code-normalised turn items, verified facts, the card catalogue (B6: whole,
        # with no retrieval status) and the other catalogues; the planner's proposals (its goal, stages) are not facts
        # and stay out.
        sid = self.start()
        self.model.outputs.extend([planned(goal='订单是否已发货'), answered()])
        status, result = self.send(sid, '我的售后到哪一步了')
        self.assertEqual((status, result['status']), (200, 'completed'))
        planner = json.loads(self.model.calls[0]['messages'][1]['content'])
        self.assertEqual(planner['session_cards'], {'open_card_order_ids': [], 'submitted_order_ids': []})
        context = json.loads(self.model.calls[1]['messages'][1]['content'])
        self.assertEqual(set(context), {'turn_items', 'verified', 'policy_cards', 'paths', 'option_sets',
                                        'conversation'})
        self.assertEqual(context['turn_items'], [{'index': 1, 'kind': 'message', 'subkind': 'none',
                                                  'quote': '我的售后到哪一步了', 'orders': [], 'fee_subject': 'none'}])
        self.assertEqual(context['verified']['actions'], [])
        self.assertEqual([row['order_id'] for row in context['verified']['customer_orders']],
                         ['O1003', 'O1001', 'O1004', 'O1002'])
        self.assertEqual(context['conversation']['current_user_message'], '我的售后到哪一步了')
        self.assertNotIn('订单是否已发货', self.model.calls[1]['messages'][1]['content'])
        self.assertEqual(self.model.calls[1]['tools'][0]['function']['name'], 'respond')


    def test_order_stage_and_fee_fields_are_bounded_and_the_fee_subject_reaches_the_items(self):
        from support_agent import understand

        schema = understand.schema(['O1001'])['properties']
        self.assertEqual(set(schema['order_stage']['enum']), {
            'unknown', 'placed_unshipped', 'shipped_unreceived', 'received', 'after_sales_active'})
        self.assertEqual(set(schema['fee_flow']['enum']), {
            'unknown', 'purchase_delivery', 'buyer_pickup', 'return_self_ship', 'return_home_pickup'})
        self.assertEqual(set(schema['items']['items']['properties']['fee_subject']['enum']), {
            'none', 'unknown', 'return_shipping', 'repair_shipping', 'purchase_shipping'})

        sid = self.start()
        question = item('policy_question', '配送费是多少？', fee_subject='purchase_shipping')
        invalid = planned([question])
        invalid.tool_calls[0].arguments.update(order_stage='maybe', fee_flow='shipping')
        corrected = planned([question])
        corrected.tool_calls[0].arguments.update(order_stage='shipped_unreceived', fee_flow='purchase_delivery')
        self.model.outputs.extend([invalid, corrected, answered('配送费明细可以在订单页查看。')])
        status, result = self.send(sid, '已发货还没收到，配送费是多少？')
        self.assertEqual((status, result['status']), (200, 'completed'))
        correction = next(row for row in result['trace'] if row['type'] == 'structure_correction')
        self.assertEqual(correction['node'], 'understand')
        self.assertIn('order_stage', correction['detail'])
        context = json.loads(self.model.calls[-1]['messages'][1]['content'])
        self.assertEqual([item['fee_subject'] for item in context['turn_items']], ['purchase_shipping'])
        self.assertEqual(result['graph']['plan']['order_stage'], 'shipped_unreceived')
        self.assertEqual(result['graph']['plan']['fee_flow'], 'purchase_delivery')


    def test_a_note_that_passes_the_guards_is_shown_as_the_models_words(self):
        sid = self.start()
        text = '您想改到周末收货，可以在物流详情里找承运快递的联系入口问问能不能改约。'
        self.model.outputs.extend([planned(), answered(text, disposition='routed', path='courier_contact')])
        result = self.send(sid, '周末才有人收，能改周末送吗？')[1]
        self.assertEqual(result['status'], 'completed')
        from support_agent import copy as code_copy
        self.assertEqual(result['reply'], text + '\n\n办理路径：' + code_copy.PATHS['courier_contact'])
        blocks = result['presentation']['blocks']
        self.assertEqual([(block['type'], block['author']) for block in blocks], [('text', 'model'), ('path', 'code')])
        self.assertEqual(result['presentation']['source_cards'], [])
        schema = self.model.calls[1]['tools'][0]['function']['parameters']
        # B5a/B5d2: with no candidate card this turn the claims are not offered; the own orders can always be shown.
        self.assertEqual(set(schema['properties']), {'items', 'show_order_ids', 'next_step', 'ask'})
        self.assertNotIn('claims', schema['properties']['items']['items']['properties'])
        self.assertEqual(result['answer_validation']['protocol'], 'respond-items-v1')
        self.assertTrue(result['answer_validation']['prose_shown'])
        self.assertIn('not_semantic_accuracy', result['answer_validation']['scope'])
        self.assertEqual([entry['hits'] for entry in result['trace'] if entry['type'] == 'guards'], [[]])


    def test_a_structure_error_gets_one_same_name_retry_with_only_its_codes(self):
        from support_agent import copy as code_copy
        sid = self.start()
        text = '可以在原平台的售后详情里看当前进度。'
        self.model.outputs.extend([planned(), answered(text, path='nowhere'), answered(text)])
        result = self.send(sid, '售后进度怎么查')[1]
        self.assertEqual(result['status'], 'completed')
        # B5g: a gap names no path here, so the code gives the item's verification entry (a whole message: general).
        self.assertEqual(result['reply'], text + '\n\n' + code_copy.PATH_LABEL + code_copy.PATHS['platform_human'])
        corrections = [row for row in result['trace'] if row['type'] == 'structure_correction']
        self.assertEqual([row['node'] for row in corrections], ['answer'])
        self.assertEqual(corrections[0]['detail'], 'G9_PATH_UNKNOWN')
        feedback = json.loads(self.model.calls[-1]['messages'][-1]['content'])
        self.assertEqual(feedback, {'error': 'G9_PATH_UNKNOWN', 'retry': '请修正结构后重新提交'})
        self.assertEqual([call['tools'][0]['function']['name'] for call in self.model.calls[1:]],
                         ['respond', 'respond'])


    def test_a_consequence_in_the_words_never_shows_and_the_card_is_the_answer(self):
        # B5f: an answered item carries no note (dropped as untidy: the card is not restated); a consequence in the next
        # step is a guard hit that drops that sentence only; no rewrite call, no fallback, the card choice stands.
        from support_agent import copy as code_copy
        sid = self.start()
        card = {'id': 'synthetic-shipping', 'title': '合成运费规则',
                'scope': '仅适用于测试商家', 'conditions': ['先核实服务范围和会员身份'],
                'outcomes': ['符合该范围且已核实身份时，按测试权益处理运费。'],
                'exceptions': ['其他情形另行核实'], 'evidence_ids': ['synthetic-source']}
        claim = [{'card_id': 'synthetic-shipping', 'outcome_index': 0}]
        self.model.outputs.extend([planned(queries=['退回运费']),
                                   answered('这单运费由平台承担。', claims=claim, next_step='运费由平台承担，放心寄回。')])
        with patch('support_agent.policy.PolicyAdapter.catalogue_cards', return_value=[card]), \
             patch('support_agent.policy.PolicyAdapter.source_cards', return_value=[{'id': 'synthetic-source'}]):
            result = self.send(sid, '退回运费谁承担？')[1]
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('运费由平台承担', result['reply'])
        self.assertTrue(result['reply'].startswith('符合该范围且已核实身份时，按测试权益处理运费。'))
        self.assertIn('仅适用于测试商家', result['reply'])
        self.assertNotIn(code_copy.FALLBACK, result['reply'])
        self.assertEqual(result['presentation']['source_cards'], [{'id': 'synthetic-source'}])
        types = [row['type'] for row in result['trace']]
        self.assertEqual((types.count('structure_correction'), types.count('revise_prose'), types.count('fallback_used')),
                         (0, 0, 0))
        self.assertEqual(next(row['dropped'] for row in result['trace'] if row['type'] == 'structure_tidied'),
                         [{'code': 'G9_NOTE_NOT_ALLOWED', 'part': 'note:1'}])
        self.assertEqual({(hit['code'], hit['part']) for hit in
                          next(row['hits'] for row in result['trace'] if row['type'] == 'guards')},
                         {('G3_COST', 'next_step')})
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(result['graph']['selection']['items'][0]['claims'], claim)


    def test_a_consequence_without_a_card_leaves_the_codes_line_for_the_gap(self):
        from support_agent import copy as code_copy
        sid = self.start()
        plan = planned([item('policy_question', '退回运费谁承担？', fee_subject='return_shipping')])
        self.model.outputs.extend([plan, answered('PLUS 运费由平台承担。')])
        status, result = self.send(sid, '退回运费谁承担？')
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('PLUS 运费由平台承担', result['reply'])
        # B5g: a policy question's gap gets its verification entry, the product page's notes.
        self.assertEqual(result['reply'], code_copy.ITEM_LINES['gap'] + '\n\n' + code_copy.PATH_LABEL
                         + code_copy.PATHS['policy_page'])
        self.assertEqual([(block['type'], block['author']) for block in result['presentation']['blocks']],
                         [('text', 'code'), ('path', 'code')])
        self.assertFalse(result['answer_validation']['fallback'])
        self.assertFalse(result['answer_validation']['prose_shown'])
        self.assertEqual({hit['part'] for row in result['trace'] if row['type'] == 'guards' for hit in row['hits']},
                         {'note:1'})
        self.assertEqual(len(self.model.calls), 2)


    def test_a_fee_card_of_another_service_is_tidied_and_a_mixed_question_keeps_both(self):
        # B6: the answer may cite any card of the catalogue (the enum is the catalogue's, fixed per version); the one
        # place that keeps a fee rule to the service asked about is G9, which drops a claim of another fee service
        # as untidy, without a second call (B5f). Two fee subjects in one turn leave the item's subject unknown.
        cards = [
            {'id': 'return_fee', 'title': '退货运费', 'scope': '退货商品',
             'effect_domain': 'return_shipping_responsibility', 'conditions': ['核对退货原因'],
             'outcomes': ['退货运费按退货规则核对。'], 'exceptions': [],
             'evidence_ids': ['return-source']},
            {'id': 'repair_fee', 'title': '维修运费', 'scope': '寄修商品',
             'effect_domain': 'repair_shipping_responsibility', 'conditions': ['核对维修原因'],
             'outcomes': ['维修运费按维修规则核对。'], 'exceptions': [],
             'evidence_ids': ['repair-source']},
        ]
        repair_plan = planned([item('policy_question', '过保寄修邮费是否按七天退货规则算？',
                                    fee_subject='repair_shipping')])
        self.model.outputs.extend([
            repair_plan,
            answered('', claims=[{'card_id': 'return_fee', 'outcome_index': 0},
                                 {'card_id': 'repair_fee', 'outcome_index': 0}]),
        ])
        with patch('support_agent.policy.PolicyAdapter.catalogue_cards', return_value=cards), \
             patch('support_agent.policy.PolicyAdapter.source_cards', return_value=[]):
            result = self.send(self.start(), '过保寄修邮费是否按七天退货规则算？')[1]
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.model.calls), 2)
        schema = self.model.calls[1]['tools'][0]['function']['parameters']
        self.assertEqual(schema['properties']['items']['items']['properties']['claims']['items']['properties']
                         ['card_id']['enum'], ['return_fee', 'repair_fee'])
        self.assertEqual(result['graph']['selection']['items'][0]['claims'],
                         [{'card_id': 'repair_fee', 'outcome_index': 0}])
        self.assertNotIn('退货运费按退货规则核对', result['reply'])
        self.assertIn('维修运费按维修规则核对', result['reply'])
        self.assertFalse(any(row['type'] == 'structure_correction' for row in result['trace']))
        self.assertIn({'code': 'G9_FEE_SUBJECT_MISMATCH', 'part': 'claim:1'},
                      next(row['dropped'] for row in result['trace'] if row['type'] == 'structure_tidied'))
        self.assertFalse(any(row['type'] in ('fee_claim_domain_filter', 'policy_bundle') for row in result['trace']))

        mixed_plan = planned([item('policy_question', '维修和退货运费分别如何承担？', fee_subject='unknown')])
        self.model.outputs.extend([
            mixed_plan,
            answered('', claims=[{'card_id': 'repair_fee', 'outcome_index': 0},
                                 {'card_id': 'return_fee', 'outcome_index': 0}]),
        ])
        with patch('support_agent.policy.PolicyAdapter.catalogue_cards', return_value=cards), \
             patch('support_agent.policy.PolicyAdapter.source_cards', return_value=[]):
            mixed = self.send(self.start(), '维修和退货运费分别如何承担？', 'mixed-fee')[1]
        self.assertEqual(mixed['status'], 'completed')
        # A question about two fee services: the planner marks its subject unknown, so G9 lets either fee card stand.
        self.assertEqual(json.loads(self.model.calls[-1]['messages'][1]['content'])['turn_items'][0]['fee_subject'],
                         'unknown')
        self.assertEqual([block['card_id'] for block in mixed['presentation']['blocks'] if block['type'] == 'policy_card'],
                         ['repair_fee', 'return_fee'])
        self.assertIn('维修运费按维修规则核对', mixed['reply'])
        self.assertIn('退货运费按退货规则核对', mixed['reply'])


    def test_repeated_consequences_in_a_note_never_reach_the_reply(self):
        from support_agent import copy as code_copy
        scenarios = [
            ('个人原因能换货吗？', '七天无理由不支持换货。', True),
            ('赠品没有寄回会扣款吗？', '赠品未退回会按赠品价值扣除。', True),
            ('已有售后单要改取件方式。', '需要撤销原售后申请才能重新申请。', False),
        ]
        for index, (question, unsafe, asks_the_rules) in enumerate(scenarios):
            with self.subTest(question=question):
                sid = self.start()
                plan = planned([item('policy_question', question)] if asks_the_rules else [])
                self.model.outputs.extend([plan, answered(unsafe)])
                status, result = self.send(sid, question, 'unsafe-' + str(index))
                self.assertEqual(status, 200)
                self.assertEqual(result['status'], 'completed')
                self.assertNotIn(unsafe, result['reply'])
                self.assertIn(code_copy.ITEM_LINES['gap'], result['reply'])
                self.assertFalse(result['answer_validation']['fallback'])
                first = next(row for row in result['trace'] if row['type'] == 'guards')
                self.assertTrue(any(hit['guard'] == 'G3' for hit in first['hits']))


    def test_a_note_of_the_wrong_type_is_a_structure_error(self):
        sid = self.start()
        self.model.outputs.extend([planned(), answered(123), answered('物流页面现在停在哪一步，可以先看一下。')])
        result = self.send(sid, '物流没更新')[1]
        self.assertEqual(result['status'], 'completed')
        detail = next(row['detail'] for row in result['trace'] if row['type'] == 'structure_correction')
        self.assertEqual(detail, 'G9_SHAPE')


    def test_answer_schema_enumerates_only_this_turn_items_cards_and_orders(self):
        from support_agent import copy as code_copy
        from support_agent.models import strict_schema
        from support_agent.respond import respond_schema
        cards = [{'id': 'available_rule', 'scope': '测试范围', 'conditions': ['测试条件'], 'outcomes': ['测试结果']},
                 {'id': 'another_rule', 'scope': '其他范围', 'conditions': ['其他条件'], 'outcomes': ['其他结果']}]
        items = [{'index': 1}, {'index': 2}]
        schema = respond_schema(items, cards, ['O1001'])
        item = schema['properties']['items']['items']['properties']
        self.assertEqual(item['index']['enum'], [1, 2])
        self.assertEqual(item['claims']['items']['properties']['card_id']['enum'], ['available_rule', 'another_rule'])
        self.assertEqual(item['path_id']['enum'], ['none', *code_copy.PATHS])
        self.assertEqual(schema['properties']['show_order_ids']['items']['enum'], ['O1001'])
        self.assertEqual(schema['properties']['ask']['properties']['option_set_id']['enum'],
                         ['none', *code_copy.OPTION_SETS])
        sent = strict_schema(schema)
        self.assertEqual(sent['required'], ['items', 'show_order_ids', 'next_step', 'ask'])
        self.assertNotIn('maxItems', json.dumps(sent))
        self.assertNotIn('claims', respond_schema(items, [], [])['properties']['items']['items']['properties'])
        self.assertNotIn('show_order_ids', respond_schema(items, [], [])['properties'])


    def test_a_read_whose_words_name_no_own_order_is_dropped_and_the_answer_takes_the_message(self):
        # B7 (K20): a read with no order in its words ("current" without an open card) is dropped on its own, silently
        # (nothing was to be changed); the answer takes the whole message, as it did for the old capability boundary.
        from support_agent import copy as code_copy
        for order_id in ('none', 'current'):
            with self.subTest(order_id=order_id):
                sid = self.start()
                lookup = [item('own_order_status', '这个订单', order='这个订单', order_id=order_id)]
                text = '可以通过物流详情联系承运方核实配送安排。'
                self.model.outputs.extend([planned(lookup), answered(text)])
                result = self.send(sid, '这个订单现在到哪里了？', 'lookup-' + order_id)[1]
                self.assertEqual(result['status'], 'completed')
                # B5g: the gap names no path, so the code gives the general verification entry.
                self.assertEqual(result['reply'],
                                 text + '\n\n' + code_copy.PATH_LABEL + code_copy.PATHS['platform_human'])
                self.assertEqual([(row['kind'], row['code']) for row in result['trace'] if row['type'] == 'item_rejected'],
                                 [('own_order_status', 'NO_ORDER')])
                self.assertIsNone(result['session']['card'])
                actual = next(row['result'] for row in result['trace'] if row['type'] == 'business')
                self.assertEqual(actual['status'], 'no_action')
                context = json.loads(self.model.calls[-1]['messages'][1]['content'])
                self.assertEqual([row['kind'] for row in context['turn_items']], ['message'])

    def test_explicit_owned_order_lookup_remains_backend_rendered(self):
        sid = self.start()
        self.model.outputs.append(planned(business('lookup_order', order='O1001')))
        result = self.send(sid, '查一下 O1001')[1]
        self.assertEqual(result['status'], 'completed')
        self.assertIn('订单 O1001：', result['reply'])
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(result['session']['card'])
        block = next(b for b in result['presentation']['blocks'] if b['type'] == 'business')
        self.assertEqual(block['author'], 'code')
        self.assertTrue(block['orders'][0]['return_eligible'])   # the page offers "申请退货" (D3)
        self.assertNotIn('teaching_rule_eligible', block['orders'][0])


    def test_a_read_of_words_that_are_no_order_number_is_dropped_and_the_question_still_answered(self):
        # B7 (K20): the item is dropped without a second planning call; a read changes nothing, so no code sentence
        # says it was not done; the answer takes the question (the whole message, or the registered question).
        from support_agent import copy as code_copy
        for queries in (None, ['发货延迟如何核实']):
            with self.subTest(queries=queries):
                sid = self.start()
                lookup = [item('own_order_status', 'ORD-EXAMPLE123', order='ORD-EXAMPLE123')]
                text = '这笔订单的发货情况可以在原平台的物流详情里看。'
                self.model.outputs.extend([planned(lookup, queries=queries),
                                           answered(text, disposition='routed', path='shipping_progress')])
                result = self.send(sid, '我的单号 ORD-EXAMPLE123 迟迟没发货',
                                   'failed-lookup-policy-' + str(bool(queries)))[1]
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['reply'], '\n\n'.join([text, '办理路径：' + code_copy.PATHS['shipping_progress']]))
                self.assertEqual([row['code'] for row in result['trace'] if row['type'] == 'item_rejected'], ['NO_ORDER'])
                self.assertEqual([call['tools'][0]['function']['name'] for call in self.model.calls[-2:]],
                                 ['plan_turn', 'respond'])
                context = json.loads(self.model.calls[-1]['messages'][1]['content'])
                self.assertEqual(context['verified']['actions'], [])
                self.assertEqual([item['kind'] for item in context['turn_items']],
                                 ['policy_question'] if queries else ['message'])
                self.assertIsNone(result['session']['card'])
                with self.server.store() as store:
                    self.assertEqual(store.list_return_requests(), [])


    def test_a_failed_foreign_lookup_exposes_no_order_and_leaves_the_open_card(self):
        from support_agent import copy as code_copy
        sid = self.start()
        prepared = self.prepare(sid)
        old = prepared['session']['card']
        lookup = business('lookup_order', order='O2001')
        text = '真实的配送进度可以在原平台的物流详情里看。'
        self.model.outputs.extend([planned(lookup), answered(text)])
        result = self.send(sid, '请查一下 O2001 的配送', 'foreign-lookup')[1]
        self.assertEqual(result['status'], 'completed')
        # B3b: the open card is untouched and declared again under the reply; nothing holds it.
        self.assertEqual(result['reply'], code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids='O2001') + '\n\n' + text
                         + '\n\n' + code_copy.PATH_LABEL + code_copy.PATHS['platform_human']
                         + '\n\n待确认：订单 O1001 的退货确认卡仍然有效，尚未提交。')
        actual = next(row['result'] for row in result['trace'] if row['type'] == 'business')
        self.assertEqual(actual['error']['code'], 'ORDER_NOT_FOUND')
        self.assertNotIn('order', actual)
        self.assertNotIn('orders', actual)
        context = json.loads(self.model.calls[-1]['messages'][1]['content'])
        self.assertEqual(context['verified']['actions'], [{'kind': 'order_not_in_account', 'order_ids': []}])
        self.assertNotIn('O2001', json.dumps(context['verified'], ensure_ascii=False))
        self.assertEqual(result['session']['card'], old)
        self.assertEqual(result['confirmation']['status'], 'available')
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])
        status, confirmed = self.confirm(sid, old, 'after-denied-lookup')
        self.assertEqual(status, 200, confirmed)
        with self.server.store() as store:
            self.assertEqual([(row['customer_id'], row['order_id']) for row in store.list_return_requests()],
                             [('alice', 'O1001')])


    def test_current_lookup_of_existing_local_task_preserves_pending_confirmation(self):
        sid = self.start()
        ready = self.prepare(sid)
        self.model.outputs.append(planned([item('own_order_status', '这单', order='这单', order_id='current')]))
        result = self.send(sid, '再查一下这单', 'read-current')[1]
        self.assertEqual(result['status'], 'completed')
        self.assertIn('订单 O1001：', result['reply'])
        self.assertEqual(result['session']['card'], ready['session']['card'])
        self.assertEqual(result['confirmation']['status'], 'available')
        self.assertEqual(len(self.model.calls), 2)

    def test_mixed_consultation_preserves_actual_backend_result(self):
        from support_agent import copy as code_copy
        sid = self.start()
        self.model.outputs.extend([planned(business(order='O1001'), queries=['退货规则']), answered('')])
        result = self.send(sid, '我要退货 O1001，不想要了；退货规则是什么')[1]
        self.assertEqual(result['status'], 'completed')
        actual = next(t['result'] for t in result['trace'] if t['type'] == 'business')
        # The prepared card is stated once, from the backend's own card, without any reason.
        self.assertEqual(actual['card']['order_id'], 'O1001')
        self.assertIn(code_copy.CARD_READY.format(order_id='O1001', product='学习用保温杯'), result['reply'])
        self.assertNotIn('待确认：订单', result['reply'])
        self.assertNotIn('不想要了', json.dumps(actual, ensure_ascii=False))
        context = json.loads(self.model.calls[-1]['messages'][1]['content'])
        self.assertEqual([item['kind'] for item in context['turn_items']], ['return_request', 'policy_question'])
        self.assertEqual(context['verified']['actions'], [{'kind': 'card_shown', 'order_ids': ['O1001']}])
        self.assertNotIn('不想要了', json.dumps(context['verified'], ensure_ascii=False))
        self.assertEqual(result['confirmation']['status'], 'available')
        self.assertIsNone(result['session']['return_request'])


    def test_a_pure_business_turn_binds_the_catalogue_but_calls_only_the_planner(self):
        # B6: the policy step runs every turn (it no longer waits for the planner to ask for a search); it is code
        # only, so a pure card turn still makes one model call and nothing in it reads the catalogue's words.
        result = self.prepare(self.start())
        self.assertEqual(result['graph']['nodes'], ['understand', 'business', 'policy', 'answer'])
        self.assertEqual(len(self.model.calls), 1)
        selection = next(row for row in result['trace'] if row['type'] == 'answer_selection')
        self.assertEqual(selection['available_policy_card_ids'], [])

    def test_another_customers_order_cannot_produce_a_card(self):
        sid = self.start()
        self.model.outputs.append(planned(business(order="O2001")))
        status, result = self.send(sid, "我要退货 O2001，不想要了")
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["session"]["card"])
        facts = next(t["result"] for t in result["trace"] if t["type"] == "business")
        self.assertEqual(facts["error"]["code"], "ORDER_NOT_FOUND")
        self.assertNotIn("order", facts)

    def test_a_reason_said_in_chat_keeps_the_card_and_only_the_click_sets_the_reason(self):
        sid = self.start()
        old = self.prepare(sid)["session"]["card"]
        self.model.outputs.append(planned(business(quote="原因改成买重复了")))
        status, result = self.send(sid, "原因改成买重复了", "change")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["session"]["card"], old)
        # B7: the planning function has no field a reason could go into (plan I1, I2).
        schema = self.model.calls[1]["tools"][0]["function"]["parameters"]
        self.assertEqual(set(schema["properties"]), {"goal", "items", "order_stage", "fee_flow"})
        self.assertEqual(set(schema["properties"]["items"]["items"]["properties"]),
                         {"kind", "subkind", "quote", "history_index", "order_quote", "order_id", "fee_subject"})
        self.assertEqual(len(self.model.calls), 2)   # a pure card turn skips the answer model
        self.assertEqual(self.confirm(sid, old, "click", reason_code="size_or_style")[0], 200)
        with self.server.store() as store:
            self.assertEqual([row["reason_code"] for row in store.list_return_requests()], ["size_or_style"])

    def test_consultation_does_not_change_or_revoke_existing_draft(self):
        sid = self.start()
        before = self.prepare(sid)["session"]
        self.model.outputs.extend([planned(), answered()])
        status, result = self.send(sid, "先不决定，我想了解一下流程", "consultation")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["session"]["card"], before["card"])
        self.assertNotIn("confirmation_hold", result["session"])

    def test_one_structure_correction_is_counted_and_persisted(self):
        sid = self.start()
        self.model.outputs.extend([response("plan_turn", {"goal": "售后进度"}), planned(), answered()])
        status, result = self.send(sid, "售后到哪一步了")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(result["execution"]["model_attempts"], 3)
        self.assertEqual(result["usage"], {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39})
        self.assertTrue(result["execution"]["usage_complete"])
        self.assertEqual(sum(t["type"] == "structure_correction" for t in result["trace"]), 1)
        self.assertIn("tool", [m["role"] for m in self.model.calls[1]["messages"]])
        status, audit = self.request("GET", f"/sessions/{sid}/runs/{result['run_id']}")
        self.assertEqual(status, 200, audit)
        self.assertEqual(audit["model_attempts"], 3)

    def test_binding_the_catalogue_is_neither_a_tool_call_nor_a_step_event(self):
        # S3-3c: policy retrieval used to reserve a made-up search_public_policy tool call; then it was a progress
        # step. B6: nothing is searched and nothing waits, so the turn records neither.
        sid = self.start()
        self.model.outputs.extend([planned(queries=["退货规则"]), answered()])
        status, result = self.send(sid, "退货规则是什么")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["execution"]["tool_calls"], 0)
        status, audit = self.request("GET", f"/sessions/{sid}/runs/{result['run_id']}")
        self.assertEqual(status, 200, audit)
        kinds = [event["kind"] for event in audit["events"]]
        self.assertNotIn("step_started", kinds)
        self.assertNotIn("tool_reserved", kinds)

    def test_two_invalid_structures_stop_and_release_the_lease(self):
        sid = self.start()
        self.model.outputs.extend([response("plan_turn", {}), response("plan_turn", {})])
        status, result = self.send(sid, "售后到哪一步了")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "model_error", result)
        self.assertFalse(result["session"]["busy"])
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(result["execution"]["model_attempts"], 2)
        self.assertIsNone(result["session"]["card"])

    def test_failed_planning_leaves_the_existing_card_unchanged_and_only_a_click_submits(self):
        # B3b (plan §4.4): the failed turn neither changes nor holds the card. The page folds it until the user
        # expands it (chat.js cardFolded); the server keeps one rule: only the click submits, with its reason.
        sid = self.start()
        old = self.prepare(sid)["session"]["card"]
        self.model.outputs.extend([response("plan_turn", {}), response("plan_turn", {})])
        status, result = self.send(sid, "我改主意了，先不要办", "withdrawal-unparsed")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "model_error", result)
        self.assertFalse(result["session"]["busy"])
        self.assertEqual(result["session"]["card"], old)
        self.assertNotIn("confirmation_hold", result["session"])
        self.assertEqual((result["confirmation"]["status"], result["confirmation"]["action_id"]),
                         ("available", old["action_id"]))
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])
        status, confirmed = self.confirm(sid, old, "click-after-failed-plan", reason_code="size_or_style")
        self.assertEqual(status, 200, confirmed)
        with self.server.store() as store:
            self.assertEqual([row["reason_code"] for row in store.list_return_requests()], ["size_or_style"])

    def test_provider_failure_releases_lease_and_a_new_request_can_run(self):
        sid = self.start()
        self.model.outputs.append(ModelError("模拟供应商错误", retryable=False))
        status, failed = self.send(sid, "杯子碎了")
        self.assertEqual(status, 200, failed)
        self.assertEqual(failed["status"], "model_error")
        self.assertFalse(failed["session"]["busy"])
        self.assertEqual(failed["execution"]["model_attempts"], 1)
        self.assertFalse(failed["execution"]["usage_complete"])
        self.model.outputs.extend([planned(), answered()])
        status, recovered = self.send(sid, "杯子碎了", "retry-new-request")
        self.assertEqual(status, 200, recovered)
        self.assertEqual(recovered["status"], "completed")
        self.assertFalse(recovered["session"]["busy"])

    def test_cached_message_does_not_initialize_or_call_model(self):
        sid = self.start()
        first = self.prepare(sid)
        def forbidden_factory():
            raise AssertionError("message replay must not construct a provider")
        self.server.model_factory = forbidden_factory
        status, replay = self.send(sid, "我要退货 O1001，不想要了")
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(replay["usage"], first["usage"])
        self.assertEqual(len(self.model.calls), 1)

    def test_pure_business_never_asks_model_for_fabricated_success_text(self):
        sid = self.start()
        malicious = response("respond", {"items": [], "show_order_ids": [], "prose": "已退款 8900 元，商家已经安排明天取件。",
                                         "ask": {"text": "", "option_set_id": "none"}})
        self.model.outputs.extend([planned(business(order="O1001")), malicious])
        status, result = self.send(sid, "我要退货 O1001，不想要了")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.outputs, [malicious])
        self.assertNotIn("已退款", result["reply"])
        self.assertNotIn("明天", result["reply"])
        self.assertIn("还没有提交", result["reply"])
        self.assertIsNone(result["session"]["return_request"])


    def test_policy_request_without_configuration_has_no_invented_sources(self):
        sid = self.start()
        self.model.outputs.extend([planned(queries=["赠品遗失退货怎么处理"]), answered()])
        status, result = self.send(sid, "赠品遗失退货怎么处理")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["policy_status"], "insufficient_evidence")
        self.assertEqual(result["presentation"]["source_cards"], [])
        self.assertIn("policy", result["graph"]["nodes"])
        self.assertIsNone(result["session"]["card"])
        context = json.loads(self.model.calls[1]["messages"][1]["content"])
        self.assertEqual(context["policy_cards"], [])   # no release, so no catalogue (fail closed)
        self.assertNotIn("policy_retrieval", context)


    def test_a_public_policy_plan_needs_no_query_to_get_the_catalogue(self):
        # B6: no query is defaulted any more; B7: the plan has no search fields at all. A rules question reaches the
        # answer with the catalogue as it is.
        sid = self.start()
        question = '这类商品可以申请什么售后，需要满足哪些条件？'
        self.model.outputs.extend([planned([item('policy_question', question)]),
                                   answered('我这边的资料没写到这类商品的说明，可以先看商品页面。')])
        result = self.send(sid, question)[1]
        self.assertEqual(result['status'], 'completed')
        self.assertIn('policy', result['graph']['nodes'])
        self.assertNotIn('policy_queries', result['graph']['plan'])
        context = json.loads(self.model.calls[-1]['messages'][1]['content'])
        self.assertNotIn('policy_retrieval', context)
        self.assertEqual(context['policy_cards'], [])
        self.assertEqual([item['kind'] for item in context['turn_items']], ['policy_question'])
        self.assertFalse(any(row['type'] == 'policy_query_defaulted' for row in result['trace']))
        self.assertEqual(result['policy_status'], 'insufficient_evidence')
        self.assertIsNone(result['session']['card'])


    def test_every_kind_of_item_gets_the_same_whole_catalogue(self):
        # B6: the answer reads the whole catalogue of the turn's version; B7: whatever kind the planner gives the
        # message (a question or chit-chat), the answer gets the same cards, every card citable.
        cards = [{'id': 'synthetic-a', 'title': '合成规则甲', 'scope': '只用于工程测试', 'conditions': ['条件甲'],
                  'outcomes': ['结果甲'], 'exceptions': []},
                 {'id': 'synthetic-b', 'title': '合成规则乙', 'scope': '只用于工程测试', 'conditions': ['条件乙'],
                  'outcomes': ['结果乙'], 'exceptions': []}]
        contexts, enums = [], []
        for index, kind in enumerate(['policy_question', 'chit_chat']):
            plan = planned([item(kind, '想联系配送人员问一下安排')])
            self.model.outputs.extend([plan, answered('')])
            with patch('support_agent.policy.PolicyAdapter.catalogue_cards', return_value=cards):
                result = self.send(self.start(), '想联系配送人员问一下安排', 'search-fields-' + str(index))[1]
            self.assertEqual(result['status'], 'completed')
            self.assertIn('policy', result['graph']['nodes'])
            self.assertFalse(any(row['type'] in ('policy_bundle', 'policy_query_defaulted') for row in result['trace']))
            contexts.append(json.loads(self.model.calls[-1]['messages'][1]['content']))
            schema = self.model.calls[-1]['tools'][0]['function']['parameters']
            enums.append(schema['properties']['items']['items']['properties']['claims']['items']['properties']
                         ['card_id']['enum'])
        self.assertEqual(contexts[0]['policy_cards'], contexts[1]['policy_cards'])
        self.assertEqual([card['id'] for card in contexts[0]['policy_cards']], ['synthetic-a', 'synthetic-b'])
        self.assertEqual([[row['kind'] for row in context['turn_items']] for context in contexts],
                         [['policy_question'], ['chit_chat']])
        self.assertEqual(enums, [['synthetic-a', 'synthetic-b']] * 2)


    def test_retryable_provider_attempt_is_counted_with_unknown_usage(self):
        sid = self.start()
        self.model.outputs.extend([ModelError("模拟限流", retryable=True, status_code=429, retry_after=0),
                                   planned(), answered()])
        status, result = self.send(sid, "售后进度怎么查")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["execution"]["model_attempts"], 3)
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(result["usage"]["total_tokens"], 26)
        self.assertFalse(result["execution"]["usage_complete"])


    def test_the_planning_schema_offers_items_of_intent_and_only_own_orders(self):
        # B7: one item list of intents; no authorisation kinds, drafts, readiness or resume fields; order_id is one of
        # the account's own ids or a word. The planning context shows an open card by its order id only (K3, K16).
        sid = self.start()
        self.model.outputs.extend([planned(), answered()])
        self.send(sid, "售后进度在哪里看")
        call = self.model.calls[0]
        self.assertEqual(call["tools"][0]["function"]["name"], "plan_turn")
        properties = call["tools"][0]["function"]["parameters"]["properties"]
        row = properties["items"]["items"]["properties"]
        self.assertEqual(row["kind"]["enum"], ["policy_question", "own_order_status", "return_request", "withdraw",
                                               "handoff_request", "complaint", "safety", "improper_request",
                                               "chit_chat"])
        self.assertEqual(row["order_id"]["enum"], ["O1003", "O1001", "O1004", "O1002", "all_mine", "current", "none"])
        self.assertFalse({"business", "authorization", "reason", "capability", "policy_queries", "policy_topics",
                          "policy_questions", "missing_information"} & set(properties))
        from support_agent import understand
        card = {"action_id": "ACT-x", "version": 1, "order_id": "O1001", "status": "pending",
                "void_reason": None, "expires_at": 1.0}
        context = understand.context("这单先等等", [], [{"order_id": "O1001", "product_name": "杯子"}],
                                     {"open_cards": [card], "return_request": None})
        self.assertEqual(context["session_cards"], {"open_card_order_ids": ["O1001"], "submitted_order_ids": []})
        self.assertNotIn("ACT-x", json.dumps(context))

    def test_a_read_then_a_return_without_naming_the_order_gets_its_card_by_k5(self):
        # B7 deleted save_draft; a read of one order followed by "帮我办理退货" finds that order by K5.
        sid = self.start()
        self.model.outputs.append(planned(business("lookup_order", order="O1001")))
        status, saved = self.send(sid, "看看 O1001，不想要了")
        self.assertEqual(status, 200, saved)
        self.assertIsNone(saved["session"]["card"])
        self.model.outputs.append(planned(business(quote="现在请帮我办理退货")))
        status, ready = self.send(sid, "现在请帮我办理退货", "explicit-request")
        self.assertEqual(status, 200, ready)
        self.assertEqual(ready["confirmation"]["status"], "available")
        self.assertEqual(ready["confirmation"]["order_id"], "O1001")
        self.assertIsNone(ready["session"]["return_request"])
        self.assertEqual(len(self.model.calls), 2)


if __name__ == "__main__":
    unittest.main()
