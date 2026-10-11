"""Every gap and every refusal shows where to check; the answer prompt says what the code does (B5g; service standard
r3 M6; plan §8.6 B5g row).

R111/R113 found a gap or a refusal shown with the code's line and no destination when the answer named no path. B5g, as
behaviour: such an item never stays without one. When the answer leaves ``path_id`` at none, the code gives the path
catalogue's verification entry for the item's domain (a policy question: the product page's after-sales notes; any
other item: the general one), records it as tidied structure and asks the model nothing more. A routing or an answer
keeps what it chose. The prompt now says the same as the code: a gap or a refusal names a path; numbers, dates and order
ids only as the facts and the cited cards have them (G4, G6); catalogue ids never (G9). Scripted models over loopback
HTTP and SQLite; synthetic wording; mechanics, not answer quality.
"""
import importlib.util
import unittest

from support_agent import copy as code_copy
from support_agent import guards, respond

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
POLICY_ENTRY = code_copy.PATH_LABEL + code_copy.PATHS["policy_page"]
GENERAL_ENTRY = code_copy.PATH_LABEL + code_copy.PATHS["platform_human"]


def row(index, disposition, *, path="none", note=""):
    return {"index": index, "disposition": disposition, "claims": [], "path_id": path, "note": note}


def tidy(rows, kinds):
    items = [{"index": index, "fee_subject": "none",
              "verify_path": code_copy.VERIFY_PATHS.get(kind, code_copy.VERIFY_PATH_GENERAL)}
             for index, kind in enumerate(kinds, 1)]
    answer = {"items": rows, "show_order_ids": [], "next_step": "", "ask": {"text": "", "option_set_id": "none"}}
    return guards.check_structure(answer, {"items": items, "cards": {}, "path_ids": tuple(code_copy.PATHS),
                                           "option_sets": code_copy.OPTION_SETS, "own_order_ids": ["O1001"]})


class DestinationTidyTests(unittest.TestCase):
    def test_a_gap_or_refusal_without_a_path_gets_its_items_verification_entry(self):
        problems, answer, dropped = tidy([row(1, "gap"), row(2, "refused"), row(3, "gap")],
                                         ["policy_question", "message", "own_order_status"])
        self.assertEqual(problems, [])
        paths = ["policy_page", "platform_human", "platform_human"]
        self.assertEqual([item["path_id"] for item in answer["items"]], paths)
        self.assertEqual(dropped, [{"code": "G9_PATH_MISSING", "part": f"path:{index}", "path_id": path}
                                   for index, path in enumerate(paths, 1)])

    def test_a_chosen_path_and_the_other_dispositions_are_left_as_they_are(self):
        rows = [row(1, "gap", path="merchant_contact"), row(2, "refused", path="cancel_order"), row(3, "routed"),
                row(4, "answered"), row(5, "asked"), row(6, "partial")]
        _, answer, dropped = tidy(rows, ["policy_question"] * 6)
        self.assertEqual([item["path_id"] for item in answer["items"]],
                         ["merchant_contact", "cancel_order", "none", "none", "none", "none"])
        self.assertEqual(dropped, [])

    def test_a_schema_problem_still_goes_back_once_and_nothing_is_assigned(self):
        problems, answer, dropped = tidy([row(1, "gap", path="nowhere"), row(2, "gap")],
                                         ["policy_question", "policy_question"])
        self.assertEqual([problem["code"] for problem in problems], ["G9_PATH_UNKNOWN"])
        self.assertEqual(([item["path_id"] for item in answer["items"]], dropped), (["nowhere", "none"], []))

    def test_the_verification_entries_are_entries_of_the_path_catalogue(self):
        for path in (*code_copy.VERIFY_PATHS.values(), code_copy.VERIFY_PATH_GENERAL):
            with self.subTest(path=path):
                self.assertIn(path, code_copy.PATHS)
        # B7: a comparison is a policy question's subkind, so one key covers both.
        self.assertEqual(set(code_copy.VERIFY_PATHS), {"policy_question"})
        self.assertIn("policy_question", respond.ANSWER_KINDS)


class PromptSaysWhatTheCodeDoesTests(unittest.TestCase):
    def test_the_prompt_asks_a_path_for_every_gap_and_refusal(self):
        self.assertIn("gap 和 refused 的事项都要用 path_id", respond.RESPOND_PROMPT)
        self.assertIn("不选 none", respond.RESPOND_PROMPT)

    def test_numbers_and_order_ids_from_the_facts_pass_and_the_prompt_no_longer_forbids_them(self):
        # G4 lets a number or date through only when the facts or a cited card have it; G6 only own order ids.
        prompt = respond.RESPOND_PROMPT
        self.assertNotIn("不写数字", prompt)
        self.assertIn("数字、日期和订单号只照抄 verified 或本轮 claims 所选规则卡里已有的", prompt)
        facts = {"order_id": "O1001", "delivered_at": "2026-09-15", "last_return_date": "2026-09-22"}
        ctx = guards.context(own_order_ids=["O1001"], order_facts=[facts])
        self.assertEqual(guards.check("核实记录写着 O1001 于 2026-09-15 送达，手头资料没提别的。", ctx), [])
        self.assertEqual(guards.guard_ids(guards.check("核实记录写着 O1001 于 2026-09-12 送达。", ctx)), ["G4"])
        self.assertEqual(guards.guard_ids(guards.check("别人名下的 O2001 我这边看不见。", ctx)), ["G6"])


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class DestinationOverHTTPTests(unittest.TestCase):
    from tests import test_graph_agent as fixtures
    setUp = fixtures.GraphHTTPTests.setUp
    stop = fixtures.GraphHTTPTests.stop
    request = fixtures.GraphHTTPTests.request
    start = fixtures.GraphHTTPTests.start
    send = fixtures.GraphHTTPTests.send

    def turn(self, message, answer, *, questions=(), request_id="m1"):
        plan = self.fixtures.planned([self.fixtures.item("policy_question", quote) for quote in questions])
        self.model.outputs.extend([plan, answer])
        status, result = self.send(self.start(), message, request_id)
        self.assertEqual((status, result["status"]), (200, "completed"))
        return result

    @staticmethod
    def tidied(result):
        return [part for entry in result["trace"] if entry["type"] == "structure_tidied" for part in entry["dropped"]]

    def test_a_policy_gap_without_a_path_shows_the_product_pages_notes_without_a_second_call(self):
        note = "我这边的资料没写到磕掉一角算不算影响完好。"
        result = self.turn("台灯底座磕了个角还算完好吗？", self.fixtures.answered(note, disposition="gap"),
                           questions=("台灯底座磕了个角还算完好吗？",))
        self.assertEqual(result["reply"], note + "\n\n" + POLICY_ENTRY)
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual([entry["type"] for entry in result["trace"] if entry["type"] == "structure_correction"], [])
        self.assertEqual(self.tidied(result), [{"code": "G9_PATH_MISSING", "part": "path:1", "path_id": "policy_page"}])
        self.assertEqual(result["graph"]["selection"]["items"][0]["path_id"], "policy_page")

    def test_a_refusal_whose_words_a_guard_drops_still_shows_the_codes_line_and_a_destination(self):
        result = self.turn("你能直接把钱退给我吗？",
                           self.fixtures.answered("商家稍后会联系你处理这件事。", disposition="refused"))
        self.assertEqual(result["reply"], code_copy.ITEM_LINES["refused"] + "\n\n" + GENERAL_ENTRY)
        self.assertEqual([(block["type"], block["author"]) for block in result["presentation"]["blocks"]],
                         [("text", "code"), ("path", "code")])
        self.assertEqual(self.tidied(result),
                         [{"code": "G9_PATH_MISSING", "part": "path:1", "path_id": "platform_human"}])
        self.assertFalse(result["answer_validation"]["fallback"])

    def test_a_path_the_answer_chose_is_kept_and_a_routing_gets_nothing_added(self):
        chosen = self.turn("这件事找谁？", self.fixtures.answered("要找卖这件商品的店。", disposition="refused",
                                                                  path="merchant_contact"))
        self.assertEqual(chosen["reply"], "要找卖这件商品的店。\n\n" + code_copy.PATH_LABEL
                         + code_copy.PATHS["merchant_contact"])
        self.assertEqual(self.tidied(chosen), [])
        routed = self.turn("改地址去哪里？", self.fixtures.answered("", disposition="routed"), request_id="m2")
        self.assertEqual(routed["reply"], code_copy.ITEM_LINES["routed"])
        self.assertEqual(self.tidied(routed), [])

    def test_an_own_order_date_in_a_note_is_shown_as_the_facts_have_it(self):
        note = "核实记录写着 O1001 于 2026-09-15 送达；杯盖松动算哪一类，手头资料没提。"
        result = self.turn("保温杯杯盖松了算质量问题吗？", self.fixtures.answered(note, disposition="gap"),
                           questions=("保温杯杯盖松了算质量问题吗？",))
        self.assertEqual(result["reply"], note + "\n\n" + POLICY_ENTRY)
        self.assertEqual([hit for entry in result["trace"] if entry["type"] == "guards" for hit in entry["hits"]], [])


if __name__ == "__main__":
    unittest.main()
