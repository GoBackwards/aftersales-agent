"""The cards are the answer; the model's few words are checked piece by piece (B5f; plan §9 first overturn condition,
principle 4: the model selects ids, code renders text).

R109: the model still restated the cards' conclusions in its own words beside the placeholders, and a single hit
dropped all of its words. B5f, as behaviour: an item answered with card outcomes shows the selected outcome first and
carries no words of the model; only an item a card cannot answer (a gap, a refusal, a routing) may carry a short note,
the turn one next-step sentence and at most one follow-up. A guard hit drops that piece only; an answer item left with
nothing of its own gets the code's line for its disposition beside its path; the whole-reply fallback stands only when
nothing at all is shown. What is only untidy (an id written in the words, a repeated item row) is removed without a
second call. G10 (D8): the names the catalogue's sources are published under, a field of the catalogue itself, never
appear in the words. Scripted models over loopback HTTP and SQLite; synthetic cards, names and wording; mechanics,
not answer quality.
"""
from datetime import date
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_agent import assemble, guards, respond
from support_agent import copy as code_copy
from support_agent.catalog import CATALOG_PATH, catalogue, publisher_problems

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
APP = Path(__file__).resolve().parents[1]
PUBLISHER = "合成商城"
CARD = {"id": "synthetic-swap", "title": "合成换新规则", "aliases": ["换新无忧"], "scope": "仅用于工程测试",
        "conditions": ["包装完整"], "outcomes": ["包装完整时可以换新一次。", "包装不完整时按页面说明处理。"],
        "outcome_premises": [["包装完整"], ["包装不完整"]], "exceptions": [], "evidence_ids": ["synthetic-source"],
        "effect_domain": "return_eligibility"}
CLAIM = [{"card_id": "synthetic-swap", "outcome_index": 0}]


def row(index, disposition, *, claims=(), path="none", note=""):
    return {"index": index, "disposition": disposition, "claims": list(claims), "path_id": path, "note": note}


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class AnswerOverHTTPTests(unittest.TestCase):
    from tests import test_graph_agent as fixtures
    setUp = fixtures.GraphHTTPTests.setUp
    stop = fixtures.GraphHTTPTests.stop
    request = fixtures.GraphHTTPTests.request
    start = fixtures.GraphHTTPTests.start
    send = fixtures.GraphHTTPTests.send

    def run_with_card(self, *outputs, message="换新的条件是什么？", questions=()):
        plan = self.fixtures.planned([self.fixtures.item("policy_question", quote) for quote in questions or (message,)])
        self.model.outputs.extend([plan, *outputs])
        with patch("support_agent.policy.PolicyAdapter.catalogue_cards", return_value=[CARD]), \
             patch("support_agent.turn.catalogue",
                   return_value={"cards": [CARD], "source_publishers": [PUBLISHER]}), \
             patch("support_agent.policy.PolicyAdapter.source_cards", return_value=[]):
            return self.send(self.start(), message)[1]

    @staticmethod
    def trace(result, kind):
        return [entry for entry in result["trace"] if entry["type"] == kind]

    def kinds(self, result):
        return [(block["type"], block["author"]) for block in result["presentation"]["blocks"]]

    def test_an_answered_item_shows_its_outcome_first_and_its_words_are_never_restated(self):
        result = self.run_with_card(self.fixtures.answered("这一条是说包装完整就能换。", claims=CLAIM))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["reply"].startswith("包装完整时可以换新一次。\n适用条件：包装完整。\n依据：合成换新规则"))
        self.assertNotIn("这一条是说", result["reply"])
        self.assertEqual(self.kinds(result), [("policy_card", "code")])
        self.assertEqual(self.trace(result, "structure_tidied")[0]["dropped"],
                         [{"code": "G9_NOTE_NOT_ALLOWED", "part": "note:1"}])
        self.assertEqual((self.trace(result, "structure_correction"), self.trace(result, "fallback_used")), ([], []))
        self.assertEqual(len(self.model.calls), 2)

    def test_a_hit_drops_its_own_piece_and_the_other_item_keeps_its_note(self):
        kept = "我这边的资料没写到包装要完整到什么程度。"
        rows = [row(1, "gap", path="policy_page", note=kept),
                row(2, "routed", path="merchant_contact", note="商家会尽快联系你处理换新。")]
        result = self.run_with_card(self.fixtures.answered(items=rows), message="包装要多完整？能直接换吗？",
                                    questions=("包装要多完整？", "能直接换吗？"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([(hit["code"], hit["part"]) for hit in self.trace(result, "guards")[0]["hits"]],
                         [("G2_UNBACKED_PROMISE", "note:2")])
        self.assertEqual(self.kinds(result), [("text", "model"), ("path", "code"), ("text", "code"), ("path", "code")])
        blocks = result["presentation"]["blocks"]
        self.assertEqual((blocks[0]["text"], blocks[0]["item"]), (kept, 1))
        self.assertEqual((blocks[2]["text"], blocks[2]["item"], blocks[2]["disposition"]),
                         (code_copy.ITEM_LINES["routed"], 2, "routed"))
        self.assertFalse(result["answer_validation"]["fallback"])
        self.assertTrue(result["answer_validation"]["prose_shown"])
        self.assertEqual([item["note"] for item in result["graph"]["selection"]["items"]], [kept, ""])

    def test_the_catalogues_publishers_never_appear_in_the_words(self):
        # D8: a data-derived lexicon (the catalogue's source_publishers); the text is read as written, so a quoted
        # or asked name is a hit too.
        named = f"这一点要到{PUBLISHER}的页面上看。"
        result = self.run_with_card(self.fixtures.answered(
            named, path="policy_page", next_step=f"也可以问问“{PUBLISHER}”的客服。",
            ask={"text": f"你是在{PUBLISHER}买的吗？", "option_set_id": "none"}))
        self.assertEqual(sorted((hit["code"], hit["part"]) for hit in self.trace(result, "guards")[0]["hits"]),
                         [("G10_PUBLISHER_NAMED", "ask"), ("G10_PUBLISHER_NAMED", "next_step"),
                          ("G10_PUBLISHER_NAMED", "note:1")])
        self.assertNotIn(PUBLISHER, result["reply"])
        self.assertEqual(self.kinds(result), [("text", "code"), ("path", "code")])
        self.assertEqual(result["presentation"]["blocks"][0]["text"], code_copy.ITEM_LINES["gap"])

    def test_an_id_written_in_the_words_drops_that_piece_without_a_second_call(self):
        result = self.run_with_card(self.fixtures.answered("可以对照[[synthetic-swap]]看看。",
                                                           next_step="下一步看 merchant_contact 那条。"))
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.trace(result, "structure_correction"), [])
        self.assertEqual(self.trace(result, "structure_tidied")[0]["dropped"],
                         [{"code": "G9_ID_IN_TEXT", "part": "note:1"},
                          {"code": "G9_PATH_MISSING", "part": "path:1", "path_id": "policy_page"},
                          {"code": "G9_ID_IN_TEXT", "part": "next_step"}])
        self.assertNotIn("[[", result["reply"])
        # B7: the message is one policy question, so its gap gets the product page's notes (B5g).
        self.assertEqual(result["reply"], code_copy.ITEM_LINES["gap"] + "\n\n" + code_copy.PATH_LABEL
                         + code_copy.PATHS["policy_page"])

    def test_a_repeated_item_row_is_tidied_without_a_second_call(self):
        # R109: an answer listing one item twice failed its turn after the same-name retry; it is only untidy.
        rows = [row(1, "answered", claims=CLAIM), row(1, "asked")]
        result = self.run_with_card(self.fixtures.answered(items=rows))
        self.assertEqual((result["status"], len(self.model.calls)), ("completed", 2))
        self.assertEqual(self.trace(result, "structure_tidied")[0]["dropped"],
                         [{"code": "G9_ITEM_REPEATED", "part": "item:1"}])
        self.assertEqual(self.kinds(result), [("policy_card", "code")])

    def test_an_item_answered_with_a_card_may_ask_one_more_fact_on_the_first_try(self):
        # R106: such answers went back twice and three of six ended as failed turns; B5e let them pass at once.
        ask = {"text": "包装现在还完整吗？", "option_set_id": "item_condition"}
        result = self.run_with_card(self.fixtures.answered("", claims=CLAIM, ask=ask))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.trace(result, "structure_correction"), [])
        self.assertEqual(result["graph"]["selection"]["items"][0]["disposition"], "answered")
        self.assertEqual(self.kinds(result), [("policy_card", "code"), ("ask", "model")])

    def test_the_fallback_sentence_stands_only_when_nothing_at_all_is_shown(self):
        empty = self.run_with_card(self.fixtures.answered("", disposition="answered"))
        self.assertEqual(empty["reply"], code_copy.FALLBACK)
        self.assertEqual(self.trace(empty, "fallback_used"), [{"type": "fallback_used", "reason": "no_visible_content"}])
        self.assertTrue(empty["answer_validation"]["fallback"])

    def test_a_shown_next_step_keeps_the_fallback_away(self):
        step = "可以把包装现在的样子再说一下。"
        with_step = self.run_with_card(self.fixtures.answered("", disposition="answered", next_step=step))
        self.assertEqual(with_step["reply"], step)
        self.assertEqual(self.kinds(with_step), [("text", "model")])
        self.assertTrue(with_step["presentation"]["blocks"][0]["next_step"])
        self.assertFalse(with_step["answer_validation"]["fallback"])


class PublisherLexiconTests(unittest.TestCase):
    def test_the_repository_catalogue_names_its_publishers_word_for_word_in_its_quotes(self):
        data = json.loads(Path(CATALOG_PATH).read_text(encoding="utf-8"))
        names = catalogue()["source_publishers"]
        self.assertTrue(names)
        self.assertFalse(publisher_problems(data))
        quotes = "".join(quote for card in data["cards"] for ref in card["references"] for quote in ref["quotes"])
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, quotes)
                # The lexicon is data: without the catalogue's names in the context, G10 has nothing to look for.
                self.assertNotIn("G10", guards.guard_ids(guards.check(f"{name}的页面写着呢。", guards.context())))

    def test_a_publisher_missing_from_the_quotes_or_a_malformed_list_is_refused(self):
        base = {"cards": [{"references": [{"quotes": ["合成商城的退换说明"]}]}]}
        self.assertFalse(publisher_problems(base))
        self.assertFalse(publisher_problems({**base, "source_publishers": ["合成商城"]}))
        for names in (["别处的店"], "合成商城", ["合成商城", "合成商城"], [""], ["长" * 21], ["合成"] * 7, [None]):
            with self.subTest(names=names):
                self.assertTrue(publisher_problems({**base, "source_publishers": names}))

    @unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
    def test_the_runtime_loader_refuses_a_catalogue_whose_publisher_its_quotes_do_not_contain(self):
        from support_agent.policy import PolicyAdapter
        from tests.test_kb_store import kb_fixture
        with tempfile.TemporaryDirectory(prefix="kb-publishers-") as directory:
            root = Path(directory)
            config, knowledge, baseline, _ = kb_fixture(root)
            catalog = json.loads(baseline.read_text(encoding="utf-8"))
            quote = catalog["cards"][1]["references"][0]["quotes"][0]
            for names, refused in (([quote[2:6]], False), (["原文里没有的店"], True)):
                path = root / f"catalog-{refused}.json"
                path.write_text(json.dumps({**catalog, "source_publishers": names}, ensure_ascii=False), encoding="utf-8")
                with self.subTest(names=names):
                    if refused:
                        with self.assertRaises(ValueError) as caught:
                            PolicyAdapter(config, knowledge=knowledge, card_catalog=path)
                        self.assertEqual(str(caught.exception), "GRAPH_CARD_PUBLISHERS_INVALID")
                    else:
                        PolicyAdapter(config, knowledge=knowledge, card_catalog=path)

    def test_g10_reads_the_text_as_written_and_needs_the_catalogues_names(self):
        ctx = guards.context(own_order_ids=["O1001"], source_publishers=[PUBLISHER])
        for text in (f"这是{PUBLISHER}的规定吗？", f"你说的“{PUBLISHER}”我这里看不到。", f"{PUBLISHER}那边的页面写着呢。"):
            with self.subTest(text=text):
                self.assertIn("G10", guards.guard_ids(guards.check(text, ctx)))
        self.assertEqual(guards.check(f"{PUBLISHER}的页面写着呢。", guards.context(own_order_ids=["O1001"])), [])
        self.assertEqual(guards.guard_ids([{"guard": "G10"}, {"guard": "G2"}]), ["G2", "G10"])


class RemovedPlaceholderTests(unittest.TestCase):
    def test_the_placeholder_mechanism_and_the_prose_field_are_gone(self):
        for module, name in ((guards, "CARD_REF"), (guards, "strip_card_refs"), (guards, "PROSE_LIMIT"),
                             (assemble, "render_card_refs"), (assemble, "selected_cards")):
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name))
        sources = "".join(path.read_text(encoding="utf-8") for path in (APP / "support_agent").rglob("*.py"))
        self.assertNotIn("G9_CARD_REF_UNKNOWN", sources)
        self.assertNotIn("[[", respond.RESPOND_PROMPT)
        schema = respond.respond_schema([{"index": 1}], [CARD], ["O1001"])
        self.assertEqual(set(schema["properties"]), {"items", "show_order_ids", "next_step", "ask"})
        self.assertIn("note", schema["properties"]["items"]["items"]["properties"])

    def test_every_block_a_reply_declares_has_an_author_the_return_card_too(self):
        from support_agent.domain import SEED_ORDERS
        from support_agent.return_cards import return_card_block
        order = SEED_ORDERS[0]
        card = {"action_id": "ACT-x", "version": 1, "order_id": order.order_id, "status": "pending",
                "expires_at": date(2026, 9, 18).isoformat()}
        self.assertEqual(return_card_block(card, order)["author"], "code")


if __name__ == "__main__":
    unittest.main()
