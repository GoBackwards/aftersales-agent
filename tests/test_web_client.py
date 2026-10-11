"""Browser transport boundaries, using only local scripted model execution."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from support_agent.service import create_server
from tests.test_graph_agent import GRAPH_AVAILABLE, ScriptedFactory, ScriptedModel, business, planned


class BrowserServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.server = create_server(db_path=self.directory / "browser.sqlite3", port=0,
                                    credentials={"browser-alice": "alice", "browser-bob": "bob"})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, token="browser-alice", **extra):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        headers = {"Origin": self.origin}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra)
        try:
            connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def test_static_assets_are_public_exact_paths_with_restrictive_headers(self):
        for path, text in (("/", "售后助手"), ("/kb", "知识库管理"),
                           ("/assets/common.js", "sessionStorage"), ("/assets/chat.js", "sa.pending.v1"),
                           ("/assets/kb.js", "/kb/overview"), ("/assets/base.css", "color-scheme")):
            status, body, headers = self.request("GET", path, token=None)
            self.assertEqual(status, 200)
            self.assertIn(text, body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("browser-alice", body)
        for path in ("/assets/../service.py", "/assets/%2e%2e/service.py", "/?token=browser-alice", "/assets/missing.js",
                     "/kb/", "/workbench/", "/chat.html", "/workbench", "/assets/app.js", "/assets/app.css"):
            self.assertEqual(self.request("GET", path, token=None)[0], 401)
        status, body, _ = self.request("GET", "/workbench")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")
        self.assertNotEqual(self.request("POST", "/", {})[0], 200)

    def test_info_requires_auth_maps_server_identity_and_does_not_construct_models(self):
        self.server.model_factory = lambda: self.fail("reading client metadata must not construct model")
        self.assertEqual(self.request("GET", "/client-info", token=None)[0], 401)
        status, info, _ = self.request("GET", "/client-info", token="browser-bob")
        self.assertEqual(status, 200)
        self.assertEqual(info["customer_id"], "bob")
        self.assertEqual(info["mode"], "offline_scripted_demo")
        self.assertEqual(info["business_data"], "simulated")
        # S3-3c: graph is the only conversation; client-info no longer names an entrypoint or version.
        # B4c: it hands the page the server's message limit.
        self.assertEqual(set(info), {"customer_id", "role", "knowledge_base", "knowledge_base_version", "demo_login",
                                     "mode", "knowledge_release", "business_data", "policy_data", "message_limit"})
        self.assertNotIn("token", info)

    def test_only_exact_same_origin_is_allowed_for_browser_requests(self):
        self.assertEqual(self.request("POST", "/sessions", {})[0], 201)
        for origin in ("null", "https://127.0.0.1:" + str(self.server.server_port),
                       "http://localhost:" + str(self.server.server_port), "https://attacker.example"):
            self.assertEqual(self.request("POST", "/sessions", {}, Origin=origin)[0], 403)
        self.assertEqual(self.request("GET", "/", token=None, **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("GET", "/", token=None, Host="attacker.example")[0], 400)

    @unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
    def test_history_returns_visible_final_replies_not_provider_transcript(self):
        _, session, _ = self.request("POST", "/sessions", {})
        sid = session["session_id"]
        _, result, _ = self.request("POST", f"/sessions/{sid}/messages", {"message": "O1001 原因：尺寸不合适", "request_id": "browser-message"})
        status, history, _ = self.request("GET", f"/sessions/{sid}/history")
        self.assertEqual(status, 200)
        self.assertEqual(history["session"]["card"], result["session"]["card"])
        self.assertEqual(len(history["entries"]), 1)
        entry = history["entries"][0]
        self.assertEqual(entry["user_message"], "O1001 原因：尺寸不合适")
        self.assertEqual(entry["response"]["reply"], result["reply"])
        self.assertNotIn("trace", entry["response"])
        self.assertNotIn("public_policy_results", entry["response"])
        self.assertNotIn("tool_calls", entry["response"])
        self.assertEqual(self.request("GET", f"/sessions/{sid}/history", token="browser-bob")[0], 404)
        self.assertEqual(self.request("GET", f"/sessions/{sid}/history", token=None)[0], 401)

    @unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
    def test_history_does_not_replay_api_and_retains_confirmation_and_current_state(self):
        self.server.model_factory = ScriptedFactory(ScriptedModel([planned(business(order="O1001"))]))
        _, session, _ = self.request("POST", "/sessions", {})
        sid = session["session_id"]
        _, result, _ = self.request("POST", f"/sessions/{sid}/messages", {"message": "我要退货 O1001，不想要了", "request_id": "prepare"})
        card = result["session"]["card"]
        status, confirmed, _ = self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm",
                                            {"version": card["version"], "reason_code": "changed_mind",
                                             "reason_text": "", "request_id": "confirm"})
        self.assertEqual(status, 200)
        self.server.model_factory = lambda: self.fail("history must not initialize provider")
        before = result["session"]["execution_totals"]
        _, history, _ = self.request("GET", f"/sessions/{sid}/history")
        self.assertEqual(history["session"]["execution_totals"], before)
        self.assertEqual(history["session"]["card"]["status"], "consumed")
        self.assertEqual([item["entry"] for item in history["entries"]], ["turn", "card_confirm"])
        self.assertTrue(history["session"]["return_request"])

    def test_conversation_version_is_no_longer_a_server_option(self):
        target = self.directory / "invalid.sqlite3"
        for version in ("graph", "m2", "unknown"):
            with self.assertRaises(TypeError):
                create_server(db_path=target, credentials={"test": "alice"}, port=0, conversation_version=version)
            self.assertFalse(target.exists())


class ProductPageSourceTests(unittest.TestCase):
    """Static guards for the product pages under the strict CSP."""
    WEB = Path(__file__).resolve().parents[1] / "support_agent" / "web"

    def test_scripts_never_build_markup_from_strings_or_evaluate_code(self):
        import re
        for name in ("common.js", "chat.js", "kb.js"):
            source = (self.WEB / name).read_text(encoding="utf-8")
            code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
            code = re.sub(r"(?m)^\s*//.*$", "", code)
            for pattern in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                with self.subTest(name=name, pattern=pattern):
                    self.assertNotIn(pattern, code)

    def test_progress_shows_no_policy_search_step(self):
        # S3-3c made policy retrieval an explicit step event instead of a reserved tool call; B6 deleted retrieval,
        # so nothing waits on a search and the page shows no such step (the trace drawer still lists the policy node).
        source = (self.WEB / "chat.js").read_text(encoding="utf-8")
        self.assertNotIn("step_started", source)
        self.assertNotIn("search_public_policy", source)
        self.assertNotIn("检索", source)

    def test_pages_load_only_same_origin_assets_without_inline_code_or_styles(self):
        import re
        for name in ("chat.html", "kb.html"):
            page = (self.WEB / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotRegex(page, r"<script(?![^>]*\bsrc=\"/assets/)")
                self.assertNotIn("style=", page)
                self.assertNotRegex(page, r"\son[a-z]+=")
                for url in re.findall(r'(?:src|href)="([^"]+)"', page):
                    self.assertTrue(url.startswith("/assets/"), url)


if __name__ == "__main__":
    unittest.main()
