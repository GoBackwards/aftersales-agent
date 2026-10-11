"""Gaps the B3 verifiers reported, closed in B3e (one decision in one place, the page renders what is declared).

Behaviours: a chat withdrawal voids one card at most — a mention that fits several own orders, a typed
number that fits none of them or two named cards void nothing and leave each card to its own "暂不提交";
only a four-digit number is an order number, so a day count or a longer number is no order reference; a
reply that prepares no card does not state the rule about confirming one; a failed turn states the open-card
reminder before its one next step (B4b replaced the record line B3e placed there); the "申请退货" click declares the card
it shows exactly as a chat turn does, says whether it is new or still valid, and the page draws a card, its
facts and its reason options only from that declaration (plan I13); an order whose card is open offers no
second "申请退货". Offline: SQLite, the real business adapter and HTTP service, a scripted model stand-in
and static checks of the page. Engineering evidence, not task success.
"""
from itertools import count
import http.client
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest

from support_agent import decide
from support_agent.business import CHOOSE_CARD_RESULT, BusinessAdapter
from support_agent import copy as code_copy
from support_agent.assemble import business_parts
from support_agent.return_cards import reasons_payload, return_card_block
from support_agent.sessions import FAILED_TURN_TEXT, RETRY_STEP, SessionError, SessionStore

def rendered(result):
    """The business outcome as the reply's code sentences word it (B5d2: assemble.py from copy.py)."""
    lead, lines, note = business_parts(result)
    return "\n".join(part for part in (lead, *lines, note) if part)


APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
# A made-up customer with two returnable orders that one product word fits (the demo seed has no such pair);
# the third order keeps that word from being noise shared by every name.
DORA_ORDERS = (("D4001", "学习用折叠椅", "2026-09-16"), ("D4002", "学习用折叠桌", "2026-09-15"),
               ("D4003", "学习用台灯", "2026-09-14"))
REMINDER = "\n\n待确认：订单 {} 的退货确认卡仍然有效，尚未提交。"


def ref(order_id, quote, role="target"):
    return {"order_id": order_id, "role": role, "quote": quote}


def item(kind, quote, reference):
    return {"kind": kind, "subkind": "none", "quote": quote, "history_index": None,
            "order_quote": reference["quote"], "order_id": reference["order_id"], "fee_subject": "none"}


def withdraw(quote, *refs):
    """Withdraw items (B7): one per reference."""
    return [item("withdraw", quote, reference) for reference in refs]


def ask_return(quote, *refs):
    return [item("return_request", quote, reference) for reference in refs]


def lookup(*refs):
    """Reads: one own_order_status item per reference, quoting the order's words."""
    return [item("own_order_status", reference["quote"], reference) for reference in refs]


class WithdrawalDecisionTests(unittest.TestCase):
    def test_a_chat_withdrawal_voids_one_card_at_most(self):
        table = (
            (({"O1", "O2"}, ["O1", "O2"], set()), {}, ("choose", ["O1", "O2"])),     # two named cards
            (({"O1", "O3"}, ["O1"], set()), {}, ("choose", ["O1"])),               # one named card, one other order
            (({"O1"}, ["O1", "O2"], set()), {}, ("void", ["O1"])),
            ((set(), ["O2"], set()), {}, ("void", ["O2"])),
            ((set(), ["O1", "O2"], set()), {"unclear": True}, ("choose", ["O1", "O2"])),
            ((set(), ["O1"], set()), {"unclear": True}, ("choose", ["O1"])),       # never the only card by default
            (({"O1"}, ["O1"], set()), {"unclear": True}, ("choose", ["O1"])),       # a named card beside an unclear one
            ((set(), [], {"O3"}), {"unclear": True}, ("no_open_card", [])),
        )
        for arguments, options, expected in table:
            with self.subTest(arguments=arguments, options=options):
                self.assertEqual(decide.withdraw_outcome(*arguments, **options), expected)


class WithdrawalTurnTests(unittest.TestCase):
    """The business boundary of a chat turn (no model) on the real store."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = SessionStore(Path(self.folder.name) / "gaps.sqlite3", clock=lambda: 1_000)
        self.addCleanup(self.store.close)
        with self.store._connection:
            self.store._connection.executemany(
                "INSERT INTO orders (order_id, customer_id, product_name, category, condition, delivered_at, "
                "paid_amount_cents, status) VALUES (?, 'dora', ?, 'general', 'sealed', ?, 4500, 'delivered')",
                DORA_ORDERS)
        self.ids = count()
        self.sessions = {}

    def sid(self, customer):
        return self.sessions.setdefault(customer, self.store.create_session(customer)["session_id"])

    def apply(self, message, proposed, customer="alice"):
        session_id = self.sid(customer)
        started = self.store.begin_turn(customer, session_id, f"g-{next(self.ids)}", message, mode="offline_graph")
        results = BusinessAdapter(self.store, customer, session_id, started["run_id"]).apply(
            [{"index": index, **item} for index, item in enumerate(proposed, 1)])
        self.store.finish_turn(customer, session_id, started["run_id"], [{"role": "user", "content": message}],
                               {"reply": results[-1].get("reply", ""), "business": results})
        return results[0] if len(results) == 1 else results

    def button(self, order_id, customer="alice"):
        return self.store.request_card(customer, self.sid(customer), order_id, f"b-{next(self.ids)}")

    def open_orders(self, customer="alice"):
        return [card["order_id"] for card in self.store.get_session(customer, self.sid(customer))["open_cards"]]

    def test_a_withdrawal_whose_product_word_fits_two_open_cards_voids_neither(self):
        self.button("D4001", "dora")
        self.button("D4002", "dora")
        result = self.apply("折叠的那张不要了", withdraw("不要了", ref("D4001", "折叠的那张")), customer="dora")
        self.assertEqual([ref["outcome"] for ref in result["binding"]["product_refs"]], ["ambiguous"])
        self.assertEqual((result["status"], result["reply"]), ("card_choice_needed", CHOOSE_CARD_RESULT))
        self.assertEqual(result["open_card_order_ids"], ["D4001", "D4002"])
        self.assertEqual(self.open_orders("dora"), ["D4001", "D4002"])
        self.assertNotIn("voided_order_id", result)

    def test_a_withdrawal_whose_product_word_fits_two_orders_keeps_even_a_single_open_card(self):
        self.button("D4002", "dora")
        result = self.apply("折叠的那张不要了", withdraw("不要了", ref("D4002", "折叠的那张")), customer="dora")
        self.assertEqual(result["binding"]["product_refs"][0]["matched_order_ids"], ["D4001", "D4002"])
        self.assertEqual((result["status"], result["open_card_order_ids"]), ("card_choice_needed", ["D4002"]))
        self.assertEqual(self.open_orders("dora"), ["D4002"])

    def test_a_withdrawal_with_a_number_that_fits_no_own_order_lists_the_only_card_instead_of_voiding_it(self):
        self.button("O1001")
        result = self.apply("9999 那张不要了", withdraw("不要了", ref("none", "9999")))
        self.assertEqual((result["status"], result["open_card_order_ids"]), ("card_choice_needed", ["O1001"]))
        self.assertEqual(self.open_orders(), ["O1001"])
        self.assertEqual(rendered(result).splitlines(), [code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="“9999”"),
                                                         code_copy.CARD_CHOOSE])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_the_number_of_the_open_cards_order_still_voids_that_card_alone(self):
        self.button("O1001")
        self.button("O1004")
        result = self.apply("1004 那张作罢", withdraw("作罢", ref("O1004", "1004")))
        self.assertEqual((result["status"], result["voided_order_id"]), ("card_voided", "O1004"))
        self.assertEqual(self.open_orders(), ["O1001"])

    def test_a_withdrawal_or_a_card_choice_named_by_its_words_shows_no_empty_brackets(self):
        # B7r: these outcomes carry no order view, so the sentence naming the order the words meant leaves out the
        # product brackets instead of showing them empty.
        self.button("O1001")
        self.button("O1004")
        voided = self.apply("收纳盒那张作罢", withdraw("作罢", ref("O1004", "收纳盒那张")))
        choice = self.apply("饼干那张作罢", withdraw("作罢", ref("O1003", "饼干那张")))
        self.assertEqual((voided["status"], choice["status"]), ("card_voided", "card_choice_needed"))
        for result, order_id in ((voided, "O1004"), (choice, "O1003")):
            with self.subTest(status=result["status"]):
                text = rendered(result)
                self.assertNotIn("（）", text)
                self.assertTrue(text.splitlines()[0].endswith(f"是订单 {order_id}。"), text)
        self.assertEqual(self.open_orders(), ["O1001"])

    def test_only_a_four_digit_number_is_an_order_number(self):
        # B7: a read whose words name no order is dropped (BusinessAdapter.read_problem, understand.check).
        catalog = BusinessAdapter.order_catalog(self.store, "alice")
        sourced = (("1003这单查一下", ref("O1003", "1003", "query")), ("1003这单查一下", ref("none", "1003", "query")))
        for message, proposal in sourced:
            with self.subTest(message=message, proposal=proposal):
                self.assertIsNone(BusinessAdapter.read_problem(lookup(proposal)[0], message, catalog, []))
        unsourced = (("签收 7 天了，查一下", ref("none", "7", "query"), "NO_ORDER"),
                     ("签收 7 天了，查一下", ref("O1003", "7", "query"), "ORDER_NOT_SOURCED"),
                     ("单号 10031 查一下", ref("none", "10031", "query"), "NO_ORDER"),
                     ("尾号 103 那单查一下", ref("none", "103", "query"), "NO_ORDER"))
        for message, proposal, code in unsourced:
            with self.subTest(message=message, proposal=proposal):
                self.assertEqual(BusinessAdapter.read_problem(lookup(proposal)[0], message, catalog, []), code)

    def test_a_short_number_in_a_turn_names_no_order_and_lists_nothing(self):
        # As a return target it is no reference at all: K5 or the candidates decide, never "order 7".
        returned = self.apply("签收 7 天了，退掉", ask_return("退掉", ref("none", "7")))
        self.assertEqual(returned["status"], "clarification_needed")
        self.assertNotIn("number_refs", returned["binding"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_reply_that_prepares_no_card_states_no_confirmation_rule(self):
        ineligible = self.apply("O1002 退掉", ask_return("退掉", ref("O1002", "O1002")))
        self.assertEqual(ineligible["status"], "ineligible")
        several = self.apply("O1002、O1003 两单一并退掉", ask_return("一并退掉", ref("O1002", "O1002"), ref("O1003", "O1003")))
        self.assertEqual(several["status"], "ineligible")
        for result in (ineligible, several):
            with self.subTest(status=result["status"], orders=[row.get("order_id") for row in result.get("orders") or []]):
                self.assertNotIn(code_copy.SUBMIT_BY_CLICK, rendered(result))
        self.assertIn(code_copy.NOT_RETURNABLE["outside_window"].format(last_date="2026-09-08"), rendered(ineligible))
        carded = self.apply("O1001 退掉", ask_return("退掉", ref("O1001", "O1001")))
        self.assertIn(code_copy.CARD_READY.format(order_id="O1001", product="学习用保温杯"), rendered(carded))
        self.assertEqual(self.store.list_return_requests(), [])

    def test_the_button_declares_its_card_with_the_same_block_a_turn_uses(self):
        first = self.button("O1001")
        self.assertEqual(first["card_outcome"], "created")
        order = self.store.owned_order("alice", "O1001")
        self.assertEqual(first["return_card"], return_card_block(first["card"], order))
        self.assertEqual(first["return_card"]["reasons"], reasons_payload())
        again = self.button("O1001")
        self.assertEqual((again["card_outcome"], again["return_card"]["action_id"]),
                         ("reshown", first["card"]["action_id"]))
        self.assertFalse({"reason", "reason_code", "reason_text"} & set(first["return_card"]))


class PageDeclarationTests(unittest.TestCase):
    """Static checks of the page (T5 walks it in a browser)."""

    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def function(self, name):
        start = self.chat.index(f"function {name}(")
        return self.chat[start:self.chat.index("\n  }\n", start)]

    def test_a_card_its_facts_and_its_reason_options_come_from_its_declaration(self):
        card = self.function("confirmCard")
        self.assertIn("const declared = declaredCard(card);", card)
        self.assertIn("declared?.reasons", card)
        self.assertIn("const order = declared?.order;", card)
        for gone in ("return_reasons", "orderInfo(", "ordersMeta?.return_reasons"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, card)
        self.assertNotIn("return_reasons", self.chat)
        declared = self.function("declaredCard")
        self.assertIn('item?.type === "return_card" && item.action_id === card.action_id', declared)
        self.assertIn("response.return_card", declared)

    def test_an_order_with_an_open_card_offers_no_second_apply_button(self):
        order = self.function("orderCard")
        self.assertIn("!openCards().some(card => card.order_id === full.order_id)", order)
        self.assertEqual(order.count("canApply"), 3)          # defined once, used by both buttons
        self.assertEqual(order.count('text: "申请退货"'), 2)

    def test_a_re_shown_card_is_announced_as_still_valid_not_as_generated(self):
        note = self.function("cardNote")
        self.assertIn('response?.card_outcome === "reshown"', note)
        self.assertIn("仍然有效", note)
        self.assertEqual(self.chat.count("确认卡已生成"), 1)  # only cardNote words a new card
        self.assertIn("toast(cardNote(result)", self.chat)
        self.assertIn("cardNote(entry.response)", self.chat)


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class DeclarationHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.model = ScriptedModel([])
        self.server = create_server(db_path=directory / "gaps.sqlite3", credentials={"gaps-alice": "alice"},
                                    port=0, model_factory=ScriptedFactory(self.model), log_path=directory / "log.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.sid = self.request("POST", "/sessions", {})[1]["session_id"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": "Bearer gaps-alice"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def fail_turn(self, request_id):
        from support_agent.models import ModelError
        self.model.outputs.append(ModelError("脚本里的供应商故障"))
        status, result = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": "顺便问点别的", "request_id": request_id})
        self.assertEqual((status, result["status"]), (200, "model_error"))
        return result

    def test_a_failed_turn_states_the_open_card_before_its_one_next_step(self):
        # B4b: the cause, the open card's state (still valid, not submitted), then the one next step.
        self.request("POST", f"/sessions/{self.sid}/cards", {"order_id": "O1001", "request_id": "cup"})
        failed = self.fail_turn("fails")
        expected = FAILED_TURN_TEXT + REMINDER.format("O1001") + "\n" + RETRY_STEP
        self.assertEqual(failed["reply"], expected)
        self.assertEqual(failed["presentation"]["short_answer"], expected)
        entries = self.request("GET", f"/sessions/{self.sid}/history")[1]["entries"]
        self.assertEqual(entries[-1]["response"]["reply"], expected)

    def test_a_failed_turn_without_a_card_ends_with_its_one_next_step_and_keeps_the_error_out_of_the_reply(self):
        failed = self.fail_turn("fails-alone")
        self.assertEqual(failed["reply"], FAILED_TURN_TEXT + "\n" + RETRY_STEP)
        self.assertNotIn("脚本里的供应商故障", failed["reply"])
        self.assertEqual(failed["error"]["message"], "脚本里的供应商故障")

    def test_the_history_keeps_each_button_clicks_declaration_and_whether_it_was_new(self):
        status, first = self.request("POST", f"/sessions/{self.sid}/cards", {"order_id": "O1001", "request_id": "b1"})
        self.assertEqual((status, first["card_outcome"]), (200, "created"))
        status, again = self.request("POST", f"/sessions/{self.sid}/cards", {"order_id": "O1001", "request_id": "b2"})
        self.assertEqual((status, again["card_outcome"]), (200, "reshown"))
        entries = self.request("GET", f"/sessions/{self.sid}/history")[1]["entries"]
        self.assertEqual([(entry["entry"], entry["response"]["card_outcome"]) for entry in entries],
                         [("card_prepare", "created"), ("card_prepare", "reshown")])
        self.assertEqual(entries[0]["response"]["return_card"], first["return_card"])
        # A later turn declares the same card with the same block the click declared.
        from tests.test_graph_agent import planned
        self.model.outputs.append(planned(lookup(ref("O1004", "O1004", "query"))))
        status, turn = self.request("POST", f"/sessions/{self.sid}/messages",
                                    {"message": "O1004 现在怎样", "request_id": "look"})
        block = next(item for item in turn["presentation"]["blocks"] if item["type"] == "return_card")
        self.assertEqual(block, first["return_card"])

    def test_the_orders_listing_carries_no_card_reason_catalogue(self):
        status, listing = self.request("GET", "/me/orders")
        self.assertEqual(status, 200)
        self.assertNotIn("return_reasons", listing)
        self.assertIn("return_shipping_rule", listing)
        for order in listing["orders"]:
            self.assertNotIn("condition_label", order)
        # B5d2: the last return day is an order fact the reply's verdict states (copy.RETURNABLE, NOT_RETURNABLE),
        # so the order view carries it, from the one rule (domain.last_return_date); the card keeps its own copy.
        self.assertEqual({order["order_id"]: order["last_return_date"] for order in listing["orders"]},
                         {"O1003": "2026-09-24", "O1001": "2026-09-22", "O1004": "2026-09-18", "O1002": "2026-09-08"})


if __name__ == "__main__":
    unittest.main()
