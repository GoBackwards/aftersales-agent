"""Order ownership, return eligibility, and persistence for the simulated business domain.
"""

from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support_agent.domain import SCENARIO_DATE, Store, ToolError, order_rule_code, order_view


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "scenario.sqlite3"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_read_own_order_and_fixed_scene(self):
        view = order_view(self.store._owned_order("alice", "O1001"))
        self.assertEqual(view["order_id"], "O1001")
        self.assertIs(view["opened"], False)
        self.assertNotIn("customer_id", view)
        self.assertEqual(view["days_since_delivery"], 3)
        self.assertEqual(SCENARIO_DATE.isoformat(), "2026-09-18")

    def test_cross_customer_access_is_indistinguishable_from_missing_order(self):
        errors = []
        for order_id in ("O2001", "DOES-NOT-EXIST"):
            with self.assertRaises(ToolError) as caught:
                self.store._owned_order("alice", order_id)
            errors.append((caught.exception.code, caught.exception.message))
        self.assertEqual(errors[0][0], "ORDER_NOT_FOUND")
        self.assertEqual(errors[0], errors[1])
        self.assertNotIn("bob", str(errors[0]))
        self.assertNotIn("O2001", self.store.owned_order_ids("alice"))
        self.assertNotIn("O2001", [order.order_id for order in self.store.owned_orders("alice")])
        self.assertEqual(self.store._owned_order("bob", "O2001").order_id, "O2001")
        self.assertEqual(self.store.owned_order_ids("bob"), {"O2001"})
        self.assertEqual(self.store.list_return_requests(), [])

    def test_eligibility_of_seed_orders(self):
        for order_id, eligible, code in (
            ("O1001", True, "eligible"),
            ("O1002", False, "outside_window"),
            ("O1003", False, "food_opened"),
        ):
            with self.subTest(order_id=order_id):
                order = self.store._owned_order("alice", order_id)
                self.assertEqual(order_rule_code(order), code)
                view = order_view(order)
                # B5d2: the rule fields are named for what they are; the outcome is a code, worded by the reply.
                self.assertIs(view["return_eligible"], eligible)
                self.assertEqual(view["eligibility_code"], code)
                self.assertFalse({"teaching_rule_eligible", "teaching_rule_note"} & set(view))
        self.assertEqual(self.store.list_return_requests(), [])

    def test_seventh_day_order_is_eligible_and_marked_as_last_return_day(self):
        order = self.store._owned_order("alice", "O1004")
        self.assertEqual((SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days, 7)
        self.assertEqual(order.category, "general")
        self.assertIs(order.opened, False)
        self.assertEqual(order_rule_code(order), "eligible")
        view = order_view(order)
        self.assertIs(view["return_eligible"], True)
        self.assertEqual(view["last_return_date"], "2026-09-18")
        self.assertIs(view["last_return_day"], True)
        self.assertIs(order_view(self.store._owned_order("alice", "O1001"))["last_return_day"], False)
        self.assertEqual(self.store.list_return_requests(), [])

    def test_eighth_day_order_is_ineligible(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE orders SET delivered_at = ? WHERE order_id = ?",
                ("2026-09-10", "O1004"),
            )
            connection.commit()
        order = self.store._owned_order("alice", "O1004")
        self.assertEqual((SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days, 8)
        self.assertEqual(order_rule_code(order), "outside_window")
        self.assertIs(order_view(order)["last_return_day"], False)

    def test_eligibility_reads_the_current_stored_order_state(self):
        self.assertEqual(order_rule_code(self.store._owned_order("alice", "O1001")), "eligible")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE orders SET condition = 'used_or_impaired' WHERE order_id = 'O1001'")
            connection.commit()
        self.assertEqual(order_rule_code(self.store._owned_order("alice", "O1001")), "used_or_impaired")

    def test_seed_initialization_does_not_reset_existing_order_state(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE orders SET condition = 'used_or_impaired' WHERE order_id = 'O1001'")
            connection.commit()
        self.store.close()
        self.store = Store(self.db_path)
        self.assertEqual(self.store._owned_order("alice", "O1001").condition, "used_or_impaired")
        self.assertIs(self.store._owned_order("alice", "O1001").opened, True)


if __name__ == "__main__":
    unittest.main()
