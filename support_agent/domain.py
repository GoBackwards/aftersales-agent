"""Deterministic teaching data: seed orders, the return rule, product mentions and the order store.

The rules in this module are invented for this exercise. They are not legal
advice or a description of any real merchant's returns policy, and nothing here
calls a rule a statutory right. The legacy model-facing tool set and its keyword
policy list were deleted in S1-1e; see af5307e.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3
from typing import Any


SCENARIO_DATE = date(2026, 9, 18)
RETURN_WINDOW_DAYS = 7
# The item's condition (B2, decision D6): still sealed; opened only to inspect it; used, or no longer intact.
CONDITIONS = ("sealed", "inspected_only", "used_or_impaired")
# A typed order id: one letter and exactly four digits ("O1003"). Whether it is the customer's own is decided
# against their orders where it is read (business, and the ticket summary in tickets.py, B4d).
ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z]\d{4}(?![A-Za-z0-9_-])")


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    product_name: str
    category: str
    condition: str
    delivered_at: str
    paid_amount_cents: int
    status: str = "delivered"

    @property
    def opened(self) -> bool:
        """Derived from the condition: anything but a sealed item has been opened."""
        return self.condition != "sealed"

    def public_dict(self) -> dict[str, Any]:
        """The order record as the order panel and a business result show it: ``opened`` stands in for ``condition``.

        Customer identity is provided by trusted context, not by the model.
        """
        return {"order_id": self.order_id, "product_name": self.product_name, "category": self.category,
                "opened": self.opened, "delivered_at": self.delivered_at,
                "paid_amount_cents": self.paid_amount_cents, "status": self.status}


SEED_ORDERS = (
    Order("O1001", "alice", "学习用保温杯", "general", "sealed", "2026-09-15", 8900),
    Order("O1002", "alice", "学习用帆布包", "general", "sealed", "2026-09-01", 5900),
    Order("O1003", "alice", "学习用饼干礼盒", "food", "inspected_only", "2026-09-17", 3900),
    Order("O1004", "alice", "学习用收纳盒", "general", "sealed", "2026-09-11", 4900),
    Order("O2001", "bob", "学习用笔记本", "general", "sealed", "2026-09-16", 2500),
)


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def eligibility_code(status: str, days_since_delivery: int, category: str, condition: str) -> str:
    """The return rule v2 (B2, decision D6), in one place: "eligible" or the first reason the order fails it.

    Aimed at the seven-day no-reason return and meant to be no stricter (plan I7; the wording has not been
    checked against the regulation, plan §9): the order is delivered; the receipt day is day 0 and day 7
    is the last day; an item opened only to inspect it still qualifies, a used or no longer intact one
    does not; opened food does not. Every order view, order fact and code sentence reads this decision
    (order_rule_code); nothing else words or recomputes it.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown item condition {condition!r}")
    if status != "delivered":
        return "not_delivered"
    if not 0 <= days_since_delivery <= RETURN_WINDOW_DAYS:
        return "outside_window"
    if category == "food" and condition != "sealed":
        return "food_opened"
    if condition == "used_or_impaired":
        return "used_or_impaired"
    return "eligible"


def last_return_date(order: Order) -> str | None:
    """The last day of the return window as YYYY-MM-DD (receipt day = day 0), or None before delivery."""
    if order.status != "delivered":
        return None
    return (date.fromisoformat(order.delivered_at) + timedelta(days=RETURN_WINDOW_DAYS)).isoformat()


def order_rule_code(order: Order) -> str:
    return eligibility_code(order.status, (SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days,
                            order.category, order.condition)


def order_facts(order: Order, return_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """One order's facts as the backend verified them, for the answer context (B5b, plan §2.3 step 3, §5.2).

    The answer model reads every order of the customer this way and only chooses which ones the reply shows
    (show_order_ids); the code renders them. The rule outcome is a code (eligibility_code), the request only
    its status: the reason picked on the card never reaches a model (plan I2). The planner never sees these
    facts (K3). B5d2: the answer reads these facts only; no model reads an order_view.
    """
    code = order_rule_code(order)
    return {"order_id": order.order_id, "product_name": order.product_name, "category": order.category,
            "status": order.status, "delivered_at": order.delivered_at,
            "days_since_delivery": (SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days,
            "condition": order.condition, "return_eligible": code == "eligible", "eligibility_code": code,
            "last_return_date": last_return_date(order),
            "return_request": {"status": return_request["status"]} if return_request else None}


CATEGORY_LABELS = {"general": "日用百货", "food": "食品"}


def order_view(order: Order, return_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """One customer's order with its rule facts, for business results and the order panel.

    B5d2 renamed the rule fields (``teaching_rule_eligible`` -> ``return_eligible``) and dropped the model-facing
    note: the rule outcome is its code, worded only by assemble.py from copy.py, and no model reads this view.
    """
    code = order_rule_code(order)
    days = (SCENARIO_DATE - date.fromisoformat(order.delivered_at)).days
    return {**order.public_dict(), "category_label": CATEGORY_LABELS.get(order.category, order.category),
            "days_since_delivery": days, "return_eligible": code == "eligible", "eligibility_code": code,
            "last_return_date": last_return_date(order),
            "last_return_day": code == "eligible" and days == RETURN_WINDOW_DAYS,
            "return_request": dict(return_request) if return_request else None}


def _name_chars(text: str) -> str:
    return "".join(char for char in text if char.isalnum())


def common_name_chars(names: list[str]) -> str:
    """Characters every product name shares (e.g. the "学习用" prefix); they identify nothing."""
    sets = [set(_name_chars(name)) for name in names]
    return "".join(sorted(set.intersection(*sets))) if len(sets) > 1 else ""


# Every product in the demo catalogue is named "学习用…"; that shared prefix
# identifies no product, even for a customer who owns only one order.
CATALOG_NAME_NOISE = common_name_chars([order.product_name for order in SEED_ORDERS])
_CLAUSE_BREAK = re.compile(r"[\s，,。．.！!？?；;、：:]+")
_HEAD_FOLLOWERS = "子那这的呢吗"


def _clause_hits(clause: str, orders: list[dict[str, str]], noise: set[str]) -> tuple[list[str], list[str]]:
    text = _name_chars(clause)
    by_bigram, by_head = [], []
    for order in orders:
        name = _name_chars(order["product_name"])
        if not name:
            continue
        # Two adjacent characters of the name, neither of them catalogue noise.
        if any(name[i] not in noise and name[i + 1] not in noise and name[i:i + 2] in text
               for i in range(len(name) - 1)):
            by_bigram.append(order["order_id"])
            continue
        # The head noun alone (杯 in 保温杯) counts only as "杯子"/"那个杯" or at the end of
        # the clause, so 包修期, 包装 or 本人 do not name a 帆布包 or 笔记本.
        head = name[-1]
        if head not in noise and any(text[i] == head and (i + 1 == len(text) or text[i + 1] in _HEAD_FOLLOWERS)
                                     for i in range(len(text))):
            by_head.append(order["order_id"])
    return by_bigram, by_head


def _clause_matches(clause: str, orders: list[dict[str, str]], noise: set[str]) -> list[str]:
    by_bigram, by_head = _clause_hits(clause, orders, noise)
    return sorted(by_bigram or by_head)


def resolve_product_mention(quote: str, order_id: str, orders: list[dict[str, str]]) -> dict[str, Any]:
    """Whether a product mention from the user can mean ``order_id`` among ONE customer's orders.

    The mention is read clause by clause. ``unique``: some clause names only this
    order. ``ambiguous``: a clause names it together with other orders (the
    candidates are returned, never a guess). ``none``: it is not named at all.
    """
    noise = set(CATALOG_NAME_NOISE) | set(common_name_chars([order["product_name"] for order in orders]))
    candidates, span = set(), None
    for clause in _CLAUSE_BREAK.split(quote or ""):
        matched = _clause_matches(clause, orders, noise)
        if matched == [order_id]:
            return {"outcome": "unique", "matched": matched, "span": clause}
        if order_id in matched:
            candidates.update(matched)
            span = span or clause
    if candidates:
        return {"outcome": "ambiguous", "matched": sorted(candidates), "span": span}
    return {"outcome": "none", "matched": [], "span": None}


def match_product_reference(quote: str, orders: list[dict[str, str]]) -> dict[str, Any]:
    """Every order of ONE customer that some clause of the mention names; unique if only one."""
    noise = set(CATALOG_NAME_NOISE) | set(common_name_chars([order["product_name"] for order in orders]))
    matched = sorted({order_id for clause in _CLAUSE_BREAK.split(quote or "")
                      for order_id in _clause_matches(clause, orders, noise)})
    return {"matched": matched, "unique": matched[0] if len(matched) == 1 else None}


class Store:
    """A small SQLite store; reopening never resets existing orders or returns."""

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                category TEXT NOT NULL,
                condition TEXT NOT NULL CHECK (condition IN ('sealed', 'inspected_only', 'used_or_impaired')),
                delivered_at TEXT NOT NULL,
                paid_amount_cents INTEGER NOT NULL CHECK (paid_amount_cents >= 0),
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS return_requests (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                reason_code TEXT NOT NULL,
                reason_text TEXT,
                source_action_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (customer_id, order_id)
            );
            """
        )
        with self._connection:
            self._connection.executemany(
                """INSERT OR IGNORE INTO orders
                   (order_id, customer_id, product_name, category, condition,
                    delivered_at, paid_amount_cents, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [tuple(asdict(order).values()) for order in SEED_ORDERS],
            )

    def close(self) -> None:
        self._connection.close()

    def _owned_order(self, customer_id: str, order_id: str) -> Order:
        row = self._connection.execute(
            "SELECT * FROM orders WHERE order_id = ? AND customer_id = ?",
            (order_id, customer_id),
        ).fetchone()
        if row is None:
            # Do not disclose whether another customer's order exists.
            raise ToolError("ORDER_NOT_FOUND", "订单不存在或无权访问。")
        return Order(**dict(row))

    def owned_order_ids(self, customer_id: str) -> set[str]:
        """Read-only: the simulated order IDs this customer may see (none of anyone else's)."""
        rows = self._connection.execute("SELECT order_id FROM orders WHERE customer_id = ?", (customer_id,))
        return {row[0] for row in rows}

    def owned_orders(self, customer_id: str) -> list[Order]:
        """Read-only: this customer's simulated orders, most recently delivered first."""
        rows = self._connection.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY delivered_at DESC, order_id",
            (customer_id,)).fetchall()
        return [Order(**dict(row)) for row in rows]

    def owned_return_requests(self, customer_id: str) -> dict[str, dict[str, Any]]:
        """Read-only: this customer's return requests keyed by order, without the reason.

        These rows reach order_view and order_facts (and so the answer context); the reason chosen on the card
        (and an "other" text) never does (plan I2, B3a).
        """
        rows = self._connection.execute(
            "SELECT id, order_id, status, created_at FROM return_requests WHERE customer_id = ?",
            (customer_id,)).fetchall()
        return {row["order_id"]: dict(row) for row in rows}

    def list_return_requests(self) -> list[dict[str, Any]]:
        """Read-only inspection for local tests/evaluation; not a model tool."""
        rows = self._connection.execute(
            "SELECT id, customer_id, order_id, reason_code, reason_text, source_action_id, status, created_at "
            "FROM return_requests ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
