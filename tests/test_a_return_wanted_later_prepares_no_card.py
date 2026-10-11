"""A return deferred by the customer remains read-only, preserves existing cards, and can resume later.

The planner, decision table and offline demo use the same deferred-return subkind.
"""
from contextlib import closing
from itertools import count
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support_agent import assemble, decide, understand
from support_agent import copy as code_copy
from support_agent.business import BusinessAdapter
from support_agent.sessions import SessionStore

OWN = ["O1001", "O1002", "O1003", "O1004"]


def item(quote, *, subkind="none", order_quote="", order_id="none", kind="return_request", index=1):
    """One checked plan item (understand.parse numbered it)."""
    return {"index": index, "kind": kind, "subkind": subkind, "quote": quote, "history_index": None,
            "order_quote": order_quote, "order_id": order_id, "fee_subject": "none"}


def proposal(*items):
    """plan_turn arguments: the items without the index understand.parse adds."""
    return {"goal": "退货", "items": [{key: value for key, value in row.items() if key != "index"} for row in items],
            "order_stage": "unknown", "fee_flow": "unknown"}


class PlanLabelTests(unittest.TestCase):
    def test_the_planner_may_label_a_return_for_later_and_only_a_return_reads_it(self):
        enum = understand.schema(OWN)["properties"]["items"]["items"]["properties"]["subkind"]["enum"]
        self.assertIn(decide.LATER, enum)
        plan = understand.parse(proposal(item("下礼拜再退", subkind="later"),
                                         item("运费谁出", subkind="later", kind="policy_question")), OWN)
        self.assertEqual([(row["kind"], row["subkind"]) for row in plan["items"]],
                         [("return_request", "later"), ("policy_question", "none")])

    def test_the_code_never_reads_the_timing_from_the_words(self):
        message = "O1001 退掉，不过下礼拜再弄"
        plan = understand.parse(proposal(item("O1001 退掉", order_quote="O1001", order_id="O1001")), OWN)
        kept, rejected = understand.check(plan, message, [], {"orders": []}, [])
        self.assertEqual(([row["subkind"] for row in kept], rejected), (["none"], []))

    def test_the_planning_prompt_tells_a_return_for_later_from_a_pause_over_a_prepared_card(self):
        prompt = understand.PROMPT
        for words in ("subkind 填 later", "不备确认卡", "现在就办的填 none", "卡会留着，这类话归 chit_chat"):
            with self.subTest(words=words):
                self.assertIn(words, prompt)
        self.assertLessEqual(len(prompt), 3000)


class DecisionTests(unittest.TestCase):
    def test_a_return_for_later_is_only_ever_its_facts(self):
        for view in ({"order_id": "O1001", "return_eligible": True, "return_request": None},
                     {"order_id": "O1001", "return_eligible": False, "return_request": None},
                     {"order_id": "O1001", "return_eligible": True, "return_request": {"status": "pending"}}):
            with self.subTest(view=view):
                self.assertEqual(decide.return_outcome(view, ["O1001"], later=True), "facts")
        self.assertEqual(decide.return_outcome({"order_id": "O1001", "return_eligible": True}, []), "prepare")


class LaterReturnTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "later.sqlite3"
        self.store = SessionStore(self.db, clock=lambda: 1000)
        self.addCleanup(self.store.close)
        self.sid = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def apply(self, message, *items):
        started = self.store.begin_turn("alice", self.sid, f"later-{next(self.ids)}", message, mode="offline_graph")
        results = BusinessAdapter(self.store, "alice", self.sid, started["run_id"]).apply(list(items))
        self.store.finish_turn("alice", self.sid, started["run_id"], [{"role": "user", "content": message}],
                               {"reply": "", "business": results})
        return results

    def cards(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT order_id, status, version FROM return_cards ORDER BY rowid").fetchall()

    def test_a_return_for_later_shows_the_orders_facts_and_its_button_and_writes_nothing(self):
        [result] = self.apply("O1001 打算退，等我出差回来再弄",
                              item("打算退", subkind="later", order_quote="O1001", order_id="O1001"))
        self.assertEqual((result["status"], result["return_later"], result["item_kind"], result["item_indexes"]),
                         ("order_found", True, "return_request", [1]))
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O1001"])
        self.assertEqual((self.cards(), self.store.list_return_requests()), ([], []))
        block = assemble.business_block(result)
        self.assertEqual((block["action"], block["lead"]), ("return_request", code_copy.RETURN_NOT_STARTED))
        self.assertEqual([order["order_id"] for order in block["orders"]], ["O1001"])   # the page's "申请退货"
        self.assertTrue(block["text"].startswith(code_copy.RETURN_NOT_STARTED))
        self.assertIn(code_copy.APPLY_HINT, block["text"])
        self.assertNotIn("确认卡在下方", block["text"])   # no card sentence (copy.CARD_READY)

    def test_a_return_now_beside_one_for_later_cards_only_the_one_now(self):
        now, later = self.apply("O1004 现在办，O1001 过阵子再说",
                                item("现在办", order_quote="O1004", order_id="O1004"),
                                item("过阵子再说", subkind="later", order_quote="O1001", order_id="O1001", index=2))
        self.assertEqual((now["status"], now["item_indexes"]), ("awaiting_confirmation", [1]))
        self.assertEqual((later["status"], later.get("return_later"), later["item_indexes"]), ("order_found", True, [2]))
        self.assertEqual([row[:2] for row in self.cards()], [("O1004", "pending")])

    def test_the_next_turn_finds_the_order_a_return_for_later_showed(self):
        self.apply("O1001 打算退，等我出差回来再弄", item("打算退", subkind="later", order_quote="O1001", order_id="O1001"))
        [result] = self.apply("回来了，办吧", item("办吧"))
        self.assertEqual((result["status"], result["card"]["order_id"]), ("awaiting_confirmation", "O1001"))
        self.assertEqual(result["binding"]["order_resolution"], decide.BY_K5)
        self.assertEqual(self.store.list_return_requests(), [])

    def test_a_return_for_later_leaves_an_open_card_as_it_was(self):
        self.apply("O1001 申请退货", item("申请退货", order_quote="O1001", order_id="O1001"))
        before = self.cards()
        [result] = self.apply("O1001 那张卡先别提交，过几天再定",
                              item("过几天再定", subkind="later", order_quote="O1001", order_id="O1001"))
        self.assertEqual(result["status"], "order_found")
        self.assertEqual((self.cards(), before[0][1]), (before, "pending"))
        self.assertEqual(self.store.list_return_requests(), [])


class StandInTests(unittest.TestCase):

    def test_the_offline_demo_reads_a_return_word_with_a_pause_word_and_no_card_as_for_later(self):
        from support_agent.demo_model import plan_for
        orders = [{"order_id": order_id, "product_name": name} for order_id, name in
                  (("O1001", "学习用保温杯"), ("O1004", "学习用收纳盒"))]

        def plan(message, open_cards=()):
            return plan_for({"current_message": message, "history": [], "account_orders": orders,
                             "session_cards": {"open_card_order_ids": list(open_cards), "submitted_order_ids": []}})
        later = plan("O1001 申请退货，先别提交")["items"]
        self.assertEqual([(row["kind"], row["subkind"], row["order_id"]) for row in later],
                         [("return_request", "later", "O1001")])
        self.assertEqual([(row["kind"], row["subkind"]) for row in plan("O1001 申请退货")["items"]],
                         [("return_request", "none")])
        self.assertEqual([row["kind"] for row in plan("先别提交", open_cards=["O1001"])["items"]], ["chit_chat"])


if __name__ == "__main__":
    unittest.main()
