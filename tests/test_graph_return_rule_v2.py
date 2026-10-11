"""B2 return rule v2 (decision D6): the item's condition replaces the opened flag and the rule follows the
seven-day no-reason return: delivered, receipt day 0 to 7, an item opened only to inspect it still qualifies,
a used or no longer intact item and opened food do not. Seed orders keep their verdicts; ineligibility is worded
from the rule code, never as law (B5d2: copy.NOT_RETURNABLE with the last return date); a return request outside
the window gets one line pointing to the platform's after-sales quality entry.

Wording in these tests is written for them (no evaluation-suite text). Offline engineering and protocol
evidence ([工程]/[协议]); it says nothing about answer quality or task success.
"""
from contextlib import closing
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from support_agent import copy as code_copy
from support_agent.assemble import business_parts, eligibility_text
from support_agent.domain import (CONDITIONS, RETURN_WINDOW_DAYS, SCENARIO_DATE, SEED_ORDERS, Order, Store,
                                  eligibility_code, order_rule_code, order_view)
from support_agent.product_api import list_orders
from support_agent.sessions import SCHEMA_VERSION, SessionStore

# B5d2: no model reads order_view any more (the answer reads domain.order_facts); its rule fields are named for what
# they are and the outcome is a code, worded only by assemble.eligibility_text from copy.py.
VIEW_KEYS_IN_ORDER = ["order_id", "product_name", "category", "opened", "delivered_at", "paid_amount_cents", "status",
                      "category_label", "days_since_delivery", "return_eligible", "eligibility_code",
                      "last_return_date", "last_return_day", "return_request"]
LAW_WORDS = re.compile(r"法律|法定|法规|条例|权利")
# The v4 orders table, as sessions.SCHEMA_VERSION 4 (3c to B1) created it.
V4_ORDERS = """CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product_name TEXT NOT NULL,
    category TEXT NOT NULL, opened INTEGER NOT NULL CHECK (opened IN (0, 1)), delivered_at TEXT NOT NULL,
    paid_amount_cents INTEGER NOT NULL CHECK (paid_amount_cents >= 0), status TEXT NOT NULL)"""
V4_RETURNS = """CREATE TABLE return_requests (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id), reason TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE (customer_id, order_id))"""


def made_order(*, days=3, condition="sealed", category="general", status="delivered", order_id="O9001"):
    delivered = (SCENARIO_DATE - timedelta(days=days)).isoformat()
    return Order(order_id, "alice", "测试用台灯", category, condition, delivered, 1000, status)


def ineligible_result(view):
    return {"ok": True, "status": "ineligible", "reply": "", "order": view}


class ReturnRuleV2Tests(unittest.TestCase):
    def test_receipt_day_zero_through_seven_qualifies_and_day_eight_does_not(self):
        for days, expected in ((0, "eligible"), (RETURN_WINDOW_DAYS, "eligible"),
                               (RETURN_WINDOW_DAYS + 1, "outside_window"), (-1, "outside_window")):
            with self.subTest(days=days):
                self.assertEqual(eligibility_code("delivered", days, "general", "sealed"), expected)
        view = order_view(made_order(days=RETURN_WINDOW_DAYS))
        self.assertIs(view["last_return_day"], True)
        self.assertIs(order_view(made_order(days=RETURN_WINDOW_DAYS - 1))["last_return_day"], False)

    def test_an_item_opened_only_to_inspect_it_still_qualifies(self):
        for days in (0, 3, RETURN_WINDOW_DAYS):
            with self.subTest(days=days):
                order = made_order(days=days, condition="inspected_only")
                self.assertEqual(order_rule_code(order), "eligible")
                view = order_view(order)
                self.assertIs(view["opened"], True)
                self.assertIs(view["return_eligible"], True)
                self.assertIs(view["last_return_day"], days == RETURN_WINDOW_DAYS)


    def test_a_used_or_impaired_item_never_qualifies(self):
        for days in (0, RETURN_WINDOW_DAYS):
            with self.subTest(days=days):
                order = made_order(days=days, condition="used_or_impaired")
                self.assertEqual(eligibility_code("delivered", days, "general", "used_or_impaired"), "used_or_impaired")
                self.assertEqual(order_rule_code(order), "used_or_impaired")
                self.assertIs(order_view(order)["last_return_day"], False)


    def test_opened_food_never_qualifies_even_when_only_inspected(self):
        for condition in ("inspected_only", "used_or_impaired"):
            with self.subTest(condition=condition):
                self.assertEqual(eligibility_code("delivered", 0, "food", condition), "food_opened")
        self.assertEqual(eligibility_code("delivered", 0, "food", "sealed"), "eligible")

    def test_delivery_and_the_window_are_checked_before_the_condition(self):
        for condition in CONDITIONS:
            for category in ("general", "food"):
                with self.subTest(condition=condition, category=category):
                    self.assertEqual(eligibility_code("shipped", 2, category, condition), "not_delivered")
                    self.assertEqual(eligibility_code("delivered", RETURN_WINDOW_DAYS + 1, category, condition),
                                     "outside_window")

    def test_an_unknown_condition_is_rejected_instead_of_guessed(self):
        for condition in ("opened", "", None, True, False):
            with self.subTest(condition=condition):
                with self.assertRaises(ValueError):
                    eligibility_code("delivered", 1, "general", condition)

    def test_every_order_the_previous_rule_accepted_is_still_accepted(self):
        # Rule v1 (937b49f): delivered, day 0-7, unopened. v2 only adds inspected items, never removes one.
        for days in (-1, 0, 4, RETURN_WINDOW_DAYS, RETURN_WINDOW_DAYS + 1):
            for category in ("general", "food"):
                for status in ("delivered", "shipped"):
                    previous = status == "delivered" and 0 <= days <= RETURN_WINDOW_DAYS
                    with self.subTest(days=days, category=category, status=status):
                        if previous:
                            self.assertEqual(eligibility_code(status, days, category, "sealed"), "eligible")


class SeedOrderTests(unittest.TestCase):
    def test_the_five_seed_orders_keep_their_conclusions(self):
        views = {order.order_id: order_view(order) for order in SEED_ORDERS}
        self.assertEqual({order_id: view["eligibility_code"] for order_id, view in views.items()},
                         {"O1001": "eligible", "O1002": "outside_window", "O1003": "food_opened",
                          "O1004": "eligible", "O2001": "eligible"})
        self.assertEqual({order.order_id: order.condition for order in SEED_ORDERS},
                         {"O1001": "sealed", "O1002": "sealed", "O1003": "inspected_only",
                          "O1004": "sealed", "O2001": "sealed"})


    def test_the_order_view_carries_the_rule_code_and_no_teaching_note(self):
        for order in SEED_ORDERS:
            with self.subTest(order=order.order_id):
                view = order_view(order)
                self.assertEqual(view["eligibility_code"], order_rule_code(order))
                self.assertFalse({"teaching_rule_eligible", "teaching_rule_note"} & set(view))
                self.assertNotIn("教学", json.dumps(view, ensure_ascii=False))


    def test_the_view_derives_opened_from_the_condition(self):
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                view = order_view(made_order(condition=condition))
                self.assertEqual(list(view), VIEW_KEYS_IN_ORDER)
                self.assertIs(view["opened"], condition != "sealed")
                self.assertNotIn("condition", view)



class DisplayVerdictTests(unittest.TestCase):
    def test_the_display_code_of_a_view_is_the_backend_code_for_every_order(self):
        for status in ("delivered", "shipped"):
            for days in (-1, 0, RETURN_WINDOW_DAYS, RETURN_WINDOW_DAYS + 1):
                for category in ("general", "food"):
                    for condition in CONDITIONS:
                        order = made_order(days=days, condition=condition, category=category, status=status)
                        with self.subTest(status=status, days=days, category=category, condition=condition):
                            backend = eligibility_code(status, days, category, condition)
                            view = order_view(order)
                            self.assertEqual(view["eligibility_code"], backend)
                            self.assertIs(view["return_eligible"], backend == "eligible")
                            if backend != "eligible":
                                self.assertEqual(eligibility_text(view), code_copy.NOT_RETURNABLE[backend].format(
                                    last_date=view["last_return_date"]))


    def test_ineligibility_is_worded_from_the_rule_code_and_never_as_law(self):
        for text in (code_copy.QUALITY_PROBLEM_PATH, *code_copy.NOT_RETURNABLE.values()):
            with self.subTest(text=text):
                self.assertIsNone(LAW_WORDS.search(text))
        cases = {"outside_window": made_order(days=RETURN_WINDOW_DAYS + 3),
                 "food_opened": made_order(category="food", condition="inspected_only"),
                 "used_or_impaired": made_order(condition="used_or_impaired"),
                 "not_delivered": made_order(status="shipped")}
        for code, order in cases.items():
            with self.subTest(code=code):
                view = order_view(order)
                _, lines, _ = business_parts(ineligible_result(view))
                self.assertIn(code_copy.NOT_RETURNABLE[code].format(last_date=view["last_return_date"]), lines[0])
                self.assertIsNone(LAW_WORDS.search("".join(lines)))


    def test_a_return_request_outside_the_window_gets_the_quality_path_once(self):
        view = order_view(next(order for order in SEED_ORDERS if order.order_id == "O1002"))
        lead, lines, note = business_parts(ineligible_result(view))
        self.assertEqual("".join([lead, *lines, note]).count(code_copy.QUALITY_PROBLEM_PATH), 1)
        self.assertIn("2026-09-08", lines[0])                      # the last return day, from the rule (§4.1)
        self.assertIn("原平台", code_copy.QUALITY_PROBLEM_PATH)
        self.assertNotIn("京东", code_copy.QUALITY_PROBLEM_PATH)   # the assistant names no real platform (D8)
        self.assertNotRegex(code_copy.QUALITY_PROBLEM_PATH, r"\d")  # and states no deadline it has no source for


    def test_other_ineligible_reasons_get_no_quality_path_and_a_list_gets_it_once(self):
        for order in (next(order for order in SEED_ORDERS if order.order_id == "O1003"),
                      made_order(condition="used_or_impaired"), made_order(status="shipped")):
            with self.subTest(order=(order.category, order.condition, order.status)):
                self.assertNotIn(code_copy.QUALITY_PROBLEM_PATH, "".join(
                    business_parts(ineligible_result(order_view(order)))[1:2][0]))
        alice = [order_view(order) for order in SEED_ORDERS if order.customer_id == "alice"]
        listing = {"ok": True, "status": "order_found", "reply": "", "orders": alice,
                   "binding": {"list_scope": "all_owned"}}
        lead, lines, note = business_parts(listing)
        self.assertIn(code_copy.NOT_RETURNABLE["outside_window"].format(last_date="2026-09-08"), "\n".join(lines))
        self.assertEqual(note.count(code_copy.QUALITY_PROBLEM_PATH), 1)


    def test_the_order_panel_words_each_condition_from_the_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.sqlite3"
            with closing(Store(path)) as store:
                with store._connection:
                    store._connection.execute("UPDATE orders SET condition = 'inspected_only' WHERE order_id = 'O1001'")
                    store._connection.execute("UPDATE orders SET condition = 'used_or_impaired' WHERE order_id = 'O1004'")
                rows = {row["order_id"]: row for row in list_orders(store, "alice")["orders"]}
        self.assertIs(rows["O1001"]["return_eligible"], True)
        self.assertEqual(rows["O1001"]["eligibility_text"], code_copy.RETURNABLE.format(last_date="2026-09-22"))
        self.assertEqual(rows["O1004"]["eligibility_text"],
                         code_copy.NOT_RETURNABLE["used_or_impaired"].format(last_date="2026-09-18"))
        self.assertNotIn("condition", rows["O1001"])



class SchemaV5Tests(unittest.TestCase):
    def test_a_v4_database_with_the_opened_column_is_renamed_aside_and_rebuilt_with_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(V4_ORDERS)
                connection.execute(V4_RETURNS)
                connection.execute("INSERT INTO orders VALUES ('O1001','alice','旧库里的商品','general',1,'2026-09-15',8900,"
                                   "'delivered')")
                connection.execute("PRAGMA user_version = 4")
                connection.commit()
            old_bytes = path.read_bytes()
            self.assertGreaterEqual(SCHEMA_VERSION, 5)   # B3a moved to v6; the v4 rebuild is unchanged
            with closing(SessionStore(path)) as store:
                backup = store.backup_path
                self.assertEqual({order.order_id: order.condition for order in store.owned_orders("alice")},
                                 {"O1001": "sealed", "O1002": "sealed", "O1003": "inspected_only", "O1004": "sealed"})
            self.assertEqual(backup.read_bytes(), old_bytes)   # renamed aside, never deleted or rewritten (D2)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                columns = [row[1] for row in connection.execute("PRAGMA table_info(orders)")]
            self.assertIn("condition", columns)
            self.assertNotIn("opened", columns)

    def test_the_orders_table_accepts_only_the_three_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(Store(Path(folder) / "check.sqlite3")) as store:
                for condition in CONDITIONS:
                    with store._connection:
                        store._connection.execute("UPDATE orders SET condition = ? WHERE order_id = 'O1001'",
                                                  (condition,))
                for condition in ("opened", "", "SEALED"):
                    with self.subTest(condition=condition):
                        with self.assertRaises(sqlite3.IntegrityError):
                            with store._connection:
                                store._connection.execute("UPDATE orders SET condition = ? WHERE order_id = 'O1001'",
                                                          (condition,))


if __name__ == "__main__":
    unittest.main()
