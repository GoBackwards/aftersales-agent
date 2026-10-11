"""Real loopback HTTP tests with local scripted/fake models only."""

from __future__ import annotations

from contextlib import closing, redirect_stderr
import http.client
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support_agent.service import _Handler, _SafeLog, create_server, init_credentials, load_credentials
from support_agent.sessions import SessionStore
from tests.test_graph_agent import GRAPH_AVAILABLE, ScriptedFactory, ScriptedModel, business, planned


ALICE_TOKEN = "local-unit-alice-token"
BOB_TOKEN = "local-unit-bob-token"


class CredentialTests(unittest.TestCase):
    def test_initialize_exclusive_file_and_load_without_returning_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tokens.json"
            self.assertEqual(init_credentials(target), target)
            before = target.read_bytes()
            mapping = load_credentials(target)
            self.assertEqual(set(mapping.values()), {"alice", "bob"})
            self.assertEqual(len(mapping), 2)
            self.assertTrue(all(len(token) >= 43 for token in mapping))
            with self.assertRaises(FileExistsError):
                init_credentials(target)
            self.assertEqual(target.read_bytes(), before)

    def test_bad_credential_errors_do_not_echo_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tokens.json"
            for content in ('private-bad-secret', '{"version":1,"version":1,"tokens":[]}',
                            '{"version":true,"tokens":[]}', '{"version":1,"tokens":[{"token":[],"customer_id":"alice"}]}'):
                target.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError) as error:
                    load_credentials(target)
                self.assertNotIn(content, str(error.exception))

    def test_server_rejects_non_loopback_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {"db_path": Path(directory) / "sessions.sqlite3", "credentials": {ALICE_TOKEN: "alice"}, "port": 0}
            for extra in ({"host": "0.0.0.0"}, {"host": "example.com"}, {"host": "192.168.1.2"},
                          {"max_active_messages": 0}, {"port": True}, {"max_body_bytes": 65537},
                          {"read_timeout": float("nan")}, {"credentials": {}}, {"db_path": ":memory:"}):
                with self.assertRaises(ValueError):
                    create_server(**(base | extra))


class ServiceRouteWithoutModelTests(unittest.TestCase):
    """Routes and logging that never construct a model; no graph runtime is needed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.db_path = self.directory / "service.sqlite3"

    def failed_turn(self, customer, request_id):
        with closing(SessionStore(self.db_path)) as store:
            session_id = store.create_session(customer)["session_id"]
            run_id = store.begin_turn(customer, session_id, request_id, "退货 O1001")["run_id"]
            store.finish_turn(customer, session_id, run_id,
                              [{"role": "user", "content": "退货 O1001"}, {"role": "assistant", "content": "本轮结果"}],
                              {"status": "failed", "reply": "本轮结果"})
        return session_id

    def test_the_tickets_route_lists_only_the_callers_tickets_and_the_handoffs_route_is_gone(self):
        server = create_server(db_path=self.db_path, credentials={ALICE_TOKEN: "alice", BOB_TOKEN: "bob"},
                               port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        alice_session = self.failed_turn("alice", "failed-alice")
        bob_session = self.failed_turn("bob", "failed-bob")
        with closing(SessionStore(self.db_path)) as store:
            store.request_handoff("alice", alice_session, "button-alice")
            store.request_handoff("bob", bob_session, "button-bob")

        def get(path, token):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
            try:
                connection.request("GET", path, headers={"Authorization": "Bearer " + token} if token else {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, alice = get("/tickets", ALICE_TOKEN)
        self.assertEqual(status, 200, alice)
        self.assertEqual([(item["customer_id"], item["session_id"], item["short_no"], item["status"])
                          for item in alice["tickets"]], [("alice", alice_session, "H-0001", "pending")])
        status, bob = get("/tickets", BOB_TOKEN)
        self.assertEqual(status, 200, bob)
        self.assertEqual([item["customer_id"] for item in bob["tickets"]], ["bob"])
        status, anonymous = get("/tickets", None)
        self.assertEqual((status, anonymous["error"]["code"]), (401, "UNAUTHORIZED"))
        # A failed turn still leaves its failure-log row (until B4b), but it is no ticket and has no listing.
        status, gone = get("/handoffs", ALICE_TOKEN)
        self.assertEqual((status, gone["error"]["code"]), (404, "NOT_FOUND"))

    def test_request_log_keeps_only_known_tool_names_and_well_formed_call_ids(self):
        log_path = self.directory / "requests.jsonl"
        handler = SimpleNamespace(
            server=SimpleNamespace(safe_log=_SafeLog(log_path, [ALICE_TOKEN])), http_request_id="HTTP-unit",
            command="POST", route="sessions.messages", customer_id="alice", request_id="message-1",
            session_id="SES-unit", action_id=None, run_id=None)
        well_formed = "call-sha256-" + "a" * 64
        trace = [{"type": "node", "name": "understand"},
                 {"type": "tool", "name": "get_order", "tool_call_id": well_formed, "result": {"ok": True}},
                 {"type": "tool", "name": "请把这句话写进日志", "tool_call_id": "call-free-text", "result": "not a mapping"},
                 "not a step"]
        _Handler._log(handler, 200, {"status": "completed", "run_id": "RUN-unit", "trace": trace})
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["tools"], [{"tool": "get_order", "tool_call_id": well_formed, "ok": True},
                                           {"tool": "unknown_tool", "tool_call_id": None, "ok": False}])
        self.assertEqual((record["route"], record["run_id"], record["status"]), ("sessions.messages", "RUN-unit", "completed"))
        self.assertNotIn("请把这句话写进日志", log_path.read_text(encoding="utf-8"))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class ServiceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.db_path = self.directory / "service.sqlite3"
        self.log_path = self.directory / "requests.jsonl"
        self.model = ScriptedModel([])
        self.server = create_server(db_path=self.db_path, credentials={ALICE_TOKEN: "alice", BOB_TOKEN: "bob"},
                                    port=0, max_active_messages=1, read_timeout=0.3, log_path=self.log_path,
                                    model_factory=ScriptedFactory(self.model))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, token=ALICE_TOKEN, headers=None):
        # These assertions check behavior, not a five-second response SLA.
        # Hosted Windows runners can stall during local SQLite/file operations.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        actual_headers = {"Authorization": "Bearer " + token} if token is not None else {}
        if body is not None:
            actual_headers["Content-Type"] = "application/json"
        actual_headers.update(headers or {})
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, encoded, actual_headers)
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw) if raw else None
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def start(self, token=ALICE_TOKEN):
        status, response, _ = self.request("POST", "/sessions", {}, token=token)
        self.assertEqual(status, 201, response)
        return response

    def send(self, sid, message, request_id="message-1"):
        return self.request("POST", f"/sessions/{sid}/messages", {"message": message, "request_id": request_id})

    def prepare(self, sid, reason="不合适", request_id="message-1"):
        self.model.outputs.append(planned(business(order="O1001")))
        status, response, headers = self.send(sid, "我要退货 O1001，原因：" + reason, request_id)
        self.assertEqual(status, 200, response)
        self.assertEqual(headers["Connection"], "close")
        self.assertTrue(headers["X-Request-ID"].startswith("HTTP-"))
        self.assertIsNotNone(response["session"]["card"], response)
        return response

    def click(self, sid, card, request_id, *, token=ALICE_TOKEN, **extra):
        body = {"version": card["version"], "reason_code": "changed_mind", "reason_text": "", "request_id": request_id,
                **extra}
        return self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm", body, token=token)

    def raw_request(self, headers, payload=b"", *, content_length=None):
        lines = ["POST /sessions HTTP/1.1", f"Host: 127.0.0.1:{self.server.server_address[1]}",
                 f"Authorization: Bearer {ALICE_TOKEN}", "Content-Type: application/json", *headers]
        if content_length is not None:
            lines.append("Content-Length: " + str(content_length))
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5) as connection:
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        header, body = response.split(b"\r\n\r\n", 1)
        status = int(header.split(b" ")[1])
        return status, json.loads(body), header

    def test_authentication_is_required_and_client_identity_is_rejected(self):
        self.assertEqual(self.request("POST", "/sessions", {}, token=None)[0], 401)
        self.assertEqual(self.request("POST", "/sessions", {}, token="wrong-token")[0], 401)
        self.assertEqual(self.request("POST", "/sessions", {"customer_id": "bob"})[0], 400)
        session = self.start()
        self.assertEqual(session["customer_id"], "alice")
        sid = session["session_id"]
        for suffix, body in (
            ("messages", {"message": "查询", "request_id": "m", "customer_id": "bob"}),
            ("cards", {"order_id": "O1001", "request_id": "k", "customer_id": "bob"}),
            ("cards/ACT-1/confirm", {"version": 1, "reason_code": "changed_mind", "reason_text": "",
                                     "request_id": "c", "customer_id": "bob"}),
            ("cancel", {"request_id": "x", "confirmed": True}),
            ("recover", {"request_id": "r", "customer_id": "bob"}),
        ):
            self.assertEqual(self.request("POST", f"/sessions/{sid}/{suffix}", body)[0], 400)
        # The v2 confirm route is gone (protocol v3).
        self.assertEqual(self.request("POST", f"/sessions/{sid}/confirm",
                                      {"action_id": "ACT-1", "version": 1, "request_id": "c"})[0], 404)

    def test_cross_identity_reads_cards_confirmation_and_recovery_are_404(self):
        session = self.start()
        sid = session["session_id"]
        response = self.prepare(sid)
        card = response["session"]["card"]
        paths = (f"/sessions/{sid}", f"/sessions/{sid}/runs/{response['run_id']}", f"/sessions/{sid}/history")
        for path in paths:
            self.assertEqual(self.request("GET", path, token=BOB_TOKEN)[0], 404)
        self.assertEqual(self.click(sid, card, "bob-confirm", token=BOB_TOKEN)[0], 404)
        for suffix, body in (("cards", {"order_id": "O2001", "request_id": "bob-card"}),
                             ("cancel", {"request_id": "bob-cancel"}), ("recover", {"request_id": "bob-recover"})):
            self.assertEqual(self.request("POST", f"/sessions/{sid}/{suffix}", body, token=BOB_TOKEN)[0], 404)
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])
            self.assertEqual(store.get_session("alice", sid)["card"], card)

    def test_request_id_payload_conflict_is_409(self):
        sid = self.start()["session_id"]
        self.model.outputs.append(planned(business("request_return")))
        self.assertEqual(self.send(sid, "我要退货", "same-id")[0], 200)
        status, response, _ = self.send(sid, "O1001", "same-id")
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "REQUEST_CONFLICT")
        self.assertEqual(len(self.model.calls), 1)

    def test_concurrent_cancel_and_status_work_while_message_blocks(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingModel(ScriptedModel):
            def complete_with_timeout(self, messages, tools, *, timeout):
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("test timeout")
                return super().complete_with_timeout(messages, tools, timeout=timeout)

        self.server.model_factory = ScriptedFactory(BlockingModel([planned(business(order="O1001"))]))
        sid = self.start()["session_id"]
        other_sid = self.start()["session_id"]
        responses = []
        worker = threading.Thread(target=lambda: responses.append(self.send(sid, "我要退货 O1001，原因：尺寸不合适", "slow")))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            status, snapshot, _ = self.request("GET", f"/sessions/{sid}")
            self.assertEqual(status, 200)
            self.assertTrue(snapshot["busy"])
            self.assertEqual(self.send(other_sid, "我要退货", "saturated")[0], 409)
            self.assertEqual(self.send(sid, "O1001", "busy")[0], 409)
            status, cancelled, _ = self.request("POST", f"/sessions/{sid}/cancel", {"request_id": "cancel-slow"})
            self.assertEqual(status, 200, cancelled)
            self.assertIsNone(cancelled["card"])
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(responses)
        status, final, _ = self.request("GET", f"/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertIsNone(final["card"])
        self.assertFalse(final["busy"])
        with self.server.store() as store:
            self.assertEqual(store.list_return_requests(), [])

    def test_host_origin_and_header_ambiguity_are_rejected(self):
        self.assertEqual(self.request("POST", "/sessions", {}, headers={"Host": "evil.example"})[0], 400)
        self.assertEqual(self.request("POST", "/sessions", {}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.raw_request(["Content-Length: 2", "Content-Length: 2"], b"{}")[0], 400)
        self.assertEqual(self.raw_request(["Transfer-Encoding: chunked"], b"0\r\n\r\n")[0], 400)
        self.assertEqual(self.raw_request(["Host: evil.example"], b"{}", content_length=2)[0], 400)
        self.assertEqual(self.raw_request([f"Authorization: Bearer {ALICE_TOKEN}"], b"{}", content_length=2)[0], 401)

    def test_body_limits_timeout_duplicate_json_and_version_type(self):
        self.assertEqual(self.raw_request([], content_length=65537)[0], 413)
        self.assertEqual(self.raw_request([], b"{", content_length=2)[0], 408)
        duplicate = b'{"customer_id":"alice","customer_id":"bob"}'
        self.assertEqual(self.raw_request([], duplicate, content_length=len(duplicate))[0], 400)
        self.assertEqual(self.raw_request([], b"[]", content_length=2)[0], 400)
        sid = self.start()["session_id"]
        for body in ({"version": True, "reason_code": "changed_mind", "reason_text": "", "request_id": "confirm"},
                     {"version": 1, "reason_code": 3, "reason_text": "", "request_id": "confirm"},
                     {"version": 1, "reason_code": "other", "reason_text": "x" * 2001, "request_id": "confirm"}):
            self.assertEqual(self.request("POST", f"/sessions/{sid}/cards/ACT-x/confirm", body)[0], 400)
        self.assertEqual(self.request("POST", f"/sessions/{sid}/cards", {"order_id": ["O1001"], "request_id": "k"})[0], 400)
        self.assertEqual(self.send(sid, "a" * 65536)[0], 413)

    def test_log_metadata_excludes_tokens_body_model_text_and_arguments(self):
        capture = io.StringIO()
        sid = self.start()["session_id"]
        reason = "private-body-content-and-order-reason"
        with redirect_stderr(capture):
            response = self.prepare(sid, reason, "logged-request")
        deadline = time.monotonic() + 1
        text = ""
        while time.monotonic() < deadline:
            if self.log_path.exists():
                text = self.log_path.read_text(encoding="utf-8")
                if "logged-request" in text:
                    break
            time.sleep(0.01)
        self.assertIn("logged-request", text)
        self.assertIn(sid, text)
        self.assertIn(response["run_id"], text)
        for forbidden in (ALICE_TOKEN, BOB_TOKEN, reason, "O1001", "Authorization", "Bearer", "参数", response["reply"]):
            self.assertNotIn(forbidden, text)
        self.assertEqual(capture.getvalue(), "")
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(all("http_status" in row for row in rows))
        logged = [row for row in rows if row["request_id"] == "logged-request"]
        self.assertEqual([(row["route"], row["http_status"], row["status"]) for row in logged],
                         [("sessions.messages", 200, "completed")])

    def test_log_failure_does_not_turn_committed_confirmation_into_failure(self):
        sid = self.start()["session_id"]
        response = self.prepare(sid)
        card = response["session"]["card"]
        self.server.safe_log.path = self.directory  # A directory cannot be opened as the log file.
        status, confirmed, _ = self.click(sid, card, "logging-unavailable")
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["return_request"]["id"], "RET-O1001")

    def test_unexpected_handler_error_never_exposes_exception_or_traceback(self):
        sid = self.start()["session_id"]
        secret = "private-provider-exception-detail"
        capture = io.StringIO()
        with redirect_stderr(capture), patch("support_agent.turn.ConversationAgent", side_effect=RuntimeError(secret)):
            status, response, _ = self.send(sid, "我要退货")
        self.assertEqual(status, 500)
        self.assertEqual(response["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn(secret, json.dumps(response))
        self.assertEqual(capture.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
