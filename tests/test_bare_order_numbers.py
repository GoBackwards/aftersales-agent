"""An order number typed without its letter (B3d, plan §8.6 B3d row and O9).

Behaviours: a bare number that is the number of exactly one of the current customer's orders names that
order, so a lookup reads it and a return prepares its card (still only a proposal: nothing in a turn
submits); a number shared by several own orders lists them, and a number that is no own order's number
offers all own orders, never the session's latest order (K5) and never another customer's order; the
number must stand alone, not inside a typed id or a longer number; a read whose number the user did not
type is dropped (B7: BusinessAdapter.read_problem; a return's number is resolved by code). The planner's first
proposal passes the provenance check, so the turn needs no structure correction. Offline: SQLite, the real business
adapter and, for HTTP, a scripted model stand-in. Engineering evidence, not task success.
"""
from itertools import count
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import http.client
import unittest

from support_agent.business import BusinessAdapter, mention_refs, order_number
from support_agent import copy as code_copy
from support_agent.assemble import business_parts
from support_agent.sessions import SessionStore

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
ALICE_ORDERS = ["O1001", "O1002", "O1003", "O1004"]
# A made-up customer whose two orders share one number (the demo seed has no such pair).
CAROL_ORDERS = (("A3001", "学习用台灯", "2026-09-16"), ("B3001", "学习用台灯罩", "2026-09-15"),
                ("C3002", "学习用书立", "2026-09-14"))


def rendered(result):
    """The business outcome as the reply's code sentences word it (B5d2: assemble.py from copy.py)."""
    lead, lines, note = business_parts(result)
    return "\n".join(part for part in (lead, *lines, note) if part)


def ref(order_id, quote, role="query"):
    return {"order_id": order_id, "role": role, "quote": quote}


def item(kind, quote, reference):
    return {"kind": kind, "subkind": "none", "quote": quote, "history_index": None,
            "order_quote": reference["quote"], "order_id": reference["order_id"], "fee_subject": "none"}


def lookup(*refs):
    """Reads (B7): one own_order_status item per reference, quoting the order's words."""
    return [item("own_order_status", reference["quote"], reference) for reference in refs]


def ask_return(quote, *refs):
    return [item("return_request", quote, reference) for reference in refs]


def withdraw(quote, *refs):
    return [item("withdraw", quote, reference) for reference in refs]


class OrderNumberTests(unittest.TestCase):
    def test_the_number_of_an_order_id_is_its_digits_without_the_letter(self):
        cases = {"O1003": "1003", "1003": "1003", "b3001": "3001", "current": None, "all_mine": None,
                 "AB1003": None, "O10x3": None, "": None, None: None, "O1003 ": None}
        for order_id, expected in cases.items():
            with self.subTest(order_id=order_id):
                self.assertEqual(order_number(order_id), expected)


class BareNumberValidationTests(unittest.TestCase):
    catalog = {"orders": [{"order_id": order_id, "product_name": "x"} for order_id in ALICE_ORDERS]}

    def problems(self, reads, message, catalog=catalog):
        """B7: why each read would be dropped (BusinessAdapter.read_problem), None when it is sourced."""
        return [BusinessAdapter.read_problem(row, message, catalog, []) for row in reads]

    def test_a_typed_number_sources_the_order_it_is_the_number_of(self):
        for proposal, message in ((ref("O1003", "编号1003"), "编号1003那件是什么"),
                                  (ref("none", "1003"), "麻烦查一下1003"),
                                  (ref("O1003", "1003号"), "1003号那单到哪一步了")):
            with self.subTest(proposal=proposal):
                self.assertEqual(self.problems(lookup(proposal), message), [None])
                # typing it is the provenance
                self.assertEqual(self.problems(lookup(proposal), message, catalog={"orders": []}), [None])

    def test_a_number_that_is_no_own_orders_number_is_still_sourced(self):
        # The read offers the candidates; the planner gets no correction for a number the user did type.
        self.assertEqual(self.problems(lookup(ref("none", "9999")), "9999这一单查一下"), [None])
        self.assertEqual(self.problems(lookup(ref("none", "2001")), "2001这一单查一下"), [None])

    def test_a_number_inside_an_id_a_longer_number_or_a_date_sources_nothing(self):
        for proposal, message in ((ref("O1003", "1003"), "O1003是哪件东西"),
                                  (ref("O1003", "21003"), "单号21003是哪件"),
                                  (ref("O1003", "10031"), "单号10031是哪件"),
                                  (ref("O2026", "2026-09-17"), "2026-09-17签收的那件"),
                                  (ref("O1003", "1003"), "a1003是哪件")):
            with self.subTest(proposal=proposal, message=message):
                self.assertEqual(self.problems(lookup(proposal), message), ["ORDER_NOT_SOURCED"])

    def test_a_proposal_whose_number_the_user_did_not_type_stays_unsourced(self):
        self.assertEqual(self.problems(lookup(ref("O1004", "1003")), "1003那单呢"), ["ORDER_NOT_SOURCED"])


class BareNumberTurnTests(unittest.TestCase):
    """The business boundary of a chat turn (no model) on the real store."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = SessionStore(Path(self.folder.name) / "numbers.sqlite3", clock=lambda: 1_000)
        self.addCleanup(self.store.close)
        with self.store._connection:
            self.store._connection.executemany(
                "INSERT INTO orders (order_id, customer_id, product_name, category, condition, delivered_at, "
                "paid_amount_cents, status) VALUES (?, 'carol', ?, 'general', 'sealed', ?, 3000, 'delivered')",
                CAROL_ORDERS)
        self.ids = count()
        self.sessions = {}

    def apply(self, message, proposed, customer="alice"):
        session_id = self.sessions.setdefault(customer, self.store.create_session(customer)["session_id"])
        started = self.store.begin_turn(customer, session_id, f"n-{next(self.ids)}", message, mode="offline_graph")
        results = BusinessAdapter(self.store, customer, session_id, started["run_id"]).apply(
            [{"index": index, **item} for index, item in enumerate(proposed, 1)])
        self.store.finish_turn(customer, session_id, started["run_id"], [{"role": "user", "content": message}],
                               {"reply": results[-1].get("reply", ""), "business": results})
        return results[0] if len(results) == 1 else results

    def open_orders(self, customer="alice"):
        return [card["order_id"] for card in self.store.get_session(customer, self.sessions[customer])["open_cards"]]

    def assertNothingSubmitted(self):
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_lookup_by_number_reads_that_one_own_order_and_says_which_it_is(self):
        result = self.apply("帮我看下1003这单", lookup(ref("O1003", "1003")))
        self.assertEqual((result["status"], [row["order_id"] for row in result["orders"]]), ("order_found", ["O1003"]))
        self.assertEqual(result["binding"]["number_refs"], [
            {"quote": "1003", "span": "1003", "role": "query", "proposed_order_id": "O1003",
             "matched_order_ids": ["O1003"], "outcome": "unique"}])
        self.assertEqual(result["binding"]["canonical_order_ids"], ["O1003"])
        text = rendered(result)
        self.assertTrue(text.startswith(code_copy.MENTION_MATCHED.format(span="1003", order_id="O1003",
                                                                        product="学习用饼干礼盒")))
        self.assertNothingSubmitted()

    def test_a_number_proposed_as_typed_is_normalised_to_the_own_order(self):
        result = self.apply("1001 那单查一下", lookup(ref("1001", "1001")))
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1001"])
        self.assertEqual(mention_refs(result["binding"])[0]["matched_order_ids"], ["O1001"])

    def test_a_return_by_number_prepares_that_orders_card_and_submits_nothing(self):
        result = self.apply("1001 退掉，用不上", ask_return("退掉", ref("1001", "1001", "target")))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual((result["card"]["order_id"], result["card"]["status"]), ("O1001", "pending"))
        self.assertEqual(result["binding"]["order_resolution"], "current_message")
        lines = rendered(result).splitlines()
        self.assertEqual(lines[0], code_copy.MENTION_MATCHED.format(span="1001", order_id="O1001", product="学习用保温杯"))
        self.assertEqual(lines[1], code_copy.CARD_READY.format(order_id="O1001", product="学习用保温杯"))
        self.assertNothingSubmitted()

    def test_a_number_beats_the_sessions_latest_order(self):
        self.apply("收纳盒那单现在什么情况", lookup(ref("O1004", "收纳盒那单")))
        result = self.apply("还是 1001 退了吧", ask_return("退了吧", ref("O1001", "1001", "target")))
        self.assertEqual(self.open_orders(), ["O1001"])
        self.assertEqual(result["binding"]["order_resolution"], "current_message")

    def test_a_number_that_is_no_own_orders_number_offers_all_own_orders_not_the_latest_one(self):
        self.apply("收纳盒那单现在什么情况", lookup(ref("O1004", "收纳盒那单")))   # K5 would pick O1004
        result = self.apply("9999 也退掉", ask_return("退掉", ref("O9999", "9999", "target")))
        self.assertEqual(result["status"], "clarification_needed")
        self.assertEqual(sorted(row["order_id"] for row in result["candidate_orders"]), ALICE_ORDERS)
        self.assertIsNone(result["binding"]["order_resolution"])
        self.assertEqual(self.open_orders(), [])
        self.assertEqual(rendered(result).splitlines()[:2], [code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="“9999”"),
                                                             code_copy.CANDIDATES_LEAD])
        self.assertNothingSubmitted()

    def test_a_lookup_by_an_unmatched_number_lists_ones_own_orders(self):
        result = self.apply("9999这单查一下", lookup(ref("O9999", "9999")))
        self.assertEqual(result["status"], "order_found")
        self.assertEqual(result["binding"]["list_scope"], "all_owned")
        self.assertEqual(sorted(row["order_id"] for row in result["orders"]), ALICE_ORDERS)
        text = rendered(result)
        self.assertTrue(text.startswith(code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="“9999”")))
        self.assertIn(code_copy.ORDERS_LEAD.format(count=4), text)

    def test_another_customers_order_number_is_answered_from_ones_own_orders_only(self):
        alice = self.apply("2001 那单退掉", ask_return("退掉", ref("O2001", "2001", "target")))
        self.assertEqual(sorted(row["order_id"] for row in alice["candidate_orders"]), ALICE_ORDERS)
        self.assertNotIn("O2001", rendered(alice))
        self.assertNotIn("学习用笔记本", json.dumps(alice, ensure_ascii=False))
        bob = self.apply("1001 那单退掉", ask_return("退掉", ref("O1001", "1001", "target")), customer="bob")
        self.assertEqual([row["order_id"] for row in bob["candidate_orders"]], ["O2001"])
        bob_text = rendered(bob)
        self.assertNotIn("O1001", bob_text)
        self.assertFalse(any(name in json.dumps(bob, ensure_ascii=False)
                             for name in ("学习用保温杯", "学习用帆布包", "学习用饼干礼盒", "学习用收纳盒")))
        self.assertEqual(self.open_orders(), [])
        self.assertEqual(self.open_orders("bob"), [])
        self.assertNothingSubmitted()

    def test_a_number_shared_by_several_own_orders_lists_them_as_candidates(self):
        result = self.apply("3001 退掉", ask_return("退掉", ref("A3001", "3001", "target")), customer="carol")
        self.assertEqual(result["status"], "clarification_needed")
        self.assertEqual([row["order_id"] for row in result["candidate_orders"]], ["A3001", "B3001"])
        self.assertEqual(self.open_orders("carol"), [])
        self.assertEqual(rendered(result).splitlines()[0], code_copy.CANDIDATES_LEAD)
        looked = self.apply("3001 是哪件", lookup(ref("B3001", "3001")), customer="carol")
        self.assertEqual([row["order_id"] for row in looked["orders"]], ["A3001", "B3001"])
        self.assertTrue(rendered(looked).startswith(code_copy.CANDIDATES_LEAD))
        single = self.apply("3002 退掉", ask_return("退掉", ref("C3002", "3002", "target")), customer="carol")
        self.assertEqual(single["card"]["order_id"], "C3002")
        self.assertNothingSubmitted()

    def test_a_number_names_the_one_card_to_withdraw(self):
        self.apply("O1001、O1004 一并退", ask_return("一并退", ref("O1001", "O1001", "target"),
                                                     ref("O1004", "O1004", "target")))
        result = self.apply("1004 那张不要了", withdraw("不要了", ref("O1004", "1004", "target")))
        self.assertEqual((result["status"], result["voided_order_id"]), ("card_voided", "O1004"))
        self.assertEqual(self.open_orders(), ["O1001"])

    def test_a_typed_letter_id_is_never_read_as_a_bare_number(self):
        result = self.apply("O1003 哪件来着", lookup(ref("O1003", "O1003")))
        self.assertNotIn("number_refs", result["binding"])
        self.assertEqual(result["binding"]["canonical_order_ids"], ["O1003"])

    def test_a_typed_foreign_id_beside_an_unmatched_number_is_still_not_found(self):
        result = self.apply("O2001 或者 9999 退掉", ask_return("退掉", ref("O2001", "O2001", "target"),
                                                               ref("O9999", "9999", "target")))
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "ORDER_NOT_FOUND"))
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_named_order_beside_an_unmatched_number_gets_its_card_and_the_number_is_reported(self):
        result = self.apply("O1001、9999 都退", ask_return("都退", ref("O1001", "O1001", "target"),
                                                              ref("O9999", "9999", "target")))
        self.assertEqual(self.open_orders(), ["O1001"])
        self.assertIn(code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="“9999”"), rendered(result))
        self.assertNothingSubmitted()


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class BareNumberHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.model = ScriptedModel([])
        self.server = create_server(db_path=directory / "numbers.sqlite3", credentials={"numbers-alice": "alice"},
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
        headers = {"Authorization": "Bearer numbers-alice"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                               headers)
            raw = connection.getresponse()
            return raw.status, json.loads(raw.read())
        finally:
            connection.close()

    def test_the_planners_first_number_proposal_passes_without_a_correction(self):
        from tests.test_graph_agent import planned
        self.model.outputs.append(planned(lookup(ref("O1003", "单号1003"))))
        status, result = self.request("POST", f"/sessions/{self.sid}/messages",
                                      {"message": "单号1003是哪件东西", "request_id": "number"})
        self.assertEqual((status, result["run_status"]), (200, "completed"), result.get("error"))
        self.assertEqual(len(self.model.calls), 1)          # one plan, no structure correction, no answer call
        self.assertIn(code_copy.MENTION_MATCHED.format(span="1003", order_id="O1003", product="学习用饼干礼盒"),
                      result["reply"])
        business = next(block for block in result["presentation"]["blocks"] if block["type"] == "business")
        self.assertEqual([order["order_id"] for order in business["orders"]], ["O1003"])
        self.assertNotIn("structure_correction", [entry.get("type") for entry in result.get("trace", [])])


if __name__ == "__main__":
    unittest.main()
