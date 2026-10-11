"""The whole-reply fallback stands only for a reply that would show nothing at all (B7r).

B7: a pause over an open card is chit-chat (K21) and the card stays; the reply's reminder line already says so, yet the
fallback sentence stood beside it. Every code block, path or reminder line is content, so the fallback only replaces a
reply that would otherwise be empty. The reminder is a block like every code sentence (the page draws blocks, not the
reply string), so such a turn is never an empty box. The answer prompt names the planner's newer item kinds and says
chit-chat cites no card. Scripted model, real store; synthetic wording; mechanics, not answer quality.
"""
from itertools import count
from pathlib import Path
import tempfile
import unittest

from support_agent import copy as code_copy
from support_agent import respond
from support_agent.turn import ConversationAgent
from support_agent.sessions import SessionStore
from tests.test_graph_agent import ScriptedModel, answered, item, planned


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / "fallback.sqlite3")
        self.addCleanup(self.store.close)
        self.sid = self.store.create_session("alice")["session_id"]
        self.ids = count()

    def send(self, message, *outputs):
        model = ScriptedModel(list(outputs))
        result = ConversationAgent(model, self.store, "alice").send(self.sid, message, f"f-{next(self.ids)}")
        return result, model

    @staticmethod
    def fallback_traced(result):
        return [entry for entry in result["trace"] if entry["type"] == "fallback_used"]

    @staticmethod
    def text_blocks(result):
        return [(block["author"], block["text"]) for block in result["presentation"]["blocks"]
                if block["type"] == "text"]

    def test_a_pause_over_an_open_card_shows_the_reminder_and_no_fallback(self):
        self.send("O1001 申请退货", planned([item("return_request", "申请退货", order="O1001", order_id="O1001")]))
        result, model = self.send("卡先放着，我再琢磨琢磨", planned([item("chit_chat", "我再琢磨琢磨")]),
                                  answered("", disposition="answered"))
        self.assertEqual((result["status"], len(model.calls)), ("completed", 2))
        reminder = code_copy.OPEN_CARDS_WAITING.format(order_ids="O1001")
        self.assertEqual(result["reply"], reminder)
        # The page draws the blocks, not the reply string: the reminder is a block, so the turn is never an empty box.
        self.assertEqual(self.text_blocks(result), [("code", reminder)])
        self.assertEqual([block["type"] for block in result["presentation"]["blocks"]], ["return_card", "text"])
        self.assertNotIn(code_copy.FALLBACK, result["reply"])
        self.assertEqual((result["answer_validation"]["fallback"], self.fallback_traced(result)), (False, []))
        self.assertEqual([card["order_id"] for card in self.store.get_session("alice", self.sid)["open_cards"]],
                         ["O1001"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_beside_other_content_the_reminder_is_the_last_block_and_the_replys_last_line(self):
        self.send("O1001 申请退货", planned([item("return_request", "申请退货", order="O1001", order_id="O1001")]))
        result, _ = self.send("顺便问一下，退回的包裹怎么寄", planned([item("policy_question", "退回的包裹怎么寄")]),
                              answered("", disposition="gap", path="policy_page"))
        reminder = code_copy.OPEN_CARDS_WAITING.format(order_ids="O1001")
        blocks = result["presentation"]["blocks"]
        self.assertEqual((blocks[-1]["type"], blocks[-1]["author"], blocks[-1]["text"]), ("text", "code", reminder))
        self.assertTrue(result["reply"].endswith("\n\n" + reminder))
        self.assertEqual(result["reply"].count(reminder), 1)
        self.assertFalse(result["answer_validation"]["fallback"])

    def test_with_no_open_card_an_otherwise_empty_reply_is_the_fallback(self):
        result, _ = self.send("嗯嗯好的", planned([item("chit_chat", "嗯嗯好的")]), answered("", disposition="answered"))
        self.assertEqual(result["reply"], code_copy.FALLBACK)
        self.assertTrue(result["answer_validation"]["fallback"])
        self.assertEqual(self.fallback_traced(result), [{"type": "fallback_used", "reason": "no_visible_content"}])

    def test_a_path_alone_is_content_and_keeps_the_fallback_away(self):
        result, _ = self.send("这件事该去哪里说", planned([item("policy_question", "该去哪里说")]),
                              answered("", disposition="answered", path="platform_human"))
        self.assertEqual(result["reply"], code_copy.PATH_LABEL + code_copy.PATHS["platform_human"])
        self.assertFalse(result["answer_validation"]["fallback"])

    def test_the_answer_prompt_names_the_planners_newer_item_kinds_and_chit_chat_cites_no_card(self):
        prompt = respond.RESPOND_PROMPT
        for words in ("handoff_request", "complaint", "safety", "improper_request", "chit_chat", "不用引用规则卡"):
            with self.subTest(words=words):
                self.assertIn(words, prompt)
        self.assertLessEqual(len(prompt), 1500)


if __name__ == "__main__":
    unittest.main()
