"""Product HTTP routes: demo sign-in, customer workspace and admin-only knowledge base.

All data is invented or simulated; the keyword demo model stands in for the
LLM, so these checks cover transport, roles and versioning, not answer quality.
"""
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

from support_agent import copy as code_copy
from support_agent.product_api import ENVIRONMENT_NOTICE
from support_agent.service import create_server

ALICE, BOB = "product-alice", "product-bob"
HAS_GRAPH = importlib.util.find_spec("pydantic") is not None


class ProductServerCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="product-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def serve(self, **options):
        self.server = create_server(db_path=self.root / "product.sqlite3", port=0,
                                    credentials={ALICE: "alice", BOB: "bob"}, **options)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(stop)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        return self.server

    def request(self, method, path, body=None, token=None, *, raw=None, **extra):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        headers = {"Origin": self.origin}
        if token:
            headers["Authorization"] = "Bearer " + token
        encoded = raw
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra)
        try:
            connection.request(method, path, encoded, headers)
            response = connection.getresponse()
            payload = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                payload = json.loads(payload)
            return response.status, payload
        finally:
            connection.close()

    def login(self, identity):
        status, result = self.request("POST", "/demo/login", {"identity": identity})
        self.assertEqual(status, 200, result)
        return result["token"]


class DemoSignInAndWorkspaceTests(ProductServerCase):
    def test_demo_routes_do_not_exist_unless_the_server_enables_them(self):
        self.serve()
        self.assertEqual(self.request("GET", "/demo/identities")[0], 404)
        self.assertEqual(self.request("POST", "/demo/login", {"identity": "alice"})[0], 404)
        status, info = self.request("GET", "/client-info", token=ALICE)
        self.assertEqual(status, 200)
        self.assertFalse(info["demo_login"])
        self.assertFalse(info["knowledge_base"])
        self.assertEqual(info["role"], "customer")

    def test_demo_login_only_hands_out_existing_customer_identities(self):
        self.serve(demo_login=True)
        status, listing = self.request("GET", "/demo/identities")
        self.assertEqual(status, 200)
        self.assertEqual([row["identity"] for row in listing["identities"]], ["alice", "bob"])
        self.assertFalse(listing["knowledge_base"])
        # The sign-in page shows the same single environment notice as the chat page (B1).
        self.assertEqual(listing["notice"], ENVIRONMENT_NOTICE)
        self.assertEqual(self.login("alice"), ALICE)
        for body in ({"identity": "kb-admin"}, {"identity": "mallory"}):
            self.assertEqual(self.request("POST", "/demo/login", body)[0], 404)
        self.assertEqual(self.request("POST", "/demo/login", {"identity": "alice", "role": "admin"})[0], 400)
        self.assertEqual(self.request("POST", "/demo/login", {"identity": "alice"}, Origin="https://attacker.example")[0], 403)
        self.assertEqual(self.request("GET", "/kb/overview", token=ALICE)[1]["error"]["code"], "ADMIN_REQUIRED")

    @unittest.skipUnless(HAS_GRAPH, "optional graph runtime not installed")
    def test_customers_see_only_their_own_conversations_and_simulated_orders(self):
        self.serve(demo_login=True)
        _, session = self.request("POST", "/sessions", {}, token=ALICE)
        status, result = self.request("POST", f"/sessions/{session['session_id']}/messages",
                                      {"message": "O1001 原因：尺寸不合适", "request_id": "product-1"}, token=ALICE)
        self.assertEqual(status, 200, result)
        status, mine = self.request("GET", "/me/sessions", token=ALICE)
        self.assertEqual(status, 200)
        self.assertEqual(len(mine["sessions"]), 1)
        row = mine["sessions"][0]
        self.assertEqual((row["session_id"], row["title"], row["turns"]), (session["session_id"], "O1001 原因：尺寸不合适", 1))
        self.assertFalse(row["busy"])
        self.assertEqual(self.request("GET", "/me/sessions", token=BOB)[1]["sessions"], [])
        status, orders = self.request("GET", "/me/orders", token=ALICE)
        self.assertEqual(status, 200)
        self.assertEqual(len(orders["orders"]), 4)
        by_id = {order["order_id"]: order for order in orders["orders"]}
        self.assertTrue(by_id["O1001"]["return_eligible"])
        self.assertFalse(by_id["O1002"]["return_eligible"])
        self.assertGreater(by_id["O1002"]["days_since_delivery"], 7)
        self.assertNotIn("customer_id", by_id["O1001"])
        # B1: the one environment notice (with the demo business date) and a display verdict per order,
        # worded from the order's fields; B5d2: the same code sentences as the chat reply, no teaching note.
        self.assertEqual(orders["notice"], ENVIRONMENT_NOTICE)
        self.assertIn("演示业务日期是 2026-09-18", orders["notice"])
        self.assertEqual(by_id["O1002"]["eligibility_text"],
                         code_copy.NOT_RETURNABLE["outside_window"].format(last_date="2026-09-08"))
        self.assertEqual(by_id["O1001"]["eligibility_text"], code_copy.RETURNABLE.format(last_date="2026-09-22"))
        self.assertNotIn("teaching_rule_note", by_id["O1001"])
        self.assertEqual({order["order_id"] for order in self.request("GET", "/me/orders", token=BOB)[1]["orders"]},
                         {"O2001"})
        self.assertEqual(self.request("GET", "/me/orders")[0], 401)
        self.assertEqual(self.request("GET", "/me/unknown", token=ALICE)[0], 404)


@unittest.skipUnless(HAS_GRAPH, "optional graph runtime not installed")
class KnowledgeBaseHTTPTests(ProductServerCase):
    def setUp(self):
        super().setUp()
        from support_agent.demo_model import KeywordDemoModel
        from tests.test_kb_store import kb_fixture
        self.config, self.knowledge, baseline, self.items = kb_fixture(self.root)
        self.serve(demo_login=True, public_policy_config=self.config,
                   model_factory=KeywordDemoModel, kb_store=self.root / "kb", kb_baseline_catalog=baseline)
        self.admin = self.login("kb-admin")

    def extra_card(self, **changes):
        card = {"id": "fixture_extra", "title": "合成附加规则", "scope": "仅限合成工程测试", "effect_domain": "return_items",
                "conditions": ["合成附加条件"], "outcomes": ["合成附加规则时适用合成结果"], "outcome_premises": [["合成附加前提"]],
                "exceptions": [], "references": [{"evidence_id": self.items["table"]["id"], "quotes": ["合成运费表"]}]}
        card.update(changes)
        return card

    def ask(self, message, request_id):
        _, session = self.request("POST", "/sessions", {}, token=ALICE)
        status, result = self.request("POST", f"/sessions/{session['session_id']}/messages",
                                      {"message": message, "request_id": request_id}, token=ALICE)
        self.assertEqual(status, 200, result)
        return result

    def test_roles_are_separate_and_admin_tokens_never_reach_customer_routes(self):
        status, listing = self.request("GET", "/demo/identities")
        self.assertEqual([row["identity"] for row in listing["identities"]], ["alice", "bob", "kb-admin"])
        self.assertNotIn(self.admin, (ALICE, BOB))
        self.assertEqual(self.request("GET", "/kb/overview", token=ALICE)[0], 403)
        self.assertEqual(self.request("POST", "/sessions", {}, token=self.admin)[1]["error"]["code"], "CUSTOMER_REQUIRED")
        self.assertEqual(self.request("GET", "/me/orders", token=self.admin)[0], 403)
        status, info = self.request("GET", "/client-info", token=self.admin)
        self.assertEqual((status, info["role"], info["knowledge_base"]), (200, "admin", True))
        status, overview = self.request("GET", "/kb/overview", token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual((overview["version_count"], overview["draft"]["dirty"], overview["draft_card_count"]), (1, False, 2))
        self.assertEqual(overview["ai_drafting"], "template")
        self.assertIn("不证明规则正确", overview["scope_notice"])

    def test_card_checks_report_field_errors_without_writing_the_draft(self):
        bad = self.extra_card(references=[{"evidence_id": self.items["table"]["id"], "quotes": ["原文里没有这句"]}])
        status, result = self.request("POST", "/kb/draft/cards/check", {"card": bad}, token=self.admin)
        self.assertEqual(status, 422)
        self.assertEqual(result["error"]["code"], "KB_VALIDATION_FAILED")
        self.assertEqual(result["error"]["details"][0]["field"], "references[0].quotes[0]")
        self.assertEqual(self.request("POST", "/kb/draft/cards/check", {"card": self.extra_card()}, token=self.admin)[0], 200)
        self.assertFalse(self.request("GET", "/kb/overview", token=self.admin)[1]["draft"]["dirty"])
        self.assertEqual(self.request("POST", "/kb/draft/cards", {"card": self.extra_card(), "x": 1}, token=self.admin)[0], 400)
        self.assertEqual(self.request("GET", "/kb/cards/missing_card?scope=draft", token=self.admin)[0], 404)
        self.assertEqual(self.request("GET", "/kb/cards?scope=everything", token=self.admin)[0], 400)

    def oversized_status(self, path, body, token):
        """POST only the headers of an oversized JSON body and return the status.

        The server rejects on Content-Length before reading any body and then closes the
        connection. Sending the body as well races that close on Windows (WinError 10053,
        registered as flaky in refactor-s1c-batch-record.json known_flaky_tests).
        """
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        try:
            connection.putrequest("POST", path)
            for name, value in (("Origin", self.origin), ("Authorization", "Bearer " + token),
                                ("Content-Type", "application/json"), ("Content-Length", str(size))):
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, payload["error"]["code"]
        finally:
            connection.close()

    def test_upload_body_limit_is_larger_only_for_administrator_uploads(self):
        text = "一、合成大文档\n" + "合成上传正文，用于测试请求体上限。\n\n" * 3000
        body = {"title": "合成大文档", "text": text}
        self.assertGreater(len(json.dumps(body, ensure_ascii=False).encode("utf-8")), 65536)
        self.assertEqual(self.oversized_status("/kb/uploads", body, ALICE), (413, "BODY_TOO_LARGE"))
        self.assertEqual(self.oversized_status("/kb/draft/cards", {"card": self.extra_card(scope=text[:70000])},
                                               self.admin), (413, "BODY_TOO_LARGE"))
        status, result = self.request("POST", "/kb/uploads", body, token=self.admin)
        self.assertEqual(status, 201, result)
        self.assertGreater(len(result["document"]["sections"]), 1)

    def test_upload_draft_publish_answer_and_rollback_end_to_end(self):
        status, upload = self.request("POST", "/kb/uploads", {"title": "合成店铺说明", "url": "https://help.example.com/p",
            "text": "一、配件退换\n合成店铺的配件签收后七天内未使用且包装完整的，可以申请退换。"}, token=self.admin)
        self.assertEqual(status, 201, upload)
        identifier = f"upload:{upload['document']['document_id']}#s01"
        status, sources = self.request("GET", "/kb/sources?scope=draft", token=self.admin)
        self.assertEqual(sources["documents"][0]["kind"], "merchant_upload")
        status, source = self.request("GET", "/kb/sources/" + quote(identifier, safe="") + "?scope=draft", token=self.admin)
        self.assertEqual((status, source["cited_by"]), (200, []))
        self.assertEqual(self.request("GET", "/kb/sources/" + quote(identifier, safe="") + "?scope=active",
                                      token=self.admin)[0], 404)
        status, drafted = self.request("POST", "/kb/ai-draft", {"evidence_ids": [identifier]}, token=self.admin)
        self.assertEqual((status, drafted["generator"]), (200, "offline_template"))
        self.assertTrue(all(quote_text in source["source"]["text"]
                            for quote_text in drafted["card"]["references"][0]["quotes"]))
        card = self.extra_card(references=[{"evidence_id": identifier, "quotes": ["签收后七天内未使用且包装完整的"]}])
        status, saved = self.request("POST", "/kb/draft/cards", {"card": card}, token=self.admin)
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.request("POST", f"/kb/uploads/{upload['document']['document_id']}/delete", {},
                                      token=self.admin)[0], 422)
        # B6: the knowledge base previews the model view (the whole catalogue the answer reads), not a search.
        status, preview = self.request("GET", "/kb/model-view?scope=draft", token=self.admin)
        self.assertEqual(status, 200, preview)
        self.assertEqual([card["id"] for card in preview["view"]], ["fixture_gift", "fixture_notice", "fixture_extra"])
        active_preview = self.request("GET", "/kb/model-view?scope=active", token=self.admin)[1]
        self.assertNotIn("fixture_extra", [card["id"] for card in active_preview["view"]])
        self.assertEqual(self.request("POST", "/kb/retrieval-preview", {"query": "合成附加规则", "scope": "draft"},
                                      token=self.admin)[0], 404)
        before = self.ask("合成附加规则的政策是什么？", "before-publish")
        first_version = before["card_catalog"]["kb_version_id"]
        self.assertEqual(self.request("POST", "/kb/draft/publish", {"label": ""}, token=self.admin)[0], 422)
        status, published = self.request("POST", "/kb/draft/publish", {"label": "新增合成规则"}, token=self.admin)
        self.assertEqual(status, 200, published)
        second_version = published["version"]["version_id"]
        after = self.ask("合成附加规则的政策是什么？", "after-publish")
        self.assertEqual(after["card_catalog"]["kb_version_id"], second_version)
        cards = [block for block in after["presentation"]["blocks"] if block["type"] == "policy_card"]
        self.assertEqual(cards[0]["card_id"], "fixture_extra")
        self.assertEqual(cards[0]["references"][0]["evidence_id"], identifier)
        self.assertTrue(after["presentation"]["display_only"])
        self.assertIn(cards[0]["selected"][0]["outcome"], after["reply"])
        status, versions = self.request("GET", "/kb/versions", token=self.admin)
        self.assertEqual([row["number"] for row in versions["versions"]], [2, 1])
        status, diff = self.request("GET", f"/kb/versions/{second_version}/diff", token=self.admin)
        self.assertEqual([row["id"] for row in diff["diff"]["added"]], ["fixture_extra"])
        self.assertEqual(len(diff["diff"]["sources_added"]), 1)
        self.assertEqual(self.request("POST", f"/kb/versions/{first_version}/activate", {"reason": ""},
                                      token=self.admin)[0], 422)
        self.assertEqual(self.request("POST", f"/kb/versions/{'0' * 64}/activate", {"reason": "不存在"},
                                      token=self.admin)[0], 404)
        status, _ = self.request("POST", f"/kb/versions/{first_version}/activate", {"reason": "演示回滚"}, token=self.admin)
        self.assertEqual(status, 200)
        rolled = self.ask("合成附加规则的政策是什么？", "after-rollback")
        self.assertEqual(rolled["card_catalog"]["kb_version_id"], first_version)
        self.assertNotIn("fixture_extra", json.dumps(rolled["presentation"], ensure_ascii=False))
        status, info = self.request("GET", "/client-info", token=ALICE)
        self.assertEqual(info["knowledge_base_version"]["number"], 1)


@unittest.skipUnless(HAS_GRAPH, "optional graph runtime not installed")
class AppCommandTests(unittest.TestCase):
    def test_offline_app_starts_without_live_configuration_and_prints_no_tokens(self):
        from tests.test_cli_service import NO_LIVE_CONFIGURATION, PROJECT_ROOT
        with tempfile.TemporaryDirectory(prefix="product-app-") as temporary:
            data = Path(temporary) / "data"
            process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", NO_LIVE_CONFIGURATION, "app", "--port", "0",
                                        "--data-dir", str(data), "--knowledge-store", str(Path(temporary) / "missing")],
                                       cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            try:
                lines = []
                while not lines or lines[-1].rstrip() != "}":
                    line = process.stdout.readline()
                    self.assertTrue(line, process.stderr.read() if process.poll() is not None else "no banner")
                    lines.append(line)
                banner = json.loads("".join(lines))
                self.assertIn("offline_keyword_demo", banner["mode"])
                self.assertTrue(banner["notices"])
                port = int(banner["url"].rsplit(":", 1)[1])
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", "/demo/identities")
                identities = json.loads(connection.getresponse().read())
                connection.close()
                self.assertEqual([row["identity"] for row in identities["identities"]], ["alice", "bob"])
                self.assertFalse(identities["knowledge_base"])
                stored = json.loads((data / "credentials.json").read_text(encoding="utf-8"))
                tokens = [row["token"] for row in stored["tokens"]]
                self.assertEqual(len(tokens), 2)
                self.assertFalse(any(token in "".join(lines) for token in tokens))
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
