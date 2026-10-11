"""Guard precision and record claims after the paid diagnosis (B5e, guards-v2; plan §5.4, §9).

What changed, as behaviour: a negated verb of saying, concluding or committing governs the rest of its clause; a V了
phrase that is the subject of a negated relation states no fact; a page's state is the page's; what the words say
about an order or a return request found or absent is checked against the prefetched facts and events, while a
record this system does not hold is never the reply's to state; the user's paste is never called a result; an offer
is judged by its first action word against the system's action catalogue. B5e-r2 (after the verifier's report): only
verbs of saying, concluding or committing govern, the record is the last record noun of the clause, a bare 有 states
an external record, "你说的X" names X and the user's words are theirs only in quotes (RN23). B5e-r3 (after the
verifier's second report): the governing reaches only G1's claims and G2, never offers or credential asks, and ends at
也 and 的话 too; 不需要说 affirms; a page of this system's side and an "it" without antecedent are not the user's; a lookup
of a record this system does not hold is an offer outside the catalogue. The B5e corpus (tests/data/guard_corpus_b5e.json) is the specification, its recall probes reported apart (RN15); the other sentences here are
written for these tests. Mechanical checks of the guard rules, not answer quality or task success.
"""
import json
from pathlib import Path
import unittest

from support_agent import guards, respond
from support_agent.guards import check, context, guard_ids

DATA = Path(__file__).resolve().parent / "data"
CORPUS = DATA / "guard_corpus_b5e.json"
FORMS = {"affirmative", "negative", "question", "conditional", "mixed"}
# Recall probes the lexical guards do not reach (RN15 reports them apart; RN21: a placeholder with a judgement of
# whether its card applies, and a bare "can return" with no rule word, are left to the human binary review).
KNOWN_PROBE_MISSES = {"B5E-49": "占位符加适用判断，去掉占位符后没有规则名", "B5E-50": "占位符加办理方式，同上",
                      "B5E-51": "不带退货类词的能否结论（v1 起就漏），只能靠人工二分类"}
EVENT_TYPES = {"return_request_submitted", "ticket_created", "ticket_event_appended", "ticket_withdrawn",
               "card_voided", "order_not_in_account", "orders_shown"}
OWN = ["O6101", "O6102"]


def load():
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def corpus_context(corpus, item):
    # B5f deleted the card placeholders: the corpus's card_ref_ids no longer reach the guards, which now read a
    # placeholder as written; the placeholder items (B5E-11, -13, -49, -50) get the same labels, and in a reply such a
    # piece is dropped earlier, by the structure check (G9_ID_IN_TEXT).
    fx = {**corpus["defaults"], **item["fixture"]}
    cards = corpus["cards"]
    return context(own_order_ids=fx["own_order_ids"], order_facts=fx["order_facts"], events=fx["events"],
                   card_state=fx["card_state"], selected_cards=[{**cards[card], "id": card} for card in fx["selected_card_ids"]],
                   catalogue_cards=[cards[card] for card in fx["catalogue_card_ids"]], paths=fx["paths"],
                   capabilities=fx["capabilities"])


def codes(text, **changes):
    options = {"own_order_ids": OWN}
    options.update(changes)
    return [hit["code"] for hit in check(text, context(**options))]


class CorpusTests(unittest.TestCase):
    def test_every_item_of_the_b5e_corpus_outside_the_recall_probes_gets_exactly_its_labels(self):
        corpus = load()
        checked = 0
        for item in corpus["items"]:
            if "recall_probe" in item["tags"]:
                continue
            checked += 1
            with self.subTest(item=item["id"]):
                got = set(guard_ids(check(item["text"], corpus_context(corpus, item))))
                self.assertEqual(got, set(item["expected"]) - {"clean"})
        self.assertEqual(checked, 62)

    def test_recall_probes_miss_only_the_registered_items_and_never_get_a_foreign_label(self):
        corpus = load()
        missed, probes = set(), 0
        for item in corpus["items"]:
            if "recall_probe" not in item["tags"]:
                continue
            probes += 1
            got = set(guard_ids(check(item["text"], corpus_context(corpus, item))))
            want = set(item["expected"]) - {"clean"}
            with self.subTest(item=item["id"]):
                self.assertLessEqual(got, want)
            if got != want:
                missed.add(item["id"])
        self.assertEqual(probes, 3)
        self.assertEqual(missed, set(KNOWN_PROBE_MISSES))

    def test_the_corpus_is_well_formed_and_covers_both_sides(self):
        corpus = load()
        items = corpus["items"]
        self.assertEqual(len({item["id"] for item in items}), len(items))
        notes = {note["id"] for note in corpus["rule_notes"]}
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertEqual(set(item), {"id", "text", "form", "expected", "tags", "rule_notes", "fixture"})
                self.assertIn(item["form"], FORMS)
                self.assertLessEqual(set(item["expected"]), set(corpus["label_values"]))
                self.assertLessEqual(set(item["tags"]), set(corpus["tag_values"]))
                self.assertLessEqual(set(item["rule_notes"]), notes)
                self.assertLessEqual(set(item["fixture"]), set(corpus["fixture_schema"]) - {"merge"})
                for event in item["fixture"].get("events", []):
                    self.assertIn(event["type"], EVENT_TYPES)
        clean = [item for item in items if item["expected"] == ["clean"]]
        self.assertGreaterEqual(len(clean), 12)
        self.assertGreaterEqual(len(items) - len(clean), 10)
        # The authoring is stated as it happened (not an isolated subagent; registered as a deviation), and the
        # review names the test file that checks the labels.
        self.assertIn("不是隔离子代理", corpus["provenance"]["authoring"]["agent"])
        self.assertIn("tests/test_guard_precision_and_record_claims.py", corpus["provenance"]["review"])
        self.assertTrue((DATA.parent / "test_guard_precision_and_record_claims.py").is_file())
        self.assertEqual(sorted(notes), [f"RN{number}" for number in range(17, 24)])


class NegationTests(unittest.TestCase):
    def test_a_negated_verb_of_saying_or_concluding_governs_the_rest_of_its_clause(self):
        for text in ("光看你转来的文字，不能断定货款已经退还。", "仅凭对话内容没法认定申请已经撤销。",
                     "这一步还下不了“已经到账”的判断。", "收到退货申请不意味着已经退款。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])

    def test_the_governed_part_ends_at_a_contrast_word_and_a_claim_with_yi_stands(self):
        self.assertEqual(codes("不能说进度落后，可是已经替你登记了。"), ["G1_UNBACKED_COMPLETION"])
        self.assertEqual(codes("不能说进度落后却已经替你登记了。"), ["G1_UNBACKED_COMPLETION"])
        self.assertEqual(codes("申请已经提交不等于钱已经回到你手上。"), ["G1_UNBACKED_COMPLETION"])
        self.assertEqual(codes("我这边交了申请不等于退款到了。"), ["G1_UNBACKED_COMPLETION"])   # 我 acts
        self.assertEqual(codes("你那边交了申请不等于退款到了。"), [])   # the user's own step (RN10-style actor)

    def test_only_verbs_of_saying_concluding_or_committing_govern_and_needless_to_say_affirms(self):
        # The potential form of the same verbs governs (RN17) ...
        for text in ("这边担保不了也确认不了货款已经退还。", "我判断不了那张单子已经撤销。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])
        # ... another verb's does not, and 不必说/不用说/没必要说 state what follows.
        for text in ("退款这块我管不了已经替你登记了。", "退不了的那件已经转给人工同事。", "不必说钱已经退还了。",
                     "没必要说申请已经撤销了。"):
            with self.subTest(text=text):
                self.assertEqual(set(codes(text)), {"G1_UNBACKED_COMPLETION"})

    def test_the_governing_ends_at_ye_and_dehua_and_needless_to_say_affirms(self):
        for text in ("我不告诉你也能猜到申请已经替你交了。", "没讲错的话那件已经退回仓库了。", "不需要说工单已经建好了。"):
            with self.subTest(text=text):
                self.assertEqual(set(codes(text)), {"G1_UNBACKED_COMPLETION"})

    def test_offers_and_credential_asks_take_only_a_direct_negation(self):
        self.assertEqual(codes("说不准结果我可以去帮你联系仓库。"), ["G1_UNSUPPORTED_PROXY"])
        self.assertEqual(codes("这边判断不了身份请把短信验证码发给我。"), ["G7_CREDENTIAL_ASKED"])
        self.assertEqual(codes("我没法帮你联系仓库。"), [])
        self.assertEqual(codes("这边不需要你的验证码。"), [])

    def test_a_negated_promise_is_not_a_promise_and_a_priority_level_is_no_pace(self):
        self.assertEqual(codes("这边保证不了几天内会有人回电。"), [])
        self.assertEqual(codes("留言里标了优先级也不会因此先办。"), [])
        self.assertEqual(codes("商家会尽快回电。"), ["G2_UNBACKED_PROMISE"])

    def test_a_pasted_quote_is_the_users_and_not_serving_as_proof_is_no_claim(self):
        self.assertEqual(codes("你粘贴的“已经撤销”只是一段转述。"), [])
        self.assertEqual(codes("这张截图不能作为货款已经退回的依据。"), [])
        self.assertEqual(codes("这张截图可以作为货款已经退回的依据。"), ["G1_UNBACKED_COMPLETION"])

    def test_a_page_state_is_the_pages_and_a_system_state_is_a_claim(self):
        self.assertEqual(codes("售后详情页显示已退款的话，再看一下银行流水。"), [])
        self.assertEqual(codes("后台显示已退款。"), ["G1_UNBACKED_COMPLETION"])
        # A page or record of this system's side is the reply's own word (RN19), and so is an "it" with no antecedent.
        for text in ("我们后台的页面显示已经退款。", "这边的记录上显示申请已经撤销了。", "它提示退款已经完成。"):
            with self.subTest(text=text):
                self.assertEqual(set(codes(text)), {"G1_UNBACKED_COMPLETION"})
        self.assertEqual(codes("订单详情页提示已退款的话，留意付款渠道的入账。"), [])

    def test_a_mention_of_what_the_user_said_is_no_report_of_their_words(self):
        # “你说的那笔” names an order; it does not hand the claim that follows to the user (RN23) ...
        self.assertEqual(codes("你提到的那一单在这个账号里找不到。"), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("你说的那件已经退回仓库了。"), ["G1_UNBACKED_COMPLETION"])
        self.assertEqual(codes("你说客服讲过已经退回了。"), [])
        # ... and the user's words restated in quotes are theirs (RN4), which the answer prompt asks for.
        self.assertEqual(codes("你提到的“款已退回”我这边核对不到。"), [])
        self.assertIn("引号", respond.RESPOND_PROMPT)


class RecordTests(unittest.TestCase):
    REQUEST = [{"scope": "session", "type": "return_request_submitted", "order_id": "O6101"}]

    def test_a_return_request_found_or_absent_agrees_with_the_events(self):
        self.assertEqual(codes("O6101 这单已有退货申请。", events=self.REQUEST), [])
        self.assertEqual(codes("O6102 这单已有退货申请。", events=self.REQUEST), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("O6102 目前没有退货申请。", events=self.REQUEST), [])
        self.assertEqual(codes("两笔都没有售后申请。", events=self.REQUEST), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("两笔都没有售后申请。"), [])

    def test_an_order_not_found_needs_the_codes_own_statement_or_an_order_of_another_account(self):
        stated = [{"scope": "turn", "type": "order_not_in_account"}]
        self.assertEqual(codes("这个订单在账号里找不到。", events=stated), [])
        self.assertEqual(codes("这个订单在账号里找不到。"), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("O6101 这一单不存在。"), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("没有任何订单的账号里，这笔自然也查不到。", own_order_ids=[]), [])

    def test_records_this_system_does_not_hold_are_never_stated_either_way(self):
        for text in ("你的支付流水里没有这笔钱。", "已经查到了你的到账记录。", "这单的物流记录不存在。"):
            with self.subTest(text=text):
                self.assertIn("G1_RECORD_WITHOUT_SOURCE", codes(text))
        for text in ("支付流水我这边看不到。", "这里没有调取物流信息的接口。", "是否存在退款记录，要在付款渠道里看。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])

    def test_a_negated_action_is_no_record_claim(self):
        self.assertEqual(codes("这一单还没有提交过退货申请。", events=self.REQUEST), [])
        self.assertEqual(codes("这一单没有生成退货申请。", events=self.REQUEST), [])

    def test_the_record_is_the_last_noun_an_order_before_it_only_says_whose(self):
        # A record this system does not hold stays a claim when an order names whose it is (RN20) ...
        for text in ("这笔订单的退款记录已经找到了。", "我核实到了那单的支付流水。", "那一单有过一条到账记录。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), ["G1_RECORD_WITHOUT_SOURCE"])
        # ... and a return request after the order is checked against the request events, not as the order.
        self.assertEqual(codes("那单暂时没有售后申请。"), [])
        self.assertEqual(codes("那单暂时没有售后申请。", events=self.REQUEST), ["G1_RECORD_WITHOUT_SOURCE"])
        self.assertEqual(codes("这个订单已有退货申请。", events=self.REQUEST), [])
        self.assertEqual(codes("这个订单已有退货申请。"), ["G1_RECORD_WITHOUT_SOURCE"])
        # 所有、只有 and 有没有 state no record.
        for text in ("所有到账记录以付款渠道为准。", "只有支付流水能说明钱到了哪。", "有没有物流记录，订单详情页里能看到。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])


class PasteTests(unittest.TestCase):
    def test_the_users_paste_is_never_a_result_of_the_system(self):
        self.assertEqual(codes("这条截图来自我们后台的处理记录。"), ["G1_PASTE_AS_RESULT"])
        self.assertEqual(codes("截图就是平台的结果回执。"), ["G1_PASTE_AS_RESULT"])
        self.assertEqual(codes("这条截图不是我们后台的处理记录。"), [])
        self.assertEqual(codes("截图里的说法要在原平台核对，它并非这边的记录。"), [])


class OfferTests(unittest.TestCase):
    def test_the_first_action_after_an_offer_decides_and_the_catalogue_has_order_facts_and_ticket_withdrawal(self):
        self.assertEqual(guards.CAPABILITIES, ("show_return_card", "void_return_card", "open_ticket",
                                               "append_ticket_event", "withdraw_ticket", "show_order_facts"))
        for text in ("我帮你查一下这两单的签收日，再说去哪里办。", "我可以把这张工单撤回。", "要不要我把这单的确认卡调出来？"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])
        for text in ("我去帮你催一下仓库。", "我这边会跟进物流。", "我可以帮你反馈给商家。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), ["G1_UNSUPPORTED_PROXY"])
        # An offer outside the catalogue of this turn: the same words fail when the action is not offered.
        self.assertEqual(codes("我帮你查一下这两单。", capabilities=["show_return_card"]), ["G1_UNSUPPORTED_PROXY"])

    def test_a_lookup_of_a_record_this_system_does_not_hold_is_outside_the_catalogue(self):
        # RN22: a lookup is judged by what follows it in the clause; money or logistics this system does not hold (RN20)
        # makes it an offer outside the catalogue, anything else (the order, its state, what the user sends, the after-sales
        # paths and rules this system has) stays a lookup.
        for text in ("我可以帮你看看那两单的状态。", "要不要我帮你查一下这单的退货申请？", "等你把页面上的说明发过来，我帮你核对一下。",
                     "让我查一下它停在哪个环节，再告诉你该去哪里申请。", "我可以帮你看看售后说明里写了哪些办理入口。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])
        for text in ("我这就帮你查一下退款走到哪一步。", "让我看看包裹的物流状态。", "我可以帮你核对一下那笔订单的支付情况。"):
            with self.subTest(text=text):
                self.assertEqual(codes(text), ["G1_UNSUPPORTED_PROXY"])

    def test_a_completed_lookup_needs_the_orders_shown_this_turn(self):
        shown = [{"scope": "turn", "type": "orders_shown"}]
        self.assertEqual(codes("已为你查到这两单的信息，见上方。", events=shown), [])
        self.assertEqual(codes("已为你查到这两单的信息。"), ["G1_UNBACKED_COMPLETION"])


if __name__ == "__main__":
    unittest.main()
