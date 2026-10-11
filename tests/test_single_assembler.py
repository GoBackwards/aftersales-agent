"""The single assembler (B5d2, plan §5.3; B5f): one fixed order, every block with its author, the reply their text joined.

Code blocks come first (the turn's actions, order facts), then the answer's items in item order (whole cards with the
selected outcome first and the others folded, the item's note or the code's line, its path), the route, the model's
next step and at most one follow-up whose options are the code's. Only a turn that only refuses or only names a gap
puts the items before the order facts. Synthetic cards and wording; mechanics, not answer quality.
"""
import unittest

from support_agent import assemble
from support_agent import copy as code_copy
from support_agent.guards import A0_WORDS

CARD = {"id": "branchy", "title": "合成分支规则", "scope": "仅用于工程测试", "effect_domain": "return_eligibility",
        "conditions": ["条件甲", "条件乙"], "outcomes": ["分支一的结果。", "分支二的结果。", "分支三的结果。"],
        "outcome_premises": [["只在情形一"], ["只在情形二"], ["只在情形三"]], "exceptions": ["另有例外"],
        "evidence_ids": ["e1"], "references": [{"evidence_id": "e1", "quotes": ["原文一句"]}]}


def kinds(blocks):
    return [(block["type"], block["author"]) for block in blocks]


def row(index, disposition, claims=(), path="none"):
    return {"index": index, "disposition": disposition, "claims": list(claims), "path_id": path, "note": ""}


class OrderTests(unittest.TestCase):
    def test_every_block_comes_in_the_fixed_order_with_its_author(self):
        business = {"type": "business", "author": "code", "text": "业务句"}
        facts = {"type": "order_facts", "author": "code", "text": "订单句"}
        route = {"type": "route", "author": "code", "text": "路线句"}
        items = assemble.item_blocks([row(1, "answered", [{"card_id": "branchy", "outcome_index": 1}], "policy_page"),
                                      {**row(2, "gap", path="policy_page"), "note": "模型的话"},
                                      row(3, "refused", path="unknown_path")], [CARD], answering={1, 2, 3})
        reply, blocks = assemble.assemble(action_blocks=[business], order_facts=facts, items=items, route=route,
                                          next_step="下一步的话", ask={"text": "要问的一句", "option_set_id": "fee_type"},
                                          notes=["假设句"])
        self.assertEqual(kinds(blocks), [("business", "code"), ("text", "code"), ("order_facts", "code"),
                                         ("policy_card", "code"), ("path", "code"), ("text", "model"),
                                         ("text", "code"), ("route", "code"), ("text", "model"), ("ask", "model")])
        self.assertEqual(reply, "\n\n".join(["业务句", "假设句", "订单句", blocks[3]["text"],
                                             code_copy.PATH_LABEL + code_copy.PATHS["policy_page"], "模型的话",
                                             code_copy.ITEM_LINES["refused"], "路线句", "下一步的话", "要问的一句"]))
        self.assertEqual((blocks[5]["item"], blocks[6]["item"], blocks[6]["disposition"], blocks[8]["next_step"]),
                         (2, 3, "refused", True))
        self.assertEqual(blocks[-1]["options"], code_copy.OPTION_SETS["fee_type"]["options"])
        self.assertEqual(assemble.reply_text(blocks), reply)

    def test_the_fallback_stands_alone_and_only_when_the_reply_would_show_nothing(self):
        # B7r: any block is content, a path or a code sentence included; the agent passes fallback=False while an open
        # card stays (its reminder line is content too).
        empty = assemble.item_blocks([row(1, "answered")], [], answering={1})
        self.assertEqual(empty, [])
        reply, blocks = assemble.assemble(items=empty, fallback=True)
        self.assertEqual((reply, kinds(blocks)), (code_copy.FALLBACK, [("text", "code")]))
        self.assertTrue(blocks[0]["fallback"])
        for shown in ({"items": assemble.item_blocks([row(1, "answered", path="platform_human")], [], answering={1})},
                      {"action_blocks": [{"type": "business", "author": "code", "text": "业务句"}]},
                      {"notes": ["假设句"]}, {"next_step": "下一步的话"},
                      {"ask": {"text": "要问的一句", "option_set_id": "none"}}):
            with self.subTest(shown=sorted(shown)):
                self.assertFalse(any(block.get("fallback") for block in assemble.assemble(**shown, fallback=True)[1]))
        self.assertEqual(assemble.assemble(items=empty), ("", []))
        self.assertEqual(assemble.item_blocks([row(1, "handled_by_system")], [], answering=set()), [])

    def test_an_item_left_with_nothing_gets_the_codes_line_for_its_disposition(self):
        rows = [row(index, disposition) for index, disposition in
                enumerate(("gap", "refused", "routed", "partial", "asked", "answered", "handled_by_system"), 1)]
        blocks = assemble.item_blocks(rows, [], answering=set(range(1, 8)))
        self.assertEqual([(block["item"], block["text"]) for block in blocks if block.get("item")],
                         [(index, code_copy.ITEM_LINES[rows[index - 1]["disposition"]]) for index in range(1, 6)])
        # Its own content keeps the line away: a note, an outcome, a shown follow-up for an asked item.
        noted = assemble.item_blocks([{**row(1, "gap"), "note": "这一点资料没写到。"}], [], answering={1})
        self.assertEqual([(block["author"], block["text"]) for block in noted], [("model", "这一点资料没写到。")])
        asked = assemble.item_blocks([row(1, "asked")], [], answering={1}, ask_shown=True)
        self.assertEqual(asked, [])

    def test_a_refusal_or_gap_turn_puts_the_items_first_otherwise_they_follow_the_code(self):
        facts = {"type": "order_facts", "author": "code", "text": "订单句"}
        items = assemble.item_blocks([{**row(1, "refused"), "note": "这件事这里办不了。"}], [], answering={1})
        _, first = assemble.assemble(order_facts=facts, items=items, lead=True)
        self.assertEqual(kinds(first), [("text", "model"), ("order_facts", "code")])
        _, after = assemble.assemble(order_facts=facts, items=items)
        self.assertEqual(kinds(after), [("order_facts", "code"), ("text", "model")])

    def test_an_empty_ask_and_an_empty_next_step_add_nothing(self):
        reply, blocks = assemble.assemble(next_step=" ", ask={"text": "  ", "option_set_id": "none"})
        self.assertEqual((reply, blocks), ("", []))


class CardBlockTests(unittest.TestCase):
    def test_a_cited_card_shows_its_selected_outcomes_first_and_the_rest_folded(self):
        # B5f: the selected outcome is the direct answer; the card's title follows as its basis.
        block = assemble.card_block(CARD, [2, 0])
        self.assertEqual([row["outcome_index"] for row in block["selected"]], [2, 0])
        self.assertEqual([row["outcome_index"] for row in block["other_outcomes"]], [1])
        self.assertEqual(block["other_outcomes"][0]["premises"], ["只在情形二"])
        self.assertEqual(block["text"], "分支三的结果。\n适用条件：只在情形三。\n"
                                        "分支一的结果。\n适用条件：只在情形一。\n依据：合成分支规则")
        self.assertNotIn("分支二", block["text"])
        self.assertEqual((block["exceptions"], block["references"]), (["另有例外"], CARD["references"]))
        self.assertEqual(block["source_kind"], "platform")

    def test_a_card_shows_once_where_it_first_appears_with_every_outcome_any_item_selected(self):
        rows = [row(1, "answered", [{"card_id": "branchy", "outcome_index": 1}, {"card_id": "gone", "outcome_index": 0}]),
                row(2, "answered", [{"card_id": "branchy", "outcome_index": 0}, {"card_id": "branchy", "outcome_index": 1}])]
        blocks = assemble.item_blocks(rows, [CARD], answering={1, 2})
        self.assertEqual([(block["type"], [part["outcome_index"] for part in block["selected"]]) for block in blocks],
                         [("policy_card", [1, 0])])


class BusinessWordingTests(unittest.TestCase):
    def test_every_outcome_is_worded_by_copy_and_states_no_environment(self):
        view = {"order_id": "O7001", "product_name": "合成水杯", "paid_amount_cents": 1200, "delivered_at": "2026-09-15",
                "days_since_delivery": 3, "eligibility_code": "eligible", "return_eligible": True,
                "last_return_date": "2026-09-22", "last_return_day": False, "return_request": None}
        results = [{"ok": True, "status": "order_found", "orders": [view]},
                   {"ok": True, "status": "awaiting_confirmation", "order": view, "card": {"order_id": "O7001"}},
                   {"ok": True, "status": "card_voided", "voided_order_id": "O7001"},
                   {"ok": True, "status": "card_choice_needed"}, {"ok": True, "status": "no_open_card"},
                   {"ok": True, "status": "submitted_no_undo"},
                   {"ok": True, "status": "clarification_needed", "missing_fields": ["query_order_id"]},
                   {"ok": False, "status": "blocked", "error": {"code": "UNSOURCED_FIELD"}}]
        for result in results:
            with self.subTest(status=result["status"]):
                block = assemble.business_block({**result, "item_kind": "own_order_status"})
                self.assertEqual((block["author"], block["action"]), ("code", "own_order_status"))
                self.assertTrue(block["text"])
                self.assertFalse([word for word in A0_WORDS if word in block["text"]])
        self.assertIsNone(assemble.business_block({"ok": True, "status": "no_action", "item_kind": "none"}))
        self.assertEqual(assemble.order_line(view), "订单 O7001：合成水杯，实付 12.00 元，签收日 2026-09-15（签收后第 3 天）。"
                                                    + code_copy.RETURNABLE.format(last_date="2026-09-22"))

    def test_an_order_this_account_does_not_have_is_named_only_as_the_user_typed_it(self):
        blocked = {"ok": False, "status": "blocked", "error": {"code": "ORDER_NOT_FOUND"}}
        self.assertEqual(assemble.business_block(blocked, foreign_ids=["O2001"])["text"],
                         code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="O2001"))
        self.assertEqual(assemble.business_block(blocked)["text"], code_copy.ORDER_NOT_FOUND)
        self.assertEqual(assemble.not_in_account_block(["O2001", "O2002"]),
                         {"type": "text", "author": "code",
                          "text": code_copy.ORDERS_NOT_IN_ACCOUNT.format(order_ids="O2001、O2002")})


class RouteTests(unittest.TestCase):
    def test_the_route_block_is_worded_by_code_from_the_checked_table(self):
        found = {"status": "evidence_found", "amount_requested": True, "source_amount_text": "13",
                 "requested_route": {"origin": "甲省", "destination": "乙省"}}
        block = assemble.route_block(found)
        self.assertEqual((block["author"], block["origin"], block["destination"], block["amount_text"]),
                         ("code", "甲省", "乙省", "13"))
        self.assertIn("13 元", block["text"])
        missing = assemble.route_block({"status": "needs_clarification", "missing": ["destination"]})
        self.assertEqual(missing["text"], code_copy.ROUTE_NEEDS_PLACES.format(missing=code_copy.ROUTE_PLACE["destination"]))
        self.assertIsNone(assemble.route_block({"status": "not_requested"}))
        self.assertEqual(assemble.route_text({"status": "no_route"}), code_copy.ROUTE_NOT_FOUND)



class PageTests(unittest.TestCase):
    """Static checks of the chat page (the T5 walk-through looks at it in a browser)."""

    def setUp(self):
        from pathlib import Path
        self.chat = (Path(__file__).resolve().parents[1] / "support_agent" / "web" / "chat.js").read_text(encoding="utf-8")

    def test_the_page_marks_the_models_words_and_only_them(self):
        body = self.chat[self.chat.index("function renderBlock"):self.chat.index("function blockView")]
        self.assertIn('view.dataset.author = block.author === "model" ? "model" : "code";', body)
        self.assertIn('if (block.author === "model") view.prepend(h("span.author-mark", { text: "模型撰写" }));', body)

    def test_the_page_draws_a_cited_card_whole_with_the_other_outcomes_folded(self):
        body = self.chat[self.chat.index("function cardBlock"):self.chat.index("function askBlock")]
        # B5f: the selected outcomes come first, the card's title after them as their basis.
        self.assertLess(body.index("(block.selected || []).map(outcome)"), body.index('h("div.rule-head"'))
        self.assertIn('h("details", h("summary", icon("chevron"), `这张卡的其他情形（${others.length}）`)', body)
        self.assertNotIn("innerHTML", body)

    def test_the_page_knows_only_the_blocks_the_assembler_writes(self):
        body = self.chat[self.chat.index("function blockView"):self.chat.index("function businessBlock")]
        for kind in ("business", "order_facts", "policy_card", "route", "path", "ask", "return_card", "text"):
            self.assertIn(f'case "{kind}":', body)
        for retired in ("policy_claims", "followups", "guidance", "notice"):
            self.assertNotIn(f'case "{retired}":', body)


if __name__ == "__main__":
    unittest.main()
