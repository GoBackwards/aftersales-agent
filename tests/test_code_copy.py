"""Code-written sentences and data catalogues of a chat reply (plan §5.6; B5d1 wrote them, B5d2 wires them).

The T8 wording guard scans support_agent/copy.py like every module (tests/test_wording_guard.py). These checks are
mechanical: fields, words, and the same guards the model's prose passes. Not answer quality.
"""
import ast
from pathlib import Path
import re
import string
import unittest

from support_agent import copy as code_copy
from support_agent import guards
from support_agent.domain import CONDITIONS, RETURN_WINDOW_DAYS, eligibility_code

APP = Path(__file__).resolve().parents[1]
PLATFORM_NAMES = ("京东", "淘宝", "天猫", "拼多多", "抖音", "美团", "苏宁", "唯品会")
FIELDS = {"order_id": "O3101", "order_ids": "O3101、O3102", "product": "电热水壶", "status": "待处理",
          "reason": "其他", "short_no": "H-0101", "last_date": "2026-09-20", "limit": "4", "count": "3",
          "span": "水壶", "amount": "12.00", "delivered_at": "2026-09-15", "day": "签收后第 3 天", "days": "3",
          "origin": "甲省", "destination": "乙省", "missing": "实际寄出的省份"}
# Records that back the sentences reporting an action (I6): the copy is used only when they exist.
RECORDS = [{"scope": "turn", "type": "return_request_submitted", "order_id": "O3101"},
           {"scope": "turn", "type": "ticket_created", "ticket": "H-0101"},
           {"scope": "turn", "type": "ticket_event_appended", "ticket": "H-0101"},
           {"scope": "turn", "type": "ticket_withdrawn", "ticket": "H-0101"},
           {"scope": "turn", "type": "card_voided", "order_id": "O3101"},
           # B5e: what the code's own blocks state this turn (an order not in the account, the orders listed).
           {"scope": "turn", "type": "order_not_in_account"}, {"scope": "turn", "type": "orders_shown"}]
# Moved word for word into copy.py by B5d2 (their old homes now read them from here).
MOVED_UNCHANGED = {"CANCEL_AFTERSALES_PATH", "SUBMITTED_NO_UNDO", "OPEN_CARDS_WAITING"}


def fields_of(template):
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def filled(template):
    return template.format(**{name: FIELDS[name] for name in fields_of(template)})


class CodeCopyTests(unittest.TestCase):
    def test_the_copy_holds_the_planned_sentences_and_names_the_ones_not_wired_yet(self):
        for name in ("CARD_READY", "CARD_KEPT", "REQUEST_EXISTS", "REQUEST_SUBMITTED", "ORDER_NOT_FOUND",
                     "CANDIDATES_LEAD", "NO_RETURNABLE_ORDER", "CARD_VOIDED", "ITEM_DEGRADED",
                     "CARD_FOLDED_AFTER_FAILURE", "FALLBACK", "ENVIRONMENT_NOTICE"):
            with self.subTest(name=name):
                self.assertIn(name, code_copy.TEXTS)
        # B8: one safety line per safety subkind the planner may label, and one for none.
        self.assertEqual({name[len("SAFETY."):] for name in code_copy.TEXTS if name.startswith("SAFETY.")},
                         {"product_hazard", "medical_emergency", "fraud_or_theft", "harm_occurred", "crisis", "none"})
        self.assertLessEqual(code_copy.NOT_WIRED, set(code_copy.TEXTS))
        self.assertIn(code_copy.SUBMIT_BY_CLICK, code_copy.CARD_READY)

    def test_every_reason_an_order_cannot_be_returned_has_its_sentence(self):
        codes = {eligibility_code(status, days, category, condition)
                 for status in ("delivered", "in_transit") for days in (0, RETURN_WINDOW_DAYS + 1)
                 for category in ("general", "food") for condition in CONDITIONS}
        self.assertEqual(set(code_copy.NOT_RETURNABLE), codes - {"eligible"})

    def test_templates_use_only_the_known_fields_and_a_reason_is_only_its_label(self):
        for name, text in code_copy.TEXTS.items():
            with self.subTest(name=name):
                self.assertLessEqual(fields_of(text), set(FIELDS))
        self.assertEqual(fields_of(code_copy.REQUEST_SUBMITTED), {"order_id", "reason"})

    def test_no_sentence_names_a_platform_or_the_demo_except_the_notice(self):
        catalogue = {**code_copy.TEXTS, **{"PATHS." + key: text for key, text in code_copy.PATHS.items()},
                     **{f"OPTION_SETS.{key}.{index}": option for key, entry in code_copy.OPTION_SETS.items()
                        for index, option in enumerate(entry["options"])}}
        for name, text in catalogue.items():
            with self.subTest(name=name):
                self.assertFalse([word for word in PLATFORM_NAMES if word in text])
                if name != "ENVIRONMENT_NOTICE":
                    self.assertEqual(guards.a0_counts(text), {})

    def test_no_sentence_promises_asks_for_a_reason_or_a_credential_or_cites_law(self):
        ctx = guards.context(own_order_ids=["O3101", "O3102"], events=RECORDS, card_state="shown")
        for name, text in [*code_copy.TEXTS.items(), *code_copy.PATHS.items()]:
            with self.subTest(name=name):
                hits = {hit["code"] for hit in guards.check(filled(text), ctx)}
                self.assertFalse(hits & {"G2_UNBACKED_PROMISE", "G5_LAW_WORDING", "G6_FOREIGN_ORDER",
                                         "G7_CREDENTIAL_ASKED", "G8_REASON_ASKED"}, hits)
                # An action is reported only with its record (I6): with the records, no G1 is left.
                self.assertFalse({code for code in hits if code.startswith("G1_")}, hits)

    def test_the_fallback_passes_every_guard_and_denies_no_rule(self):
        self.assertEqual(guards.check(code_copy.FALLBACK, guards.context()), [])
        self.assertIsNone(re.search(r"没有(?:这|该)?(?:条|项)?(?:规定|规则|政策)|不存在", code_copy.FALLBACK))

    def test_the_option_sets_offer_no_return_reason_and_at_most_five_choices(self):
        # Decision 3 and G8's structural half: the reason is chosen on the card, never offered in chat.
        for key, entry in code_copy.OPTION_SETS.items():
            with self.subTest(option_set=key):
                self.assertNotEqual(entry["kind"], guards.REASON_OPTION_KIND)
                self.assertNotIn("reason", key)
                self.assertTrue(1 < len(entry["options"]) <= 5)

    def test_the_notice_states_the_demo_environment_and_its_business_date(self):
        notice = code_copy.ENVIRONMENT_NOTICE
        for words in ("演示环境", "模拟", "不产生真实交易", "没有真人", "2026-09-18", "确认提交"):
            with self.subTest(words=words):
                self.assertIn(words, notice)

    def test_safety_lines_carry_only_the_standard_emergency_numbers(self):
        numbers = {number for text in code_copy.SAFETY.values() for number in re.findall(r"\d+", text)}
        self.assertEqual(numbers, {"110", "120", "119"})

    def test_the_sentences_are_newly_worded_except_the_ones_moved_unchanged(self):
        old = set()
        for module in ("return_cards", "tickets", "sessions", "product_api"):
            tree = ast.parse((APP / "support_agent" / f"{module}.py").read_text(encoding="utf-8"))
            old |= {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for name, text in code_copy.TEXTS.items():
            with self.subTest(name=name):
                if name not in MOVED_UNCHANGED:
                    self.assertNotIn(text, old)
        from support_agent import return_cards, tickets
        self.assertIs(return_cards.SUBMITTED_NO_UNDO, code_copy.SUBMITTED_NO_UNDO)
        self.assertIs(tickets.PLATFORM_CHANNEL, code_copy.PATHS["platform_human"])


if __name__ == "__main__":
    unittest.main()
