"""B6 (first-principles plan K2): the whole card catalogue reaches every answer, whatever the planner asked.

Real loopback HTTP and SQLite with a release built from invented sources and a catalogue bound to it; the model is
scripted. These are protocol checks of what the answer is offered and how a cited card is shown, not answer quality.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from support_agent.catalog import CATALOG_PATH, MAX_MODEL_VIEW_CHARS, model_view, model_view_chars
from support_agent.policy import PublicPolicyConfig, load_policy_knowledge, resolve_policy_config
from tests import test_graph_agent as fixture
from tests.test_public_policy_bundles import bundle_fixture

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
STORE = Path(__file__).resolve().parents[1] / "var/knowledge-store"


def bound_catalogue(root, config):
    """Six invented cards bound to the invented release: five with a premise for each outcome, and one with two
    outcomes and no premises (shown to the answer, never citable)."""
    knowledge = load_policy_knowledge(config)
    sources = [key for key in knowledge.evidence if key.startswith("parent:")]
    sources += [key for key in knowledge.evidence if key.startswith("rule:")]
    cards = []
    for index in range(5):
        source = knowledge.evidence[sources[index]]
        cards.append({"id": f"invented_{index}", "title": f"合成卡片第{index}号", "scope": "只用于合成工程测试",
                      "conditions": [f"合成条件{index}"], "outcomes": [f"合成第{index}号结论。"],
                      "outcome_premises": [[f"合成第{index}号前提"]], "exceptions": [], "support_scope_ids": ["synthetic"],
                      "effect_domain": "general_guidance", "evidence_ids": [source["id"]],
                      "references": [{"evidence_id": source["id"], "parent_sha256": source["sha256"],
                                      "quotes": [source["text"][:12]]}]})
    unmapped = dict(cards[0], id="invented_unmapped", title="合成两分支卡片", outcomes=["分支一。", "分支二。"])
    unmapped.pop("outcome_premises")
    cards.append(unmapped)
    catalogue = {"schema_version": "graph-policy-cards-v1", "human_reviewed": False, "knowledge_release_id": "c" * 64,
                 "corpus_sha256": knowledge.metadata["corpus_sha256"], "cards": cards}
    path = Path(root) / "bound-cards.json"
    path.write_text(json.dumps(catalogue, ensure_ascii=False), encoding="utf-8")
    return path, cards


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class WholeCatalogueHTTPTests(unittest.TestCase):
    request = fixture.GraphHTTPTests.request
    start = fixture.GraphHTTPTests.start
    send = fixture.GraphHTTPTests.send
    stop = fixture.GraphHTTPTests.stop

    def setUp(self):
        from support_agent.service import create_server
        temporary = tempfile.TemporaryDirectory(prefix="whole-catalogue-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = PublicPolicyConfig(*bundle_fixture(root))
        self.catalogue, self.cards = bound_catalogue(root, self.config)
        self.model = fixture.ScriptedModel([])
        self.server = create_server(db_path=root / "graph.sqlite3", credentials={fixture.ALICE: "alice"}, port=0,
                                    model_factory=fixture.ScriptedFactory(self.model), public_policy_config=self.config,
                                    agent_options={"card_catalog_path": self.catalogue},
                                    log_path=root / "requests.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def answer_call(self):
        call = self.model.calls[-1]
        schema = call["tools"][0]["function"]["parameters"]
        enum = schema["properties"]["items"]["items"]["properties"]["claims"]["items"]["properties"]["card_id"]["enum"]
        return json.loads(call["messages"][1]["content"]), enum

    def test_every_answer_reads_the_same_whole_catalogue_whatever_the_planner_asked(self):
        # B7: whether the planner lists no item, a chit-chat item or a policy question, the answer reads the same cards.
        plans = [fixture.planned(items) for items in ([], [fixture.item("chit_chat", "这种情况怎么算？")],
                                                      [fixture.item("policy_question", "这种情况怎么算？")])]
        seen = []
        for index, plan in enumerate(plans):
            self.model.outputs.extend([plan, fixture.answered("")])
            result = self.send(self.start(), "这种情况怎么算？", f"whole-{index}")[1]
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["graph"]["nodes"], ["understand", "business", "policy", "answer"])
            self.assertFalse({"policy_bundle", "policy_query_defaulted"} & {row["type"] for row in result["trace"]})
            seen.append(self.answer_call())
        expected = model_view(self.cards)
        for context, enum in seen:
            self.assertEqual(context["policy_cards"], expected)
            self.assertNotIn("policy_retrieval", context)
            # Fixed per catalogue version: every card with a premise for some outcome; the unmapped one is shown only.
            self.assertEqual(enum, [f"invented_{index}" for index in range(5)])

    def test_a_card_the_question_shares_no_words_with_can_be_cited_and_shows_its_sources(self):
        claim = [{"card_id": "invented_3", "outcome_index": 0}]
        self.model.outputs.extend([fixture.planned(queries=["毫不相干的检索词"]), fixture.answered("", claims=claim)])
        result = self.send(self.start(), "麻烦讲讲这条规定。")[1]
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["reply"].startswith("合成第3号结论。\n适用条件：合成第3号前提。\n依据：合成卡片第3号"))
        blocks = [block for block in result["presentation"]["blocks"] if block["type"] == "policy_card"]
        self.assertEqual([(block["card_id"], block["author"]) for block in blocks], [("invented_3", "code")])
        self.assertEqual([card["citation_id"] for card in result["presentation"]["source_cards"]],
                         self.cards[3]["evidence_ids"])
        self.assertEqual(result["policy_status"], "answered")
        selection = next(row for row in result["trace"] if row["type"] == "answer_selection")
        self.assertEqual(selection["available_policy_card_ids"], [card["id"] for card in self.cards])

    def test_a_rules_question_whose_answer_fails_still_reports_the_unmet_policy_need(self):
        # The policy step decides whether the turn asks about the rules, so a failed answer call keeps that decision.
        broken = fixture.response("respond", {"next_step": ""})
        self.model.outputs.extend([fixture.planned(queries=["随便一个检索词"]), broken, broken])
        result = self.send(self.start(), "这种情况怎么算？")[1]
        self.assertEqual(result["status"], "model_error", result)
        self.assertEqual(result["policy_status"], "insufficient_evidence")
        self.assertIn("policy", result["graph"]["nodes"])

    def test_a_pure_business_turn_reads_no_card_words_and_calls_only_the_planner(self):
        self.model.outputs.append(fixture.planned(fixture.business(order="O1001", quote="办一下")))
        result = self.send(self.start(), "办一下 O1001")[1]
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn("policy", result["graph"]["nodes"])
        self.assertFalse(any(block["type"] == "policy_card" for block in result["presentation"]["blocks"]))
        self.assertEqual(result["policy_status"], "not_requested")


@unittest.skipUnless(GRAPH_AVAILABLE and STORE.exists(), "the local frozen release is not distributed with source")
class RepositoryCatalogueHTTPTests(WholeCatalogueHTTPTests):
    """The repository catalogue on its own release: the answer context carries all of it, under the cap."""

    def setUp(self):
        from support_agent.service import create_server
        temporary = tempfile.TemporaryDirectory(prefix="whole-catalogue-real-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = resolve_policy_config(STORE)
        self.cards = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["cards"]
        self.model = fixture.ScriptedModel([])
        self.server = create_server(db_path=root / "graph.sqlite3", credentials={fixture.ALICE: "alice"}, port=0,
                                    model_factory=fixture.ScriptedFactory(self.model), public_policy_config=self.config,
                                    log_path=root / "requests.jsonl")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def test_every_answer_reads_the_same_whole_catalogue_whatever_the_planner_asked(self):
        self.model.outputs.extend([fixture.planned(queries=["随便一个检索词"]), fixture.answered("")])
        result = self.send(self.start(), "这种情况怎么算？")[1]
        self.assertEqual(result["status"], "completed", result)
        context, enum = self.answer_call()
        self.assertEqual(context["policy_cards"], model_view(self.cards))
        self.assertEqual(len(context["policy_cards"]), 24)
        self.assertEqual(enum, [card["id"] for card in self.cards])
        self.assertLess(model_view_chars(self.cards), MAX_MODEL_VIEW_CHARS)
        self.assertEqual(len(json.dumps(context["policy_cards"], ensure_ascii=False)), model_view_chars(self.cards))

    def test_a_card_the_question_shares_no_words_with_can_be_cited_and_shows_its_sources(self):
        claim = [{"card_id": "pickup_carrier", "outcome_index": 0}]
        self.model.outputs.extend([fixture.planned(queries=["毫不相干的检索词"]), fixture.answered("", claims=claim)])
        result = self.send(self.start(), "麻烦讲讲这条规定。")[1]
        self.assertEqual(result["status"], "completed", result)
        card = next(card for card in self.cards if card["id"] == "pickup_carrier")
        self.assertEqual([source["citation_id"] for source in result["presentation"]["source_cards"]],
                         card["evidence_ids"])
        self.assertIn(card["outcomes"][0].strip(), result["reply"])


if __name__ == "__main__":
    unittest.main()
