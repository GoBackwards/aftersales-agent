"""The source-publisher list (G10, D8) lives in the published catalogue and the knowledge base keeps and edits it (B5g;
plan §8.6 B5g row).

R111/R113 found G10 off in the local demo app and unreachable by a release: the app reads the knowledge base's published
catalogue, whose versions had no ``source_publishers``, and the editor could not add a top-level field. B5g, as
behaviour: a draft and a publish keep the field; the editor sets it under the runtime loader's own rule (each name word
for word in the cards' quotes); while the active catalogue has none the guard stays off, the turn never borrows the
repository file's list, and the administrator page says so in one line; a release that adds the names turns the guard
on for the next turn. Invented sources and names; scripted models over loopback HTTP; mechanics, not answer quality.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from support_agent.catalog import CATALOG_PATH, catalogue, publisher_problems

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
APP = Path(__file__).resolve().parents[1]
NAME = "合成工程"   # in the fixture's quote "这是合成工程测试的售后说明文字" (tests/test_kb_store.kb_fixture)


class PublisherRuleTests(unittest.TestCase):
    def test_the_one_rule_names_the_field_of_each_problem(self):
        base = {"cards": [{"references": [{"quotes": ["合成商城的退换说明"]}]}]}
        self.assertEqual(publisher_problems({**base, "source_publishers": ["合成商城"]}), [])
        self.assertEqual([field for field, _ in publisher_problems({**base, "source_publishers": ["合成商城", "别处"]})],
                         ["source_publishers[1]"])
        self.assertEqual([field for field, _ in publisher_problems({**base, "source_publishers": ["合成商城"] * 2})],
                         ["source_publishers"])
        self.assertEqual([field for field, _ in publisher_problems({**base, "source_publishers": "合成商城"})],
                         ["source_publishers"])


class PublisherStoreTests(unittest.TestCase):
    def setUp(self):
        from support_agent.kb_store import KBValidationError
        from support_agent.service import _knowledge_base
        from tests.test_kb_store import kb_fixture, proposal_for
        temporary = tempfile.TemporaryDirectory(prefix="kb-publishers-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.error, self.proposal_for = KBValidationError, proposal_for
        self.config, self.knowledge, baseline, _ = kb_fixture(self.root)
        self.store = _knowledge_base(self.root / "kb", self.config, self.knowledge, baseline)

    def test_names_are_set_in_the_draft_under_the_loaders_rule_and_a_publish_keeps_them(self):
        self.assertNotIn("source_publishers", self.store.active_catalog())
        with self.assertRaises(self.error) as caught:
            self.store.set_publishers(["原文里没有的店"])
        self.assertEqual(caught.exception.errors[0]["field"], "source_publishers[0]")
        self.assertFalse(self.store.draft_status()["dirty"])
        self.assertEqual(self.store.set_publishers([" " + NAME + " "]), [NAME])
        status = self.store.draft_status()
        self.assertTrue(status["dirty"])
        self.assertEqual(status["diff"]["publishers"], {"before": [], "after": [NAME]})
        # A card edit keeps the draft's top-level field; the publish stores it in the new immutable version.
        card = next(card for card in self.store.draft()["catalog"]["cards"] if card["id"] == "fixture_gift")
        self.store.upsert_card(self.proposal_for(card, title="合成赠品条件（修订）", replaces="fixture_gift"),
                               author="kb-admin")
        self.assertEqual(self.store.draft()["catalog"]["source_publishers"], [NAME])
        published = self.store.publish(label="登记来源发布方", author="kb-admin")
        path = self.store.active_catalog_path()
        self.assertEqual(path.stem, published["version"]["version_id"])
        self.assertEqual(catalogue(path)["source_publishers"], [NAME])
        self.assertEqual(self.store.diff(published["version"]["version_id"])["diff"]["publishers"],
                         {"before": [], "after": [NAME]})
        # Clearing the names (the guard off again) is an unpublished draft change like any other.
        self.assertEqual(self.store.set_publishers([]), [])
        self.assertEqual(self.store.draft_status()["diff"]["publishers"], {"before": [NAME], "after": []})


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class PublisherReleaseEndToEndTests(unittest.TestCase):
    from tests.test_graph_product_api import ProductServerCase as product
    serve, request, login = product.serve, product.request, product.login

    def setUp(self):
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel
        from tests.test_kb_store import kb_fixture
        self.product.setUp(self)
        self.config, _, baseline, _ = kb_fixture(self.root)
        self.model = ScriptedModel([])
        self.serve(demo_login=True, public_policy_config=self.config, model_factory=ScriptedFactory(self.model),
                   kb_store=self.root / "kb", kb_baseline_catalog=baseline)
        self.admin = self.login("kb-admin")
        self.customer = self.login("alice")

    def ask(self, note, request_id):
        from tests.test_graph_agent import answered, planned
        self.model.outputs.extend([planned(), answered(note, disposition="gap", path="policy_page")])
        _, session = self.request("POST", "/sessions", {}, token=self.customer)
        status, result = self.request("POST", f"/sessions/{session['session_id']}/messages",
                                      {"message": "这个说明在哪儿能看全？", "request_id": request_id}, token=self.customer)
        self.assertEqual((status, result["status"]), (200, "completed"))
        return result

    @staticmethod
    def hits(result):
        return [(hit["code"], hit["part"]) for entry in result["trace"] if entry["type"] == "guards"
                for hit in entry["hits"]]

    def test_a_release_that_adds_the_names_turns_the_guard_on_and_nothing_borrows_the_repository_list(self):
        repository = catalogue(CATALOG_PATH)["source_publishers"]
        self.assertTrue(repository)
        note = f"可以去{repository[0]}或{NAME}的页面看看。"
        status, overview = self.request("GET", "/kb/overview", token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(overview["source_publishers"], {"active": [], "draft": []})
        self.assertTrue(overview["publisher_warning"])
        self.assertNotIn("\n", overview["publisher_warning"])
        # The active catalogue names no publisher: G10 stays off, and the repository file's list is not used instead.
        before = self.ask(note, "before-release")
        self.assertEqual(self.hits(before), [])
        self.assertIn(note, before["reply"])
        status, saved = self.request("POST", "/kb/draft/publishers", {"source_publishers": ["别处的店"]},
                                     token=self.admin)
        self.assertEqual((status, saved["error"]["details"][0]["field"]), (422, "source_publishers[0]"))
        self.assertEqual(self.request("POST", "/kb/draft/publishers", {"source_publishers": [NAME]},
                                      token=self.customer)[0], 403)
        status, saved = self.request("POST", "/kb/draft/publishers", {"source_publishers": [NAME]}, token=self.admin)
        self.assertEqual((status, saved["source_publishers"], saved["draft"]["dirty"]), (200, [NAME], True))
        status, published = self.request("POST", "/kb/draft/publish", {"label": "登记来源发布方"}, token=self.admin)
        self.assertEqual(status, 200, published)
        overview = self.request("GET", "/kb/overview", token=self.admin)[1]
        self.assertEqual((overview["source_publishers"]["active"], overview["publisher_warning"]), ([NAME], ""))
        after = self.ask(note, "after-release")
        self.assertEqual(after["card_catalog"]["kb_version_id"], published["version"]["version_id"])
        self.assertEqual(self.hits(after), [("G10_PUBLISHER_NAMED", "note:1")])
        self.assertNotIn(NAME, after["reply"])


class PublisherPageTests(unittest.TestCase):
    def test_the_knowledge_base_page_shows_the_warning_and_edits_the_names(self):
        page = (APP / "support_agent" / "web" / "kb.js").read_text(encoding="utf-8")
        for needle in ("ov.publisher_warning", "/kb/draft/publishers", "source_publishers", "diff.publishers"):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
