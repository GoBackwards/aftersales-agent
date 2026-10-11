"""Output guards (B5d1, plan §5.4; B5f: the structure check splits schema problems from what is only untidy).

The T10 corpora are the guards' specification (labels plus rule notes RN1–RN16); every item outside the recall
probes must get exactly its labels, and the probes are reported apart (RN15). The other checks use newly written
sentences. These are mechanical checks of the guard rules, not answer quality or task success.
"""
import ast
import json
from pathlib import Path
import unittest

from support_agent import guards
from support_agent.guards import check, check_structure, context, guard_ids

APP = Path(__file__).resolve().parents[1]
DATA = APP / "tests" / "data"
CORPORA = (DATA / "guard_corpus_v1.json", DATA / "guard_redteam_v1.json")
# Recall probes the lexical guards do not reach (RN15 reports them apart); a new miss or a fixed one shows here.
KNOWN_PROBE_MISSES = {"G1R-31": "条件句预设了一条并不存在的取消记录（隐含完成态），没有完成态动词可探测"}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def corpus_context(corpus, item):
    fx = {**corpus["defaults"], **item["fixture"]}
    cards = corpus["cards"]
    return context(own_order_ids=fx["own_order_ids"], order_facts=fx["order_facts"], events=fx["events"],
                   card_state=fx["card_state"],
                   selected_cards=[{**cards[card], "id": card} for card in fx["selected_card_ids"]],
                   catalogue_cards=[cards[card] for card in fx["catalogue_card_ids"]], paths=fx["paths"],
                   capabilities=fx["capabilities"], answer_model_called=fx.get("answer_model_called", True))


def labels(text, ctx):
    return set(guard_ids(check(text, ctx)))


# A small invented turn: two own orders, a selected card, a path with a phone number, a rule name.
CARD = {"id": "c_swap", "title": "换新服务说明", "aliases": ["换新无忧"], "source_kind": "platform",
        "scope": "签收后 10 天内的家电", "conditions": ["包装齐全"], "outcomes": ["可以申请换新一次"],
        "outcome_premises": [["签收后 10 天内的家电；包装齐全"]], "exceptions": []}
LAW_CARD = {"id": "c_law", "title": "条文摘录", "aliases": [], "source_kind": "law", "scope": "摘录",
            "conditions": ["摘录"], "outcomes": ["摘录"], "exceptions": []}
PATHS = {"merchant_line": {"label": "商家热线", "phone": "400-123-4567"}, "help_page": {"label": "帮助页",
                                                                                 "url": "help.shop.example/swap"}}
FACTS = {"O3101": {"product": "电热水壶", "delivered_on": "10月1日", "paid": "88元"},
         "O3102": {"product": "台灯", "status": "in_transit"}}


def turn(**changes):
    options = {"own_order_ids": ["O3101", "O3102"], "order_facts": FACTS, "selected_cards": [CARD],
               "catalogue_cards": [CARD], "paths": PATHS}
    options.update(changes)
    return context(**options)


class CorpusTests(unittest.TestCase):
    def items(self):
        for path in CORPORA:
            corpus = load(path)
            for item in corpus["items"]:
                yield corpus, item

    def test_every_corpus_item_outside_the_recall_probes_gets_exactly_its_labels(self):
        checked = 0
        for corpus, item in self.items():
            if "recall_probe" in item["tags"]:
                continue
            checked += 1
            with self.subTest(item=item["id"]):
                self.assertEqual(labels(item["text"], corpus_context(corpus, item)), set(item["expected"]) - {"clean"})
        self.assertEqual(checked, 197)

    def test_recall_probes_miss_only_the_registered_items_and_never_get_a_foreign_label(self):
        missed, probes = set(), 0
        for corpus, item in self.items():
            if "recall_probe" not in item["tags"]:
                continue
            probes += 1
            got, want = labels(item["text"], corpus_context(corpus, item)), set(item["expected"]) - {"clean"}
            with self.subTest(item=item["id"]):
                self.assertLessEqual(got, want)
            if got != want:
                missed.add(item["id"])
        self.assertEqual(probes, 26)
        self.assertEqual(missed, set(KNOWN_PROBE_MISSES))

    def test_the_rule_name_lexicon_comes_from_the_catalogue_titles_and_aliases(self):
        for path in CORPORA:
            corpus = load(path)
            names = guards.rule_names([corpus["cards"][card] for card in corpus["defaults"]["catalogue_card_ids"]])
            with self.subTest(corpus=path.name):
                self.assertLessEqual(set(corpus["defaults"]["rule_names"]), names)
                self.assertEqual(names, {name for card in corpus["cards"].values()
                                         if card in [corpus["cards"][c] for c in corpus["defaults"]["catalogue_card_ids"]]
                                         for name in [card["title"], *card["aliases"]]})

    def test_the_rule_notes_that_narrow_the_shared_rules_are_recorded_with_the_corpus(self):
        notes = {note["id"]: note for note in load(CORPORA[0])["rule_notes"]}
        self.assertEqual(sorted(notes), sorted(f"RN{number}" for number in range(1, 17)))
        for number in (2, 3, 4):   # RN1 states the kept mechanism; RN2–RN4 change §5.4 and wait for the owner
            with self.subTest(note=number):
                self.assertNotEqual(notes[f"RN{number}"]["spec_change"].strip(), "")
                self.assertFalse(notes[f"RN{number}"]["spec_change"].startswith("无"))


class SharedRuleTests(unittest.TestCase):
    def test_half_width_punctuation_cuts_clauses_like_full_width_punctuation(self):
        self.assertEqual(guards.clauses("甲,乙;丙?丁!戊. 己"), ["甲", "乙", "丙", "丁", "戊", " 己"])
        self.assertEqual(guards.clauses("见 help.shop.example/swap 页面"), ["见 help.shop.example/swap 页面"])
        # The negation of the first clause cannot reach the claim after a half-width comma (RN7, RN8).
        self.assertEqual(labels("我还没看到截图,不过换新已经办好了", turn()), {"G1"})

    def test_only_quotes_attributed_to_the_user_a_page_or_a_card_are_left_out(self):
        self.assertEqual(labels("你提到短信上写着“退款已到账”，这条我这边看不到。", turn()), set())
        self.assertEqual(labels("卡片的标题是“换新无忧”，以原文为准。", turn()), set())
        self.assertEqual(labels("这单的进度是“已退款”。", turn()), {"G1"})
        self.assertEqual(labels("这单可以用“换新无忧”。", turn()), {"G3"})
        # G6 reads quotes as written.
        self.assertEqual(labels("你说的“O3999”不在这个账号下。", turn()), {"G6"})

    def test_the_question_exemption_covers_only_what_follows_a_marker(self):
        self.assertEqual(labels("你是否想了解换新无忧的入口？", turn()), set())
        self.assertEqual(labels("换新无忧你是否了解？", turn()), {"G3"})
        self.assertEqual(labels("你知道这台壶可以退货吗？", turn()), {"G3"})        # 吗 is not a marker
        self.assertEqual(labels("这台壶能不能退货，要看卡片前提。", turn()), {"G3"})   # nor is 能不能
        # No question exemption for G6, G7 and G8 (RN2).
        self.assertEqual(labels("你是否方便把登录密码发给我？", turn()), {"G7"})
        self.assertEqual(labels("你是否愿意说说想退的原因？", turn()), {"G8"})

    def test_negation_counts_only_directly_before_the_verb(self):
        self.assertEqual(labels("我没有替你递交这份申请。", turn()), set())
        self.assertEqual(labels("我没法替你给商家打电话。", turn()), set())
        self.assertEqual(labels("我这边没法直接退款但已经替你登记了。", turn()), {"G1"})
        self.assertEqual(labels("不用你再催商家也会尽快处理。", turn()), {"G2"})
        self.assertEqual(labels("这台壶不支持退货。", turn()), {"G3"})              # G3 has no negation exemption
        self.assertEqual(labels("我没法承诺什么时候会有人联系你。", turn()), set())

    def test_a_condition_exempts_nothing(self):
        self.assertEqual(labels("如果你今天点了确认，商家会尽快联系你。", turn()), {"G2"})
        self.assertEqual(labels("要是包装齐全，就可以退货。", turn()), {"G3"})
        self.assertEqual(labels("如果你同意，我会替你给仓库留言。", turn()), {"G1"})


class GuardTests(unittest.TestCase):
    def test_g1_needs_a_record_of_the_same_type_and_order(self):
        submitted = [{"scope": "turn", "type": "return_request_submitted", "order_id": "O3101"}]
        self.assertEqual(labels("O3101 这单的申请已经提交好了。", turn(events=submitted)), set())
        self.assertEqual(labels("O3102 这单的申请已经提交好了。", turn(events=submitted)), {"G1"})
        self.assertEqual(labels("O3101 这单的申请已经提交好了。", turn()), {"G1"})
        earlier = [{"scope": "session", "type": "ticket_created", "ticket": "H-0101"},
                   {"scope": "turn", "type": "ticket_event_appended", "ticket": "H-0101"}]
        self.assertEqual(labels("这次的情况已补充到工单。", turn(events=earlier)), set())
        self.assertEqual(labels("已为你新建了一张人工工单。", turn(events=earlier)), {"G1"})   # new = this turn
        self.assertEqual(labels("那笔货款眼下已到账。", turn(events=submitted)), {"G1"})       # nothing records refunds

    def test_g1_reads_every_completion_form_the_plan_lists(self):
        # §5.4 G1: 已转 on its own, an open 已为你/帮你/替你 with any verb, the old performed-action probe (已为您完成)
        # and the paid-result probe of trust (成功退款、退款完成 ...) all need a record; nothing records a refund.
        submitted = [{"scope": "turn", "type": "return_request_submitted", "order_id": "O3101"}]
        for text in ("这件事已转仓库跟进。", "已为你核实过包裹重量。", "已帮你反馈到售后组。", "已替你预约了上门时间。",
                     "我已为您完成换货。", "台灯那笔退款完成。", "货款这边已经成功退回。", "钱已成功到账。"):
            with self.subTest(text=text):
                self.assertEqual(labels(text, turn(events=submitted)), {"G1"})
        # The same forms pass with their record: a ticket for 已转人工, the card on this turn for 已帮你调出.
        ticket = [{"scope": "turn", "type": "ticket_created", "ticket": "H-0107"}]
        self.assertEqual(labels("这件事已转人工专员。", turn(events=ticket)), set())
        card = turn(card_state="shown")
        self.assertEqual(labels("已帮你把 O3101 的确认卡调出来。", card), set())
        self.assertEqual(labels("已帮你把 O3101 的确认卡调出来。", turn()), {"G1"})

    def test_g1_states_need_their_record(self):
        withdrawn = [{"scope": "session", "type": "ticket_created", "ticket": "H-0102"},
                     {"scope": "turn", "type": "ticket_withdrawn", "ticket": "H-0102"}]
        self.assertEqual(labels("工单 H-0102 还在处理中。", turn(events=withdrawn)), {"G1"})
        self.assertEqual(labels("确认卡仍然有效。", turn()), {"G1"})
        self.assertEqual(labels("确认卡仍然有效。", turn(card_state="kept")), set())
        self.assertEqual(labels("商家那边回复说同意换新。", turn()), {"G1"})

    def test_g1_reports_of_the_user_or_their_paste_are_theirs(self):
        self.assertEqual(labels("你说客服告诉你已经退回了。", turn()), set())
        self.assertEqual(labels("这段截图里写着已经退款，我这边不能当成结果。", turn()), set())
        self.assertEqual(labels("从你发的截图看，退款已经通过了。", turn()), {"G1"})
        self.assertEqual(labels("那条申请你自己撤回了吗？", turn()), set())

    def test_g1_offers_pass_only_for_what_the_system_can_do(self):
        self.assertEqual(labels("要不要我把 O3101 的确认卡调出来？", turn()), set())
        self.assertEqual(labels("要不要我帮你改一下收货地址？", turn()), {"G1"})
        self.assertEqual(labels("我可以帮你建一张人工工单。", turn()), set())
        self.assertEqual(labels("我可以帮你建一张人工工单。", turn(capabilities=["show_return_card"])), {"G1"})

    def test_g2_applies_when_someone_else_would_act_not_to_the_users_own_pace(self):
        self.assertEqual(labels("稍后会有同事回电。", turn()), {"G2"})
        self.assertEqual(labels("你可以稍后再来看进度。", turn()), set())
        self.assertEqual(labels("如果你想尽快处理，点上方的按钮就行。", turn()), set())
        self.assertEqual(labels("现在排队的人比较多。", turn()), {"G2"})
        self.assertEqual(labels("这里不会有人主动回电。", turn()), set())

    def test_g3_rule_names_pass_only_inside_the_gap_sentence(self):
        self.assertEqual(labels("我这边的资料没写到换新无忧在你这单上怎么用。", turn()), set())
        self.assertEqual(labels("你这台壶符合换新无忧。", turn()), {"G3"})
        self.assertEqual(labels("换新服务说明里的条件请看上方原文。", turn()), {"G3"})   # titles count as names

    def test_g4_numbers_dates_addresses_and_phones_need_a_source_that_is_not_the_user(self):
        self.assertEqual(labels("签收日期 10月1日，实付金额 88元。", turn()), set())
        self.assertEqual(labels("可以拨打 400-123-4567，或打开 help.shop.example/swap。", turn()), set())
        self.assertEqual(labels("这单是 10月3日签收的。", turn()), {"G4"})
        self.assertEqual(labels("也可以拨打 021-555-0188。", turn()), {"G4"})
        self.assertEqual(labels("可以看 www.other-help.test/page。", turn()), {"G4"})
        self.assertEqual(labels("这个月还能换两次。", turn()), {"G4"})                  # Chinese numerals count (RN9)
        self.assertEqual(labels("签收后 10 天内的家电写在卡片里。", turn()), set())
        self.assertEqual(labels("签收后 10 天内的家电写在卡片里。", turn(selected_cards=[])), {"G4"})

    def test_g5_law_wording_needs_a_selected_law_card(self):
        self.assertEqual(labels("这件事不涉及法律。", turn()), {"G5"})
        self.assertEqual(labels("这件事不涉及法律。", turn(selected_cards=[LAW_CARD])), set())
        self.assertEqual(labels("按你的说法，我这边没法核对，可以换个方法联系商家。", turn()), set())

    def test_g6_flags_any_order_this_identity_does_not_own(self):
        self.assertEqual(labels("O3999 这一单我这边看不到。", turn()), {"G6"})
        self.assertEqual(labels("O3101、O3102 两单都归你，详情见上方。", turn()), set())
        self.assertEqual(labels("型号 TO3999 写在包装上。", turn()), set())

    def test_g7_flags_credential_requests_and_long_numbers_whatever_their_source(self):
        self.assertEqual(labels("请把手机收到的验证码发给我。", turn()), {"G7"})
        self.assertEqual(labels("我不会向你要密码，也请不要告诉任何人。", turn()), set())
        self.assertEqual(labels("这里不需要你提供卡号。", turn()), set())
        long_number = {"O3101": {"tracking": "SF1234567890123"}}
        self.assertEqual(labels("单号 SF1234567890123 可以在快递官网查。", turn(order_facts=long_number)), {"G7"})

    def test_g8_flags_reason_questions_anywhere_and_submitted_hints_on_card_turns(self):
        self.assertEqual(labels("能讲一下想退这台壶的原因吗？", turn()), {"G8"})
        self.assertEqual(labels("原因在卡片上点选就行。", turn(card_state="shown")), set())
        card = turn(card_state="shown")
        self.assertEqual(labels("接下来等商家审核就好。", card), {"G8"})
        self.assertEqual(labels("接下来等商家审核就好。", turn()), set())
        self.assertEqual(labels("申请也一并替你交了。", card), {"G1", "G8"})

    def test_everyday_sentences_around_the_checked_words_stay_clean(self):
        for text in ("你联系过商家吗？", "我能理解你想联系商家。", "请不要把验证码发给我。", "我没有办法替你联系商家。",
                     "我可以帮你取消这张确认卡。", "包裹已经到了驿站。", "退货、换货的办理方式都列出来了。"):
            with self.subTest(text=text):
                self.assertEqual(labels(text, turn()), set())

    def test_the_promise_the_wired_scenario_scripts_is_one_the_guards_stop(self):
        scenario = load(APP / "tests" / "golden" / "g22-guards-wired-once-per-turn.json")
        steps = {step["id"]: step for step in scenario["steps"]}
        promise = steps["a-promise-is-dropped-alone-and-the-path-stays"]["model"][1]["answer"]["note"]
        clean = steps["words-without-a-hit-are-shown-as-the-models"]["model"][1]["answer"]["note"]
        self.assertEqual(labels(promise, context(own_order_ids=["O1001"])), {"G2"})
        self.assertEqual(labels(clean, context(own_order_ids=["O1001"])), set())

    def test_nothing_is_checked_when_no_answer_was_written(self):
        self.assertEqual(check("已为你提交退货。", turn(answer_model_called=False)), [])
        self.assertEqual(check("   ", turn()), [])
        with self.assertRaises(ValueError):
            check(None, turn())

    def test_a0_words_are_counted_and_never_block(self):
        self.assertEqual(guards.a0_counts("这是演示，也是演示环境里的模拟订单"), {"演示": 2, "模拟": 1})
        self.assertEqual(labels("这是演示环境。", turn()), set())


class StructureTests(unittest.TestCase):
    """B5f: check_structure returns (problems, tidied answer, dropped). Only a schema break is a problem (it goes back
    once); what is only untidy is removed and listed, never failing the turn."""
    CARDS = {"c_swap": {**CARD}, "c_fee": {"id": "c_fee", "effect_domain": "return_shipping_responsibility",
                                         "scope": "退回运费", "conditions": ["个人原因"], "outcomes": ["甲", "乙"],
                                         "outcome_premises": [["个人原因"], ["质量原因"]]},
             "c_bare": {"id": "c_bare", "scope": "范围", "conditions": [], "outcomes": ["甲", "乙"]},
             "c_many": {"id": "c_many", "scope": "范围", "conditions": ["条件"], "outcomes": list("丙丁戊己庚辛"),
                        "outcome_premises": [["前提"]] * 6}}

    def ctx(self, **changes):
        options = {"items": [{"index": 0, "fee_subject": "return_shipping"}, {"index": 1, "fee_subject": "unknown"}],
                   "cards": self.CARDS, "path_ids": ["merchant_line"],
                   "option_sets": {"which_order": {"kind": "order"}, "why_return": {"kind": "return_reason"}},
                   "own_order_ids": ["O3101"], "identifiers": ["c_swap", "c_fee", "merchant_line", "which_order"]}
        options.update(changes)
        return options

    def answer(self, **changes):
        value = {"items": [{"index": 0, "disposition": "answered", "claims": [{"card_id": "c_fee", "outcome_index": 1}],
                            "path_id": "none", "note": ""},
                           {"index": 1, "disposition": "gap", "claims": [], "path_id": "merchant_line",
                            "note": "包装要求这边的资料没写到。"}],
                 "show_order_ids": ["O3101"], "next_step": "能确认的都在上方。", "ask": {"text": "", "option_set_id": "none"}}
        value.update(changes)
        return value

    def codes(self, answer, ctx=None):
        return [hit["code"] for hit in check_structure(answer, ctx or self.ctx())[0]]

    def dropped(self, answer, ctx=None):
        problems, _, dropped = check_structure(answer, ctx or self.ctx())
        self.assertEqual(problems, [])
        return [(row["code"], row["part"]) for row in dropped]

    def test_a_well_formed_answer_passes_untouched(self):
        problems, tidied, dropped = check_structure(self.answer(), self.ctx())
        self.assertEqual((problems, dropped, tidied), ([], [], self.answer()))

    def test_ids_outside_the_turns_lists_and_wrong_shapes_are_problems(self):
        bad = self.answer(items=[{"index": 0, "disposition": "answered", "path_id": "none", "note": "",
                                  "claims": [{"card_id": "c_other", "outcome_index": 0},
                                             {"card_id": "c_fee", "outcome_index": 5},
                                             {"card_id": "c_bare", "outcome_index": 0}]},
                                 {"index": 7, "disposition": "maybe", "claims": [], "path_id": "nowhere", "note": ""}])
        self.assertEqual(sorted(self.codes(bad)), ["G9_CLAIM_NOT_A_CANDIDATE", "G9_CLAIM_NOT_A_CANDIDATE",
                                                   "G9_CLAIM_WITHOUT_PREMISES", "G9_DISPOSITION_UNKNOWN",
                                                   "G9_ITEM_UNKNOWN", "G9_PATH_UNKNOWN"])
        self.assertEqual(self.codes(self.answer(show_order_ids=["O3999"])), ["G9_ORDER_NOT_OWN"])
        self.assertEqual(self.codes(self.answer(ask={"text": "哪一单？", "option_set_id": "no_such_set"})),
                         ["G9_OPTION_SET_UNKNOWN"])
        self.assertEqual(self.codes(self.answer(next_step=None)), ["G9_NEXT_STEP_SHAPE"])
        self.assertEqual(self.codes({"prose": ""}), ["G9_SHAPE"])
        no_note = self.answer(items=[{key: value for key, value in row.items() if key != "note"}
                                     for row in self.answer()["items"]])
        self.assertEqual(self.codes(no_note), ["G9_SHAPE"])

    def test_a_fee_card_of_another_service_is_dropped_only_when_both_are_known(self):
        repair = self.ctx(items=[{"index": 0, "fee_subject": "repair_shipping"}, {"index": 1, "fee_subject": "none"}])
        self.assertEqual(self.dropped(self.answer(), repair), [("G9_FEE_SUBJECT_MISMATCH", "claim:0")])
        self.assertEqual(check_structure(self.answer(), repair)[1]["items"][0]["claims"], [])
        unknown = self.ctx(items=[{"index": 0, "fee_subject": "unknown"}, {"index": 1, "fee_subject": "none"}])
        self.assertEqual(self.dropped(self.answer(), unknown), [])

    def test_a_repeated_row_is_dropped_a_missing_one_recorded_and_the_ask_alone_decides(self):
        first, second = self.answer()["items"]
        self.assertEqual(self.dropped(self.answer(items=[first])), [("G9_ITEM_MISSING", "item:1")])
        repeated = self.answer(items=[first, second, {**second, "note": "", "disposition": "routed"}])
        self.assertEqual(self.dropped(repeated), [("G9_ITEM_REPEATED", "item:1")])
        self.assertEqual(check_structure(repeated, self.ctx())[1]["items"], [first, second])
        # B5e: an item answered with a card may still come with one follow-up.
        asking = self.answer(items=[first, {**second, "disposition": "answered", "note": ""}],
                             ask={"text": "你说的是哪一单？", "option_set_id": "which_order"})
        self.assertEqual(self.dropped(asking), [])

    def test_an_outcome_may_answer_two_items_and_a_repeat_within_one_is_dropped(self):
        claim = {"card_id": "c_fee", "outcome_index": 1}
        shared = self.answer(items=[{"index": 0, "disposition": "answered", "claims": [claim], "path_id": "none",
                                     "note": ""},
                                    {"index": 1, "disposition": "answered", "claims": [claim], "path_id": "none",
                                     "note": ""}])
        self.assertEqual(self.dropped(shared), [])
        twice = self.answer(items=[{"index": 0, "disposition": "answered", "claims": [claim, claim], "path_id": "none",
                                    "note": ""}, self.answer()["items"][1]])
        self.assertEqual(self.dropped(twice), [("G9_CLAIM_REPEATED", "claim:0")])
        self.assertEqual(check_structure(twice, self.ctx())[1]["items"][0]["claims"], [claim])

    def test_claims_past_the_limits_are_dropped_in_item_order(self):
        many = [{"card_id": "c_many", "outcome_index": index} for index in range(6)]
        rows = [{"index": 0, "disposition": "answered", "claims": many[:4], "path_id": "none", "note": ""},
                {"index": 1, "disposition": "answered", "claims": many[:4], "path_id": "none", "note": ""}]
        ctx = self.ctx(items=[{"index": 0, "fee_subject": "none"}, {"index": 1, "fee_subject": "none"}])
        problems, tidied, dropped = check_structure(self.answer(items=rows), ctx)
        self.assertEqual([len(row["claims"]) for row in tidied["items"]], [guards.CLAIMS_PER_ITEM] * 2)
        self.assertEqual([(row["code"], row["part"]) for row in dropped],
                         [("G9_CLAIMS_PER_ITEM", "claim:0"), ("G9_CLAIMS_PER_ITEM", "claim:1")])
        rows.append({"index": 2, "disposition": "answered", "claims": many[4:5], "path_id": "none", "note": ""})
        ctx["items"].append({"index": 2, "fee_subject": "none"})
        self.assertIn(("G9_CLAIMS_PER_ANSWER", "claim:2"), self.dropped(self.answer(items=rows), ctx))

    def test_a_note_only_where_a_card_cannot_answer_short_and_without_ids(self):
        first, second = self.answer()["items"]
        noted = self.answer(items=[{**first, "note": "卡片上写得很清楚。"}, second])
        self.assertEqual(self.dropped(noted), [("G9_NOTE_NOT_ALLOWED", "note:0")])
        for words in ("可以对照[[c_swap]]看看。", "参考 c_fee 那一条。", "长" * (guards.NOTE_LIMIT + 1)):
            with self.subTest(words=words[:12]):
                code = "G9_TOO_LONG" if words.startswith("长") else "G9_ID_IN_TEXT"
                self.assertEqual(self.dropped(self.answer(items=[first, {**second, "note": words}])),
                                 [(code, "note:1")])
                self.assertEqual(self.dropped(self.answer(next_step=words)), [(code, "next_step")])
        tidied = check_structure(self.answer(items=[first, {**second, "note": "长" * 101}]), self.ctx())[1]
        self.assertEqual(tidied["items"][1]["note"], "")

    def test_a_reason_set_drops_the_ask_and_what_is_never_shown_is_left_alone(self):
        reason = self.answer(ask={"text": "选一个原因", "option_set_id": "why_return"})
        self.assertEqual(self.dropped(reason), [("G8_REASON_OPTION_SET", "ask")])
        self.assertEqual(check_structure(reason, self.ctx())[1]["ask"], {"text": "", "option_set_id": "none"})
        # An ask without words is never shown, and a repeated order id is shown once anyway (assemble).
        self.assertEqual(self.dropped(self.answer(ask={"text": " ", "option_set_id": "which_order"})), [])
        self.assertEqual(self.dropped(self.answer(show_order_ids=["O3101", "O3101"])), [])


class ModuleBoundaryTests(unittest.TestCase):
    def test_guards_has_no_more_than_46_regular_expressions(self):
        tree = ast.parse((APP / "support_agent" / "guards.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"]
        self.assertEqual({call.func.attr for call in calls}, {"compile"})   # every pattern is a counted compile
        # B5f: the one compile of a catalogue lexicon serves two lexicons (G3's rule names, G10's publishers): count both.
        self.assertLessEqual(len(calls) + 1, guards.MAX_PATTERNS)
        self.assertEqual(guards.MAX_PATTERNS, 46)

    def test_only_the_answer_step_reads_the_guards_and_the_copy_has_its_few_readers(self):
        # B5d2 wires them: the guards run once per turn in the answer step (turn) and respond reads their item
        # dispositions and limits (B5f: the assembler no longer reads a card placeholder); the code sentences are read
        # by the assembler and the modules that state a record.
        readers = {"guards": set(), "copy": set()}
        for path in sorted((APP / "support_agent").glob("*.py")):
            if path.stem in ("guards", "copy"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.level or node.module == "support_agent"):
                    names.add(node.module or "")                        # from .guards import ..., from . import x
                    names.update(alias.name for alias in node.names if not node.module or node.module == "support_agent")
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("support_agent."):
                    names.add(node.module.split(".", 1)[1])
                elif isinstance(node, ast.Import):
                    names.update(alias.name.split(".", 1)[1] for alias in node.names
                                 if alias.name.startswith("support_agent."))
            for module in readers:
                if module in names:
                    readers[module].add(path.stem)
        self.assertEqual(readers["guards"], {"turn", "respond"})
        self.assertEqual(readers["copy"], {"assemble", "turn", "respond", "return_cards", "tickets"})




if __name__ == "__main__":
    unittest.main()
