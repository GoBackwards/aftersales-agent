"""Authenticated loopback HTTP adapter for the simulated support service.

Bearer credentials map to identities only on the server. The same-origin local
browser client uses this API; it is not a public deployment. Request bodies and model
text are deliberately absent from its request log.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
from uuid import uuid4

from .models import LazyModel
from .product_api import ProductHTTPError, is_product_path, split_path
from .sessions import EVENT_TOOL_NAMES, SessionError, SessionStore


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PATH_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")
_LOG_TOOL_NAMES = EVENT_TOOL_NAMES  # the tools a graph run reserves; any other name is logged as unknown_tool
_NOT_FOUND = {"SESSION_NOT_FOUND", "RUN_NOT_FOUND", "ORDER_NOT_FOUND", "TICKET_NOT_FOUND"}
_BAD_REQUEST = {"INVALID_ARGUMENTS", "MISSING_FIELDS", "INVALID_REASON"}
_UNAVAILABLE = {"STORAGE_ERROR", "UNSUPPORTED_SCHEMA", "INVALID_SCHEMA", "INVALID_CLOCK"}
_WEB_ROOT = Path(__file__).with_name("web")
_STATIC_ASSETS = {"/": ("chat.html", "text/html; charset=utf-8"),
                  "/kb": ("kb.html", "text/html; charset=utf-8"),
                  "/assets/base.css": ("base.css", "text/css; charset=utf-8"),
                  "/assets/common.js": ("common.js", "text/javascript; charset=utf-8"),
                  "/assets/chat.js": ("chat.js", "text/javascript; charset=utf-8"),
                  "/assets/chat.css": ("chat.css", "text/css; charset=utf-8"),
                  "/assets/kb.js": ("kb.js", "text/javascript; charset=utf-8"),
                  "/assets/kb.css": ("kb.css", "text/css; charset=utf-8"),
                  "/assets/favicon.svg": ("favicon.svg", "image/svg+xml")}
_PUBLIC_PRODUCT_ROUTES = {("GET", "/demo/identities"), ("POST", "/demo/login")}
# B4c (D15): the longest chat message in characters (code points, as len() and the page's Array.from count them).
# Decided here only: /client-info hands it to the page, and a longer message is refused before any turn exists.
MESSAGE_LIMIT = 2000
MESSAGE_TOO_LONG_TEXT = f"这条消息超过 {MESSAGE_LIMIT} 字，没有处理。请删减后再发，或分成几条发送。"
# B4b: a failed turn declares its one next step (next_step) and, after two failed turns in a row, its ticket card.
_BROWSER_RESPONSE_FIELDS = {"schema_version", "status", "reply", "mode", "run_status",
                            "policy_status", "run_id", "request_id", "replayed", "presentation",
                            "knowledge_release", "conversation_version", "next_step", "ticket_card", "error",
                            "usage", "graph", "answer_validation", "confirmation", "card_catalog"}


def _browser_history(store, customer_id, session_id):
    """Read saved user turns and final public replies, never provider transcripts."""
    with store._transaction(read_only=True):
        snapshot = store._snapshot(customer_id, session_id)
        rows = store._connection.execute("""SELECT entry, request_id, payload, response, status
            FROM session_requests WHERE customer_id = ? AND session_id = ?
            ORDER BY rowid DESC LIMIT 201""", (customer_id, session_id)).fetchall()
        entries = []
        for row in reversed(rows[:200]):
            payload = json.loads(row["payload"])
            saved = json.loads(row["response"]) if row["response"] else {}
            response = {key: value for key, value in saved.items() if key in _BROWSER_RESPONSE_FIELDS}
            # Independent commands save snapshots rather than model responses; B3c: with several cards in a
            # session, the page also needs the card the click acted on (chat.js cardFolded). B3e: a "申请退货"
            # click also declares that card (return_card) and says whether it was new or re-shown. B4a: a ticket click
            # declares its ticket card (ticket_card) and says what it did to the ticket. B4d: a recovery repeats the
            # interrupted turn's next step and ticket card, which the history keeps with that turn only.
            if row["entry"] != "turn":
                response.pop("next_step", None)
                response.pop("ticket_card", None)
                response["reply"] = saved.get("last_reply", saved.get("reply", ""))
                response["card"] = saved.get("card")
                if row["entry"] == "card_prepare":
                    response.update(card_outcome=saved.get("card_outcome"), return_card=saved.get("return_card"))
                if row["entry"] in ("ticket_handoff", "ticket_withdraw"):
                    response.update(ticket_outcome=saved.get("ticket_outcome"), ticket_card=saved.get("ticket_card"))
            entries.append({"entry": row["entry"], "request_id": row["request_id"],
                            "user_message": payload.get("user_message", ""),
                            "status": row["status"], "response": response})
        return {"session": snapshot, "entries": entries, "truncated": len(rows) > 200,
                "history_limit": 200}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-JSON numeric constant")


def _credentials(mapping):
    if type(mapping) is not dict or not mapping:
        raise ValueError("至少配置一个服务器端测试凭据。")
    if any(type(token) is not str or not _TOKEN.fullmatch(token)
           or type(customer) is not str or not _IDENTIFIER.fullmatch(customer)
           for token, customer in mapping.items()):
        raise ValueError("服务器测试凭据格式无效。")
    return dict(mapping)


def init_credentials(path: str | Path) -> Path:
    """Create credentials exclusively; never return their secret values."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "tokens": [
        {"customer_id": customer, "token": secrets.token_urlsafe(32)}
        for customer in ("alice", "bob")
    ]}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        json.dump(document, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return path


def load_credentials(path: str | Path) -> dict[str, str]:
    """Read local credentials without printing tokens or exposing parse text."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"),
                             object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if (type(payload) is not dict or set(payload) != {"version", "tokens"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or type(payload["tokens"]) is not list or not payload["tokens"]):
            raise ValueError
        mapping = {}
        for entry in payload["tokens"]:
            if type(entry) is not dict or set(entry) != {"customer_id", "token"}:
                raise ValueError
            if type(entry["token"]) is not str or entry["token"] in mapping:
                raise ValueError
            mapping[entry["token"]] = entry["customer_id"]
        return _credentials(mapping)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("无法读取有效的本地服务凭据文件。") from None


class _HTTPError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class _SafeLog:
    def __init__(self, path, tokens):
        self.path = Path(path) if path is not None else None
        self.tokens = tuple(tokens)
        self.lock = threading.Lock()

    def identifier(self, value):
        # IDs are bounded metadata, not arbitrary request/model strings. Reject
        # a token if a caller deliberately places it in a request ID.
        if (type(value) is str and _IDENTIFIER.fullmatch(value)
                and not any(token in value for token in self.tokens)):
            return value
        return None

    def write(self, record):
        if self.path is None:
            return
        try:
            encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as output:
                    output.write(encoded + "\n")
        except (OSError, ValueError, TypeError):
            # A committed confirmation must not become a failed business result
            # merely because the optional diagnostic destination is unwritable.
            pass


class LocalServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Never let socket-worker exceptions print request details or tracebacks.
        self.safe_log.write({"event": "connection_error", "timestamp": datetime.now(timezone.utc).isoformat()})

    @contextmanager
    def store(self):
        store = SessionStore(self.db_path, **self.store_options)
        try:
            yield store
        finally:
            store.close()


def _demo_model():
    # Without a knowledge release, the graph runtime is imported when the first
    # model is constructed rather than at startup.
    from .demo_model import DemoModel
    return DemoModel()
_demo_model.mode = "offline_scripted_demo"   # LazyModel.mode reads it before construction


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalSupportService"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.read_timeout)
        self.http_request_id = "HTTP-" + uuid4().hex
        self.request_id = self.session_id = self.action_id = self.run_id = self.customer_id = None
        self.role = None
        self.route = "unmatched"

    def log_message(self, format, *args):
        pass

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler also calls this for malformed request lines and
        # unsupported methods. Do not reflect its raw messages or HTML payloads.
        self._send(code, {"ok": False, "error": {"code": "HTTP_REQUEST_REJECTED", "message": "HTTP 请求格式不受支持。"}})

    def _send(self, status, payload, *, content_type="application/json; charset=utf-8"):
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("X-Request-ID", self.http_request_id)
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _validate_origin(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.server.allowed_hosts:
            raise _HTTPError(400, "INVALID_HOST", "Host 必须对应当前本机服务地址与端口。")
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != "http://" + hosts[0].lower()):
            raise _HTTPError(403, "ORIGIN_NOT_ALLOWED", "只接受本机页面的同源请求。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise _HTTPError(403, "ORIGIN_NOT_ALLOWED", "只接受本机页面的同源请求。")

    def _authenticate(self):
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not auth[0].startswith("Bearer "):
            raise _HTTPError(401, "UNAUTHORIZED", "需要有效的本地服务 Bearer 凭据。")
        supplied = auth[0][7:]
        if not _TOKEN.fullmatch(supplied):
            raise _HTTPError(401, "UNAUTHORIZED", "需要有效的本地服务 Bearer 凭据。")
        customer = None
        for token, identity in self.server.credentials.items():
            if hmac.compare_digest(token, supplied):
                customer = identity
        role = "customer"
        for token, identity in self.server.admin_tokens.items():
            if hmac.compare_digest(token, supplied):
                customer, role = identity, "admin"
        if customer is None:
            raise _HTTPError(401, "UNAUTHORIZED", "需要有效的本地服务 Bearer 凭据。")
        self.customer_id, self.role = customer, role

    def _body(self):
        if self.headers.get_all("Transfer-Encoding", []):
            raise _HTTPError(400, "INVALID_FRAMING", "不支持 Transfer-Encoding。")
        if self.headers.get_all("Content-Encoding", []):
            raise _HTTPError(400, "INVALID_ENCODING", "不支持压缩请求体。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            raise _HTTPError(400, "INVALID_FRAMING", "Content-Length 必须是单个非负整数。")
        length = int(lengths[0]) if lengths else 0
        # Only an authenticated administrator may send a document-sized body.
        uploading = self.path == "/kb/uploads" and getattr(self, "role", None) == "admin"
        limit = self.server.kb_max_body_bytes if uploading else self.server.max_body_bytes
        if length > limit:
            raise _HTTPError(413, "BODY_TOO_LARGE", "请求体超过本地服务大小限制。")
        if self.command != "POST":
            if length:
                raise _HTTPError(400, "UNEXPECTED_BODY", "该接口不接受请求体。")
            return {}
        if len(lengths) != 1:
            raise _HTTPError(400, "INVALID_FRAMING", "POST 需要明确的 Content-Length。")
        if (len(self.headers.get_all("Content-Type", [])) != 1
                or self.headers.get_content_type().lower() != "application/json"):
            raise _HTTPError(400, "INVALID_CONTENT_TYPE", "请求体必须使用 application/json。")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise _HTTPError(400, "INCOMPLETE_BODY", "请求体不完整。")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (TimeoutError, socket.timeout):
            raise _HTTPError(408, "REQUEST_TIMEOUT", "读取请求超时。") from None
        except (ValueError, UnicodeError):
            raise _HTTPError(400, "INVALID_JSON", "请求体必须是有效且无重复字段的 JSON。") from None
        if type(payload) is not dict:
            raise _HTTPError(400, "INVALID_ARGUMENTS", "请求体必须为 JSON 对象。")
        return payload

    @staticmethod
    def _fields(body, fields):
        if set(body) != set(fields):
            raise _HTTPError(400, "INVALID_ARGUMENTS", "请求字段缺失或包含未允许字段。")

    def _request_identifier(self, body):
        value = body.get("request_id")
        if type(value) is not str or not _IDENTIFIER.fullmatch(value):
            raise _HTTPError(400, "INVALID_ARGUMENTS", "request_id 必须为 1 至 128 个允许的标识字符。")
        self.request_id = value
        return value

    def _product(self, body):
        from . import product_api
        path, query = split_path(self.path)
        self.route = "product." + path.strip("/").replace("/", ".")[:60]
        if (self.command, path) == ("GET", "/demo/identities"):
            return product_api.demo_identities(self.server)
        if (self.command, path) == ("POST", "/demo/login"):
            return product_api.demo_login(self.server, body)
        if path.startswith("/kb/"):
            if self.role != "admin":
                raise _HTTPError(403, "ADMIN_REQUIRED", "知识库管理需要管理员身份。")
            with self.server.kb_lock:
                return product_api.kb_dispatch(self, self.command, path, query, body)
        if self.role != "customer":
            raise _HTTPError(403, "CUSTOMER_REQUIRED", "该接口只接受买家身份。")
        if self.command == "GET" and path == "/me/sessions":
            with self.server.store() as store:
                with store._transaction(read_only=True):
                    return 200, product_api.list_sessions(store, self.customer_id)
        if self.command == "GET" and path == "/me/orders":
            with self.server.store() as store:
                with store._transaction(read_only=True):
                    return 200, product_api.list_orders(store, self.customer_id,
                                                        shipping_rule=product_api.return_shipping_rule(self.server))
        raise _HTTPError(404, "NOT_FOUND", "接口不存在。")

    def _dispatch(self, body):
        if is_product_path(split_path(self.path)[0]):
            return self._product(body)
        if self.role != "customer" and not (self.command == "GET" and self.path == "/client-info"):
            raise _HTTPError(403, "CUSTOMER_REQUIRED", "会话接口只接受买家身份。")
        if self.command == "GET" and self.path == "/client-info":
            self.route = "client.info"
            kb_version = None
            if self.server.kb is not None:
                pointer = self.server.kb.active_pointer()
                row = next((item for item in self.server.kb.versions() if item["active"]), {})
                kb_version = {"version_id": pointer["version_id"], "number": row.get("number"),
                              "label": row.get("label"), "activated_at": pointer.get("activated_at")}
            return 200, {"customer_id": self.customer_id, "role": self.role,
                         "knowledge_base": self.server.kb is not None, "knowledge_base_version": kb_version,
                         "demo_login": self.server.demo_login, "message_limit": MESSAGE_LIMIT,
                         "mode": self.server.client_mode,
                         "knowledge_release": (self.server.public_policy_config.release_metadata()
                                               if self.server.public_policy_config is not None else None),
                         "business_data": "simulated", "policy_data": "public_reference_only"}
        if self.command == "POST" and self.path == "/sessions":
            self.route = "sessions.create"
            self._fields(body, ())
            with self.server.store() as store:
                return 201, store.create_session(self.customer_id)
        if self.command == "GET" and self.path == "/tickets":
            # B4a: the caller's own human-service tickets (replaces GET /handoffs, the failure log listing).
            self.route = "tickets.list"
            with self.server.store() as store:
                return 200, {"tickets": store.list_tickets(self.customer_id)}
        run = re.fullmatch(r"/sessions/(" + _PATH_IDENTIFIER + r")/runs/(" + _PATH_IDENTIFIER + r")", self.path)
        if run and self.command == "GET":
            self.route, self.session_id, self.run_id = "runs.get", run[1], run[2]
            with self.server.store() as store:
                return 200, store.get_run(self.customer_id, self.session_id, self.run_id)
        history = re.fullmatch(r"/sessions/(" + _PATH_IDENTIFIER + r")/history", self.path)
        if history and self.command == "GET":
            self.route, self.session_id = "sessions.history", history[1]
            with self.server.store() as store:
                return 200, _browser_history(store, self.customer_id, self.session_id)
        click = re.fullmatch(r"/sessions/(" + _PATH_IDENTIFIER + r")/cards/(" + _PATH_IDENTIFIER + r")/(confirm|void)",
                             self.path)
        if click:
            if self.command != "POST":
                raise _HTTPError(405, "METHOD_NOT_ALLOWED", "该接口不接受此 HTTP 方法。")
            self.route, self.session_id, self.action_id = "cards." + click[3], click[1], click[2]
            if click[3] == "void":
                # The "暂不提交" button (B3b): voids this session's open card, no model call.
                self._fields(body, ("request_id",))
                self._request_identifier(body)
                with self.server.store() as store:
                    return 200, store.request_void(self.customer_id, self.session_id, self.action_id, self.request_id)
            # Confirmation protocol v3: the only path that writes a return request. Every check of the
            # session, the card, its owner and the reason is made inside the store's transaction.
            self._fields(body, ("version", "reason_code", "reason_text", "request_id"))
            self._request_identifier(body)
            if (type(body["version"]) is not int or body["version"] < 1
                    or type(body["reason_code"]) is not str or len(body["reason_code"]) > 64
                    or type(body["reason_text"]) is not str or len(body["reason_text"]) > 2000):
                raise _HTTPError(400, "INVALID_ARGUMENTS", "确认需要正整数 version，以及文本形式的 reason_code 和 reason_text。")
            with self.server.store() as store:
                return 200, store.confirm_card(self.customer_id, self.session_id, self.action_id, body["version"],
                                               body["reason_code"], body["reason_text"], self.request_id)
        withdraw = re.fullmatch(r"/sessions/(" + _PATH_IDENTIFIER + r")/tickets/(" + _PATH_IDENTIFIER + r")/withdraw",
                                self.path)
        if withdraw:
            # The ticket card's "撤回" (B4a): no model call, allowed while a turn runs.
            if self.command != "POST":
                raise _HTTPError(405, "METHOD_NOT_ALLOWED", "该接口不接受此 HTTP 方法。")
            self.route, self.session_id = "tickets.withdraw", withdraw[1]
            self._fields(body, ("request_id",))
            self._request_identifier(body)
            with self.server.store() as store:
                return 200, store.withdraw_ticket(self.customer_id, self.session_id, withdraw[2], self.request_id)
        session = re.fullmatch(r"/sessions/(" + _PATH_IDENTIFIER + r")(?:/(messages|cards|handoff|cancel|recover))?",
                               self.path)
        if not session:
            raise _HTTPError(404, "NOT_FOUND", "接口不存在。")
        self.session_id, operation = session[1], session[2]
        if operation is None and self.command == "GET":
            self.route = "sessions.get"
            with self.server.store() as store:
                return 200, store.get_session(self.customer_id, self.session_id)
        if self.command != "POST" or operation is None:
            raise _HTTPError(405, "METHOD_NOT_ALLOWED", "该接口不接受此 HTTP 方法。")
        self.route = "sessions." + operation
        if operation == "messages":
            self._fields(body, ("message", "request_id"))
            if type(body["message"]) is not str or not body["message"].strip():
                raise _HTTPError(400, "INVALID_ARGUMENTS", "message 必须为非空文本。")
            if len(body["message"]) > MESSAGE_LIMIT:
                # Not a turn: nothing is saved, the request id stays free and no run of failures counts it.
                raise _HTTPError(400, "MESSAGE_TOO_LONG", MESSAGE_TOO_LONG_TEXT)
            self._request_identifier(body)
            if not self.server.message_slots.acquire(blocking=False):
                raise _HTTPError(409, "SERVICE_BUSY", "消息并发达到上限，请稍后使用同一请求 ID 重试。")
            try:
                with self.server.store() as store:
                    # Ownership is checked before constructing a model or making
                    # a possible network request.
                    store.get_session(self.customer_id, self.session_id)
                    model = LazyModel(self.server.model_factory)
                    agent_options = dict(self.server.agent_options)
                    if self.server.public_policy_config is not None:
                        agent_options["public_policy_config"] = self.server.public_policy_config
                    from .turn import ConversationAgent
                    agent_type = ConversationAgent
                    agent_options["policy_knowledge"] = self.server.policy_knowledge
                    if self.server.kb is not None:
                        # Pin this turn to one immutable published catalog.
                        agent_options["card_catalog_path"] = self.server.kb.active_catalog_path()
                    response = agent_type(model, store, self.customer_id, **agent_options).send(
                        self.session_id, body["message"], self.request_id)
                    return 200, response
            finally:
                self.server.message_slots.release()
        if operation == "cards":
            # The "申请退货" button and candidate clicks: a card for one own order, no model call.
            self._fields(body, ("order_id", "request_id"))
            self._request_identifier(body)
            if type(body["order_id"]) is not str or not _IDENTIFIER.fullmatch(body["order_id"]):
                raise _HTTPError(400, "INVALID_ARGUMENTS", "order_id 必须为有效的订单编号。")
            with self.server.store() as store:
                return 200, store.request_card(self.customer_id, self.session_id, body["order_id"], self.request_id)
        self._fields(body, ("request_id",))
        self._request_identifier(body)
        with self.server.store() as store:
            # B4a: the persistent "找人工" button opens the session's ticket or appends to its open one (no model
            # call, allowed while a turn runs); cancel and recover control the run only.
            method = {"handoff": store.request_handoff, "cancel": store.cancel, "recover": store.recover}[operation]
            return 200, method(self.customer_id, self.session_id, self.request_id)

    def _log(self, status, response):
        snapshot = response.get("session", response)
        card = snapshot.get("card") if isinstance(snapshot, dict) else None
        card = card if isinstance(card, dict) else {}
        safe = self.server.safe_log.identifier
        record = {"event": "http_request", "timestamp": datetime.now(timezone.utc).isoformat(),
                  "http_request_id": self.http_request_id, "method": self.command,
                  "route": self.route, "http_status": status,
                  "customer_id": safe(self.customer_id), "request_id": safe(self.request_id),
                  "session_id": safe(snapshot.get("session_id", self.session_id)) if isinstance(snapshot, dict) else safe(self.session_id),
                  "action_id": safe(card.get("action_id", self.action_id)),
                  "run_id": safe(response.get("run_id", self.run_id)),
                  "status": safe(response.get("status", card.get("status"))),
                  "error_code": safe(response.get("error", {}).get("code")), "tools": []}
        for step in response.get("trace", []):
            if not isinstance(step, dict) or step.get("type") != "tool":
                continue
            result = step.get("result", {})
            tool_name = step.get("name")
            tool_call_id = step.get("tool_call_id")
            if type(tool_call_id) is not str or not re.fullmatch(r"call-sha256-[0-9a-f]{64}", tool_call_id):
                tool_call_id = None
            # A model can put arbitrary user text in an unknown function name;
            # being syntactically an identifier does not make it safe metadata.
            record["tools"].append({"tool": tool_name if type(tool_name) is str and tool_name in _LOG_TOOL_NAMES else "unknown_tool",
                                    "tool_call_id": tool_call_id,
                                    "ok": result.get("ok") is True if isinstance(result, dict) else False})
        self.server.safe_log.write(record)

    def _handle(self):
        status, response = 500, {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "本地服务暂时无法完成请求。"}}
        try:
            self._validate_origin()
            if self.command in {"GET", "HEAD"} and self.path in _STATIC_ASSETS:
                self._body()
                self.route = "client.asset"
                filename, content_type = _STATIC_ASSETS[self.path]
                self._send(200, (_WEB_ROOT / filename).read_bytes(), content_type=content_type)
                self._log(200, {})
                return
            if (self.command, self.path) in _PUBLIC_PRODUCT_ROUTES:
                self.role = "anonymous"
                body = self._body()
                status, response = self._product(body)
            else:
                self._authenticate()
                body = self._body()
                status, response = self._dispatch(body)
        except _HTTPError as error:
            status, response = error.status, {"ok": False, "error": {"code": error.code, "message": error.message}}
        except ProductHTTPError as error:
            status, response = error.status, {"ok": False, "error": {"code": error.code, "message": error.message}}
            if error.details is not None:
                response["error"]["details"] = error.details
        except SessionError as error:
            status = 404 if error.code in _NOT_FOUND else 400 if error.code in _BAD_REQUEST else 503 if error.code in _UNAVAILABLE else 409
            response = {"ok": False, "error": {"code": error.code, "message": error.public_message}}
        except Exception:
            pass
        self._send(status, response)
        self._log(status, response)

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def create_server(*, db_path: str | Path, credentials: dict[str, str], host: str = "127.0.0.1",
                  port: int = 8765, model_factory=None, max_active_messages: int = 4,
                  log_path: str | Path | None = None, agent_options: dict | None = None,
                  store_options: dict | None = None, read_timeout: float = 5.0,
                  max_body_bytes: int = 65536, public_policy_config=None, demo_login: bool = False,
                  kb_store: str | Path | None = None, kb_max_body_bytes: int = 600_000,
                  kb_baseline_catalog: str | Path | None = None) -> LocalServiceServer:
    """Initialize storage and return a server; caller owns serve/shutdown/close."""
    credentials = _credentials(credentials)
    host = "127.0.0.1" if host == "localhost" else host
    try:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError("服务仅允许绑定明确的本机 loopback 地址。") from None
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须为 0 至 65535。")
    if type(max_active_messages) is not int or max_active_messages < 1:
        raise ValueError("消息并发上限必须为正整数。")
    if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= 65536:
        raise ValueError("请求体限制必须在 1 至 65536 字节内。")
    if type(read_timeout) not in (int, float) or not 0 < read_timeout <= 5:
        raise ValueError("请求读取超时必须在 0 至 5 秒内。")
    if str(db_path) == ":memory:":
        raise ValueError("多请求服务需要持久化 SQLite 文件。")
    if model_factory is not None and not callable(model_factory):
        raise ValueError("model_factory 必须可调用。")
    policy_knowledge = None
    if public_policy_config is not None:
        from .policy import PublicPolicyConfig, load_policy_knowledge
        if not isinstance(public_policy_config, PublicPolicyConfig):
            raise ValueError("官方政策配置必须使用 PublicPolicyConfig。")
        # Reject missing/tampered sources before opening storage or a listener.
        # Credentials and API configuration remain lazy on this offline path.
        policy_knowledge = load_policy_knowledge(public_policy_config)
        # With a knowledge release (the product app) a missing graph runtime
        # still fails at startup, as before.
        from .demo_model import DemoModel
        demo_factory = DemoModel
    else:
        demo_factory = _demo_model
    if type(demo_login) is not bool:
        raise ValueError("demo_login 必须为布尔值。")
    if type(kb_max_body_bytes) is not int or not 1 <= kb_max_body_bytes <= 2_000_000:
        raise ValueError("知识库上传大小限制必须在 1 至 2000000 字节内。")
    kb = None
    if kb_store is not None:
        if policy_knowledge is None:
            raise ValueError("知识库管理需要固定的官方政策知识发布。")
        kb = _knowledge_base(kb_store, public_policy_config, policy_knowledge, kb_baseline_catalog)
    options = dict(store_options or {})
    store = SessionStore(db_path, **options)
    try:
        recovered = store.recover_expired_runs()
    finally:
        store.close()
    server_type = LocalServiceServer
    if address.version == 6:
        class IPv6LocalServiceServer(LocalServiceServer):
            address_family = socket.AF_INET6
        server_type = IPv6LocalServiceServer
    server = server_type((str(address), port), _Handler)
    server.db_path = Path(db_path)
    server.credentials = credentials
    server.model_factory = model_factory or demo_factory
    server.client_mode = "offline_scripted_demo" if model_factory is None else getattr(model_factory, "mode", "custom_model")
    server.agent_options = dict(agent_options or {})
    server.public_policy_config = public_policy_config
    server.policy_knowledge = policy_knowledge
    server.store_options = options
    server.message_slots = threading.BoundedSemaphore(max_active_messages)
    server.read_timeout = read_timeout
    server.max_body_bytes = max_body_bytes
    server.demo_login = demo_login
    server.kb = kb
    server.kb_lock = threading.Lock()
    server.kb_max_body_bytes = kb_max_body_bytes
    # The administrator token exists only in memory for this process. It is
    # handed out by demo sign-in and never mixed with customer identities.
    server.admin_tokens = {secrets.token_urlsafe(32): "kb-admin"} if kb is not None else {}
    server.safe_log = _SafeLog(log_path, [*credentials, *server.admin_tokens])
    server.recovered_on_startup = len(recovered)
    bound_port = server.server_address[1]
    literal = f"[{address}]" if address.version == 6 else str(address)
    server.allowed_hosts = {f"{literal}:{bound_port}"}
    if str(address) in {"127.0.0.1", "::1"}:
        server.allowed_hosts.add(f"localhost:{bound_port}")
    return server


def _knowledge_base(directory, config, knowledge, baseline=None):
    """Open the card store pinned to the running release; validate like a turn.

    ``baseline`` seeds version 1 only when the store is new; it defaults to the
    repository card catalog bound to the frozen release.
    """
    from .policy import PolicyAdapter, _thaw
    from .catalog import CATALOG_PATH
    from .kb_store import KnowledgeBaseStore

    def runtime_check(path):
        adapter = PolicyAdapter(config, knowledge=knowledge, card_catalog=path)
        status = adapter._metadata.get("policy_card_catalog", {}).get("status")
        if status != "enabled":
            raise ValueError("GRAPH_CARD_CATALOG_NOT_ENABLED")

    official = {identifier: _thaw(item) for identifier, item in knowledge.evidence.items()}
    return KnowledgeBaseStore(directory, baseline_catalog=CATALOG_PATH if baseline is None else Path(baseline),
                              official_evidence=official, runtime_check=runtime_check)
