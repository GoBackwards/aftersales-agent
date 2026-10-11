"""Production voice (B1, decision 2; B5d2 for the reply's code sentences and the answer's prompt): text the code renders
states no demo environment and names no real platform, the environment is stated once in a persistent UI notice, and
the planning call's text is in production voice too since B7 rewrote it (K14).

Wording in these tests is written for them (no evaluation-suite text). Offline engineering evidence
([协议]/[工程]); it says nothing about answer quality or task success.
"""
from datetime import timedelta
import hashlib
import importlib.util
from pathlib import Path
import re
import unittest

from support_agent.guards import A0_WORDS
from support_agent import copy as code_copy
from support_agent.assemble import business_parts, eligibility_text
from support_agent.domain import (CONDITIONS, RETURN_WINDOW_DAYS, SCENARIO_DATE, SEED_ORDERS, Order, eligibility_code,
                                  order_view)
from support_agent.product_api import ENVIRONMENT_NOTICE
from support_agent.sessions import FAILED_TURN_TEXT, RETRY_STEP
from support_agent.tickets import TECHNICAL_FAILURE_LEAD

APP = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
# Unbacked follow-up promises, the G2 phrases of plan §5.4; a handoff sentence must contain none of them.
FOLLOW_UP_PROMISES = re.compile(r"\d+\s*(?:小时|个工作日)内|尽快|稍后|会有人(?:联系|跟进)|排队")
PLATFORM_NAMES = ("京东", "淘宝", "天猫", "拼多多", "抖音", "美团", "苏宁", "唯品会")
# sha256 of the planning call's model-visible texts as B7 wrote them (understand.PROMPT, trust.TRUST_NOTE; B7r
# added the later subkind of return_request to the prompt); the answer's prompts and trust note are B5d2's (respond.py);
# the stage that rewrites one of them updates it here.
MODEL_VISIBLE_DIGESTS = {
    "PLANNING_PROMPT": "520afb84c351975aae622564f5fdb9023415527457bdc230e8231c1cff5acd34",
    "PLANNING_TRUST_NOTE": "7c6759f1d09172f0623b3dee3976fb826a1a57f5c901ef498d50f5318c72d0fc",
}


def demo_words(text):
    return {word: text.count(word) for word in A0_WORDS if word in text}


def view(order_id, return_request=None):
    order = next(order for order in SEED_ORDERS if order.order_id == order_id)
    return order_view(order, return_request)


def alice_views():
    rows = [view(order.order_id) for order in SEED_ORDERS if order.customer_id == "alice"]
    return sorted(rows, key=lambda row: row["delivered_at"], reverse=True)


class EligibilityWordingTests(unittest.TestCase):
    def test_every_rule_outcome_gets_one_display_clause_from_the_rule_code(self):
        days_cases = (-1, 0, RETURN_WINDOW_DAYS, RETURN_WINDOW_DAYS + 1)
        for status in ("delivered", "in_transit"):
            for days in days_cases:
                for category in ("general", "food"):
                    for condition in CONDITIONS:
                        delivered = (SCENARIO_DATE - timedelta(days=days)).isoformat()
                        order = Order("O9001", "alice", "测试用水壶", category, condition, delivered, 1000, status)
                        row = order_view(order)
                        with self.subTest(status=status, days=days, category=category, condition=condition):
                            text = eligibility_text(row)
                            code = eligibility_code(status, days, category, condition)
                            self.assertEqual(row["return_eligible"], code == "eligible")
                            if code == "eligible":
                                self.assertEqual(text, code_copy.RETURNABLE_LAST_DAY if row["last_return_day"] else
                                                 code_copy.RETURNABLE.format(last_date=row["last_return_date"]))
                            else:
                                self.assertEqual(text, code_copy.NOT_RETURNABLE[code].format(
                                    last_date=row["last_return_date"]))
                            self.assertEqual(demo_words(text), {})

    def test_an_existing_request_and_a_bare_record_are_worded_too(self):
        self.assertEqual(eligibility_text(view("O1004")), code_copy.RETURNABLE_LAST_DAY)
        self.assertEqual(eligibility_text(view("O1001", {"id": "RET-O1001", "status": "pending"})),
                         "已经有一条退货申请，当前状态：待处理。")
        self.assertEqual(eligibility_text({"order_id": "O1001", "product_name": "测试用水壶"}), "")


class RenderedBusinessTextTests(unittest.TestCase):
    """Every business outcome the code renders, built from real order views of the demo catalogue."""

    def results(self):
        alice = alice_views()
        card = view("O1001")
        pending = {"order_id": "O1001", "status": "pending", "action_id": "ACT-1", "version": 1}
        ambiguous = {"product_refs": [{"outcome": "ambiguous", "span": "那个盒子", "matched_order_ids": ["O1003", "O1004"]}]}
        return {
            "list": {"ok": True, "status": "order_found", "reply": "已查询到本人的模拟订单。", "orders": alice,
                     "binding": {"list_scope": "all_owned"}},
            "one": {"ok": True, "status": "order_found", "reply": "已查询到本人的模拟订单。", "orders": [view("O1002")],
                    "binding": {"product_refs": [{"outcome": "unique", "span": "帆布包", "matched_order_ids": ["O1002"]}]}},
            "ineligible": {"ok": True, "status": "ineligible", "reply": "模拟", "order": view("O1003")},
            "card": {"ok": True, "status": "awaiting_confirmation", "reply": "模拟", "order": card, "card": pending,
                     "card_outcome": "created"},
            "card_reshown": {"ok": True, "status": "awaiting_confirmation", "reply": "模拟", "order": card,
                             "card": pending, "card_outcome": "reshown"},
            "existing": {"ok": True, "status": "already_requested", "reply": "模拟",
                         "order": view("O1001", {"id": "RET-O1001", "status": "pending"})},
            "candidates": {"ok": True, "status": "clarification_needed", "reply": "模拟",
                           "missing_fields": ["order_id"], "candidate_orders": alice},
            "query_missing": {"ok": True, "status": "clarification_needed", "reply": "请提供要查询的模拟订单号。",
                              "missing_fields": ["query_order_id"]},
            "voided": {"ok": True, "status": "card_voided", "reply": "模拟", "voided_order_id": "O1001"},
            "ambiguous": {"ok": True, "status": "clarification_needed", "reply": "模拟",
                          "missing_fields": ["order_id"], "binding": ambiguous,
                          "candidate_orders": [view("O1003"), view("O1004")]},
            "rejected": {"ok": False, "status": "blocked", "reply": "业务字段需要来自本轮用户原话。",
                         "error": {"code": "UNSOURCED_FIELD", "message": "业务字段需要来自本轮用户原话。"}},
        }

    def test_rendered_business_text_never_states_the_demo_environment_or_a_platform(self):
        for name, result in self.results().items():
            with self.subTest(result=name):
                lead, lines, note = business_parts(result)
                for text in (lead, note, *lines):
                    self.assertEqual(demo_words(text), {}, text)
                    self.assertFalse([word for word in PLATFORM_NAMES if word in text], text)

    def test_a_business_outcome_is_worded_from_its_fields_never_from_the_model_facing_reply(self):
        for name, result in self.results().items():
            with self.subTest(result=name):
                lead, lines, note = business_parts(result)
                self.assertNotIn(result["reply"], "".join([lead, *lines, note]))
        self.assertEqual(business_parts(self.results()["query_missing"])[1], [code_copy.ASK_WHICH_ORDER])
        self.assertEqual(business_parts(self.results()["rejected"])[1], [code_copy.ITEM_DEGRADED])

    def test_no_rendered_business_text_asks_for_a_return_reason(self):
        # Decision 3: the reason is picked on the card; chat never asks for it. The only sentences that mention the
        # reason point to the card.
        card_ready = code_copy.CARD_READY.format(order_id="O1001", product="学习用保温杯")
        for name, result in self.results().items():
            with self.subTest(result=name):
                text = "".join(business_parts(result)[1:2][0]) + business_parts(result)[2]
                self.assertNotIn("原因", text.replace(card_ready, "").replace(code_copy.APPLY_HINT, ""))

    def test_an_ambiguous_mention_heads_its_candidates_returnable_first(self):
        lines = business_parts(self.results()["ambiguous"])[1]
        self.assertEqual(lines[0], code_copy.CANDIDATES_LEAD)
        self.assertEqual([line.split("：")[0] for line in lines[1:]], ["订单 O1004", "订单 O1003"])

    def test_a_prepared_or_kept_card_states_the_click_rule_and_an_ineligible_order_does_not(self):
        self.assertIn(code_copy.SUBMIT_BY_CLICK, business_parts(self.results()["card"])[1][0])
        self.assertEqual(business_parts(self.results()["card_reshown"])[1], [code_copy.CARD_KEPT.format(order_id="O1001")])
        ineligible = "".join(business_parts(self.results()["ineligible"])[1])
        self.assertTrue(ineligible.startswith("订单 O1003：学习用饼干礼盒"))
        self.assertIn(code_copy.NOT_RETURNABLE["food_opened"], ineligible)
        self.assertNotIn(code_copy.SUBMIT_BY_CLICK, ineligible)


class FixedSentenceTests(unittest.TestCase):
    def test_the_failure_sentences_promise_no_person_and_state_no_environment(self):
        for text in (FAILED_TURN_TEXT, RETRY_STEP, TECHNICAL_FAILURE_LEAD):
            with self.subTest(text=text):
                self.assertEqual(demo_words(text), {})
                self.assertIsNone(FOLLOW_UP_PROMISES.search(text))
                self.assertNotIn("真人", text)

    def test_the_identity_boundary_names_no_owner_and_states_no_environment(self):
        reply = code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="O2001、O2002")
        self.assertEqual(reply, "当前登录的账号里没有订单 O2001、O2002。我这里只能查看和办理这个账号名下的订单。")
        self.assertEqual(demo_words(reply), {})

    def test_every_code_sentence_and_catalogue_entry_of_a_reply_is_in_production_voice(self):
        texts = [text for name, text in code_copy.TEXTS.items() if name != "ENVIRONMENT_NOTICE"]
        texts += list(code_copy.PATHS.values())
        texts += [option for entry in code_copy.OPTION_SETS.values() for option in entry["options"]]
        for text in texts:
            with self.subTest(text=text[:20]):
                self.assertEqual(demo_words(text), {})
                self.assertFalse([word for word in PLATFORM_NAMES if word in text])
                self.assertIsNone(FOLLOW_UP_PROMISES.search(text))


class EnvironmentNoticeTests(unittest.TestCase):
    def setUp(self):
        self.chat = (APP / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")
        self.page = (APP / "support_agent" / "web" / "chat.html").read_text(encoding="utf-8")

    def test_the_notice_states_the_assistant_the_demo_data_no_follow_up_and_the_business_date(self):
        for fact in ("自动回复的售后助手", "模拟数据", "不会发生真实交易", "不会有真人跟进或联系你",
                     "演示业务日期是 " + SCENARIO_DATE.isoformat(), "生效日期未核实"):
            with self.subTest(fact=fact):
                self.assertEqual(ENVIRONMENT_NOTICE.count(fact), 1)

    def test_the_chat_page_shows_the_server_notice_in_one_persistent_element(self):
        self.assertEqual(self.chat.count("h(\"p.env-notice\""), 1)
        self.assertEqual(self.chat.count("els.envNotice.textContent = state.ordersMeta?.notice"), 1)
        self.assertIn("state.demo.notice", self.chat)   # the sign-in page shows the same server text

    def test_no_other_chat_copy_states_the_environment_or_names_the_framework(self):
        allowed = {"当前服务未开启演示登录",                                  # operator setup message
                   "请用 app 命令启动演示，或粘贴 service init 生成的本地凭据。",
                   "离线演示：「模型调用」由本地关键词脚本模拟，不调用 API，也不计 tokens。"}   # process drawer
        literals = set(re.findall(r'"([^"\n]*)"', self.chat)) | set(re.findall(r"`([^`\n]*)`", self.chat))
        flagged = {text for text in literals if demo_words(text)}
        self.assertEqual(flagged, allowed)
        self.assertEqual(demo_words(self.page), {})
        for source in (self.chat, self.page):
            self.assertNotIn("LangGraph", source)

    def test_the_run_mode_appears_only_in_the_process_drawer(self):
        calls = [match.start() for match in re.finditer(r"modeBadge\(", self.chat)]
        self.assertEqual(len(calls), 1)
        drawer = self.chat.index("async function openTrace")
        self.assertGreater(calls[0], drawer)
        self.assertNotIn("MODE_LABELS", self.chat)

    def test_order_cards_show_the_server_verdict_and_no_rule_note(self):
        self.assertNotIn("teaching_rule", self.chat)
        self.assertIn("full.eligibility_text", self.chat)

    def test_the_confirm_card_shows_its_expiry_as_a_time_of_day_only(self):
        # D13: the page states the demo business date; a wall-clock date on the card would contradict it.
        self.assertIn("clock(card.expires_at, { timeOnly: true })", self.chat)


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class ModelVisibleTextTests(unittest.TestCase):
    def test_the_planning_prompt_and_its_trust_note_are_b7s_in_production_voice(self):
        from support_agent.trust import TRUST_NOTE
        from support_agent.understand import PROMPT
        values = {"PLANNING_PROMPT": PROMPT, "PLANNING_TRUST_NOTE": TRUST_NOTE}
        self.assertEqual({name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in values.items()},
                         MODEL_VISIBLE_DIGESTS)
        for name, text in values.items():
            with self.subTest(prompt=name):
                self.assertEqual(demo_words(text), {})
                self.assertFalse([word for word in PLATFORM_NAMES if word in text])
                self.assertNotIn("本地", text)
                self.assertNotIn("simulated", text)
        self.assertLessEqual(len(PROMPT), 3000)   # plan §8.6 B7

    def test_the_answer_prompts_are_in_production_voice(self):
        from support_agent.respond import RESPOND_PROMPT, TRUST_NOTE
        for name, text in (("respond", RESPOND_PROMPT), ("trust", TRUST_NOTE)):
            with self.subTest(prompt=name):
                self.assertEqual(demo_words(text.replace("不要提演示或模拟", "")), {})
                self.assertFalse([word for word in PLATFORM_NAMES if word in text])
                self.assertNotIn("本地", text)
        self.assertLessEqual(len(RESPOND_PROMPT), 1500)   # plan §5.2: about 1,500 characters


if __name__ == "__main__":
    unittest.main()
