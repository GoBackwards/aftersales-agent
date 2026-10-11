"""Persistent sessions with click-only, card-bound confirmation (protocol v3, B3a; lifecycle B3b; B3c),
human-service tickets (B4a) and the failure policy (B4b).

A session holds at most one open return card per order (return_cards.py) and goes on after a click submits
a request (B3c, decision D4). It also holds at most one open ticket, opened by the "找人工" button and
withdrawn from its card (tickets.py, B4a).
Busy leases survive restart. Explicit recovery, cancellation, or recovery after a persisted deadline,
abandons a run without replaying its work and leaves every card as it was (B3b); a click while a run is
active is refused as BUSY. Action IDs identify proposed cards; they are not authentication tokens.

A failed turn (B4b; plan §6.1, §6.3; standard HO4, HR5, W6) writes nothing but its run and its reply, and the
reply ends with exactly one next step, chosen here when the run is finalized: a retry, which the failure card
offers beside a "找人工" (B4d, HO4: the same click as the persistent button), or, when this turn and the one before
it both failed (counted from session_runs, K11), the session's ticket, opened or appended to with the trigger
technical_failure in the same transaction and stated plainly. Once the customer has withdrawn a ticket during a
run of failures and has none open, that run neither reopens one nor offers "找人工" again (HN7, S12c; B4d). A
completed turn, an item-level degraded one included, and a cancelled one end a run of failures. The failed turn's
cards stay as they were (B3b, I8).

There is no schema migration. A database written by an older schema version is
renamed to <name>.bak-<UTC timestamp> next to it (never deleted or overwritten)
and a fresh database with the seeded demo orders takes its place.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .domain import Store
from .return_cards import ReturnCards
from .tickets import TECHNICAL_FAILURE, Tickets, technical_failure_text, ticket_block


# v5 (B2): orders.opened became orders.condition (domain.CONDITIONS).
# v6 (B3a): return_cards replaces session_tasks and pending_actions; return_requests stores reason_code,
# reason_text and source_action_id; session_runs and handoffs lose task_id, and session_runs keeps the
# orders each turn resolved (K5). v7 (B3c): one open card per session and order. v8 (B4a): tickets and
# ticket_events, one open ticket per session (plan §6.2). B4b deleted the only writer of the failure log
# handoffs; the empty table stays until the next schema change, so B4b sets no database aside. An older
# database is set aside (D2).
SCHEMA_VERSION = 8
_SCHEMA = (
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
        created_at REAL NOT NULL, last_reply TEXT NOT NULL DEFAULT '',
        busy_run_id TEXT, busy_since REAL)""",
    """CREATE TABLE session_messages (
        session_id TEXT NOT NULL REFERENCES sessions(session_id), sequence INTEGER NOT NULL,
        payload TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(session_id, sequence))""",
    """CREATE TABLE session_requests (
        customer_id TEXT NOT NULL, entry TEXT NOT NULL, request_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id), payload TEXT NOT NULL,
        run_id TEXT UNIQUE, status TEXT NOT NULL, response TEXT,
        PRIMARY KEY(customer_id, entry, request_id))""",
    """CREATE TABLE return_cards (
        action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id), order_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'voided', 'expired')),
        void_reason TEXT, created_run_id TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, result TEXT)""",
    # One open card per session and order (plan §4.6, B3c with decision D4).
    "CREATE UNIQUE INDEX one_open_card ON return_cards(session_id, order_id) WHERE status = 'pending'",
    """CREATE TABLE session_runs (
        run_id TEXT PRIMARY KEY REFERENCES session_requests(run_id), customer_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        request_id TEXT NOT NULL, started_at REAL NOT NULL, deadline REAL NOT NULL,
        max_model_attempts INTEGER NOT NULL, max_tool_calls INTEGER NOT NULL,
        model_attempts INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
        last_step INTEGER NOT NULL DEFAULT 0, last_event TEXT NOT NULL DEFAULT 'started',
        status TEXT NOT NULL, finished_at REAL, mode TEXT NOT NULL DEFAULT 'unknown',
        context TEXT NOT NULL DEFAULT '{}', usage_complete INTEGER NOT NULL DEFAULT 0,
        resolved_orders TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE run_events (
        run_id TEXT NOT NULL REFERENCES session_runs(run_id), sequence INTEGER NOT NULL,
        created_at REAL NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL,
        PRIMARY KEY(run_id, sequence))""",
    """CREATE TABLE handoffs (
        handoff_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES session_runs(run_id),
        customer_id TEXT NOT NULL, session_id TEXT NOT NULL,
        request_id TEXT NOT NULL, created_at REAL NOT NULL, reason_code TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', notice TEXT NOT NULL)""",
    """CREATE TABLE tickets (
        ticket_id TEXT PRIMARY KEY, short_no TEXT NOT NULL, customer_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        status TEXT NOT NULL CHECK (status IN ('pending', 'assigned', 'resolved', 'cancelled')),
        urgency TEXT NOT NULL CHECK (urgency IN ('normal', 'high', 'safety')),
        summary TEXT NOT NULL, summary_written INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(customer_id, short_no))""",
    # One open ticket per session (plan §6.2, standard A3).
    "CREATE UNIQUE INDEX one_open_ticket ON tickets(session_id) WHERE status IN ('pending', 'assigned')",
    """CREATE TABLE ticket_events (
        ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id), seq INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('opened', 'appended', 'withdrawn')), trigger TEXT NOT NULL,
        evidence TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(ticket_id, seq))""",
)
TABLES = ("orders", "return_requests", "sessions", "session_messages", "session_requests",
          "return_cards", "session_runs", "run_events", "handoffs", "tickets", "ticket_events")
FAILED_RUN_STATUSES = {"failed", "model_error", "step_limit", "error", "tool_error",
                       "runtime_error", "timed_out", "budget_exhausted", "interrupted"}
# B4b failure copy (production voice). A failed turn's reply starts with FAILED_TURN_TEXT (the run's own error
# stays in the response's error field), the envelope adds the open cards' state, and _failure_next_step adds the
# one next step: RETRY_STEP (the page's "重试" sends the same message again as a new request; B4d: next_step
# offer_handoff adds the card's "找人工"), or, from the second failed turn in a row, the ticket sentence
# (tickets.technical_failure_text), never both (W6).
FAILED_TURN_TEXT = "这一轮没有处理完。"
RETRY_STEP = "可以点“重试”再发一次这条消息。"
FAILURES_FOR_TICKET = 2
EVENT_FIELDS = {"step", "attempt", "error_category", "error_code", "reason_code", "status_code",
                "retryable", "delay_seconds", "tool_call_id", "tool_name", "outcome", "status",
                "elapsed_seconds", "remaining_seconds", "model_attempts", "tool_calls",
                "max_model_attempts", "max_tool_calls", "deadline", "mode", "retrieval", "usage", "usage_complete", "reason",
                "knowledge_release", "conversation_version", "action_id"}
USAGE_NOTE = "中断或失败请求可能产生未返回用量；不表示费用为零。"
# The tools a graph run reserves (business); any other name is recorded as unknown_tool.
EVENT_TOOL_NAMES = frozenset({"get_order", "check_return_eligibility", "update_return_request", "prepare_return"})


class SessionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = self.public_message = message
        super().__init__(message)


def _text(value, *, strip=False):
    if type(value) is not str or not value.strip():
        raise SessionError("INVALID_ARGUMENTS", "参数必须为非空字符串。")
    return value.strip() if strip else value


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise SessionError("INVALID_ARGUMENTS", "消息或结果必须为有效 JSON 数据。") from None


def _set_aside(path):
    """Rename an older-schema database to <name>.bak-<UTC timestamp>; never delete or overwrite a file.

    The name is claimed first with an exclusive create, so the rename can only replace that
    empty placeholder. A leftover rollback journal or WAL file means a writer may still hold
    the database: nothing is renamed then.
    """
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-journal", "-wal")):
        raise SessionError("STORAGE_ERROR", "旧版数据库仍有未完成的日志文件，未改名备份，也未重建。")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for attempt in range(100):
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{attempt}" if attempt else ""))
        try:
            with backup.open("xb"):
                pass
        except FileExistsError:
            continue
        try:
            os.replace(path, backup)
        except OSError:
            backup.unlink()  # only the empty placeholder created just above
            raise
        return backup
    raise SessionError("STORAGE_ERROR", "无法为旧版数据库生成不重名的备份文件名。")


class SessionStore(Tickets, ReturnCards, Store):
    def __init__(self, db_path, *, clock=time.time, confirmation_ttl=600):
        if (isinstance(confirmation_ttl, bool) or not isinstance(confirmation_ttl, (int, float))
                or not math.isfinite(confirmation_ttl) or confirmation_ttl <= 0):
            raise SessionError("INVALID_ARGUMENTS", "确认有效期必须为正数秒。")
        self.clock, self.confirmation_ttl = clock, confirmation_ttl
        self.backup_path = None
        try:
            if str(db_path) != ":memory:" and Path(db_path).exists():
                version, tables = self._inspect(db_path)
                if version > SCHEMA_VERSION:
                    raise SessionError("UNSUPPORTED_SCHEMA", "数据库版本高于当前程序支持版本。")
                if version == SCHEMA_VERSION:
                    if not set(TABLES) <= tables:
                        raise SessionError("INVALID_SCHEMA", "数据库缺少所需业务表。")
                    # An existing current database is opened as is: no seeding, no write.
                    self._connection = sqlite3.connect(str(db_path))
                    self._connection.row_factory = sqlite3.Row
                    self._connection.execute("PRAGMA foreign_keys = ON")
                    return
                if tables or version:
                    if not {"orders", "return_requests"} <= tables:
                        # Not a database this program wrote; leave it untouched.
                        raise SessionError("INVALID_SCHEMA", "数据库缺少所需业务表。")
                    self.backup_path = _set_aside(Path(db_path))
            super().__init__(db_path)
            self._create_schema()
        except (sqlite3.Error, OSError):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise SessionError("STORAGE_ERROR", "无法打开、备份或创建会话数据库。") from None
        except BaseException:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    @staticmethod
    def _inspect(db_path):
        connection = sqlite3.connect(str(db_path))
        try:
            # Reading the header also rolls back a hot journal left by a crashed writer.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        return version, tables

    def _create_schema(self):
        """Create the current schema in a database that has none yet (Store has seeded the orders)."""
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return  # another connection created it first
            if version != 0:
                raise SessionError("UNSUPPORTED_SCHEMA", "数据库版本与当前程序不一致。")
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self, *, read_only=False):
        try:
            self._connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            yield
            self._connection.commit()
        except sqlite3.Error:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            raise SessionError("STORAGE_ERROR", "会话存储暂时无法完成操作，请稍后重试。") from None
        except BaseException:
            self._connection.rollback()
            raise

    def _now(self):
        value = self.clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise SessionError("INVALID_CLOCK", "确认时钟必须包含时区。")
            value = value.timestamp()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SessionError("INVALID_CLOCK", "确认时钟必须返回有效 UTC 时间或 epoch 秒。")
        return float(value)

    def _owned(self, customer_id, session_id):
        _text(customer_id)
        _text(session_id)
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND customer_id = ?",
            (session_id, customer_id),
        ).fetchone()
        if row is None:
            raise SessionError("SESSION_NOT_FOUND", "会话不存在或无权访问。")
        return row

    def _history(self, session_id):
        return [json.loads(row[0]) for row in self._connection.execute(
            "SELECT payload FROM session_messages WHERE session_id = ? ORDER BY sequence", (session_id,))]

    def _append_messages(self, session_id, messages):
        first = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM session_messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        self._connection.executemany(
            "INSERT INTO session_messages(session_id, sequence, payload, created_at) VALUES (?, ?, ?, ?)",
            [(session_id, first + offset, _json(message), self._now()) for offset, message in enumerate(messages)],
        )

    def _snapshot(self, customer_id, session_id):
        session = self._owned(customer_id, session_id)
        totals = self._connection.execute("""SELECT COUNT(*) AS runs,
            COALESCE(SUM(model_attempts), 0) AS model_attempts, COALESCE(SUM(tool_calls), 0) AS tool_calls
            FROM session_runs WHERE session_id = ?""", (session_id,)).fetchone()
        snapshot = {"session_id": session_id, "customer_id": customer_id,
                # The session's latest card in any state (a card never carries a reason), its open cards
                # (B3c: one per order, oldest first) and the request a click in this session submitted last,
                # without its reason.
                "card": self._card_view(self._latest_card(session_id)),
                "open_cards": self._open_cards(session_id),
                "return_request": self._session_return_request(customer_id, session_id),
                "last_reply": session["last_reply"], "busy": session["busy_run_id"] is not None,
                "active_run_id": session["busy_run_id"],
                "execution_totals": {"runs": totals["runs"], "model_attempts": totals["model_attempts"],
                                     "tool_calls": totals["tool_calls"]}}
        return snapshot

    def _request(self, customer_id, entry, request_id, payload):
        _text(request_id)
        row = self._connection.execute(
            "SELECT * FROM session_requests WHERE customer_id = ? AND entry = ? AND request_id = ?",
            (customer_id, entry, request_id),
        ).fetchone()
        if row and row["payload"] != _json(payload):
            raise SessionError("REQUEST_CONFLICT", "该请求 ID 已用于不同的请求内容。")
        return row

    @staticmethod
    def _not_busy(session):
        if session["busy_run_id"] is not None:
            raise SessionError("BUSY", "会话正在处理一轮输入；请等待完成，不能自动接管未完成运行。")

    def _lease(self, customer_id, session_id, run_id, *, allow_timeout=False):
        session = self._owned(customer_id, session_id)
        _text(run_id)
        runtime = self._connection.execute(
            "SELECT * FROM session_runs WHERE run_id = ? AND session_id = ? AND customer_id = ?",
            (run_id, session_id, customer_id)).fetchone()
        if runtime and runtime["status"] in {"cancelled", "interrupted", "timed_out"}:
            code = {"cancelled": "RUN_CANCELLED", "interrupted": "RUN_INTERRUPTED", "timed_out": "RUN_TIMEOUT"}[runtime["status"]]
            raise SessionError(code, "运行已取消、中断或超时；请读取已保存结果。")
        if session["busy_run_id"] != run_id:
            raise SessionError("RUN_NOT_ACTIVE", "运行不属于该会话的当前活动轮次。")
        row = self._connection.execute(
            "SELECT * FROM session_requests WHERE run_id = ? AND session_id = ? AND customer_id = ? AND status = 'running'",
            (run_id, session_id, customer_id),
        ).fetchone()
        if row is None:
            raise SessionError("RUN_NOT_ACTIVE", "运行不属于该会话的当前活动轮次。")
        if runtime is None:
            raise SessionError("RUN_NOT_ACTIVE", "运行控制记录不存在。")
        if not allow_timeout and self._now() >= runtime["deadline"]:
            raise SessionError("RUN_TIMEOUT", "运行已超过截止时间，不再执行新的动作。")
        return row

    @staticmethod
    def _safe_event_details(details):
        if type(details) is not dict or not set(details) <= EVENT_FIELDS:
            raise SessionError("INVALID_ARGUMENTS", "运行事件只接受已声明的安全元信息字段。")
        details = dict(details)
        if "tool_name" in details:
            name = details["tool_name"]
            details["tool_name"] = name if type(name) is str and name in EVENT_TOOL_NAMES else "unknown_tool"
        if "tool_call_id" in details:
            call_id = _text(details["tool_call_id"])
            details["tool_call_id"] = "call-sha256-" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()
        for key, value in details.items():
            if key == 'knowledge_release':
                required = {'release_id', 'manifest_sha256', 'generation', 'channel', 'review_status', 'effective_dates'}
                if (type(value) is not dict or set(value) != required
                        or any(type(value[k]) is not str or len(value[k]) != 64
                               or any(c not in '0123456789abcdef' for c in value[k])
                               for k in ('release_id', 'manifest_sha256'))
                        or value['release_id'] != value['manifest_sha256']
                        or type(value['generation']) is not int or value['generation'] < 1
                        or value['channel'] not in ('development', 'reviewed')
                        or value['review_status'] != ('development_candidate' if value['channel'] == 'development' else 'human_attestations_recorded')
                        or value['effective_dates'] != 'unverified_snapshot'):
                    raise SessionError('INVALID_ARGUMENTS', '知识版本运行元信息无效。')
            elif key == 'conversation_version':
                if value not in ('m2', 'm3', 'm5', 'graph'):
                    raise SessionError('INVALID_ARGUMENTS', '会话版本运行元信息无效。')
            elif key == "retrieval":
                if (type(value) is not dict or not set(value) <= {"enabled", "strategy", "top_k", "max_context_chars"}
                        or ("enabled" in value and type(value["enabled"]) is not bool)
                        or ("strategy" in value and (type(value["strategy"]) is not str
                                                     or value["strategy"] not in {"top_k", "policy_diverse"}))
                        or any(type(value[field]) is not int or value[field] < 1
                               for field in ("top_k", "max_context_chars") if field in value)):
                    raise SessionError("INVALID_ARGUMENTS", "检索运行元信息只能包含检索配置。")
            elif key == "mode":
                if type(value) is not str or not value or len(value) > 64 or not all(char.isalnum() or char == "_" for char in value):
                    raise SessionError("INVALID_ARGUMENTS", "运行来源必须为简短标识符。")
            elif key == "usage_complete":
                if type(value) is not bool:
                    raise SessionError("INVALID_ARGUMENTS", "用量完整性标记必须为布尔值。")
            elif key == "usage":
                if (type(value) is not dict or not set(value) <= {"prompt_tokens", "completion_tokens", "total_tokens"}
                        or any(type(number) is not int or number < 0 for number in value.values())):
                    raise SessionError("INVALID_ARGUMENTS", "用量元信息只能包含非负 token 计数。")
            elif type(value) is str:
                if not value or len(value) > 160 or any(char in value for char in ("\n", "\r")):
                    raise SessionError("INVALID_ARGUMENTS", "运行事件标签格式无效。")
            elif value is not None and (type(value) not in {bool, int, float}
                                        or (type(value) is float and not math.isfinite(value))):
                raise SessionError("INVALID_ARGUMENTS", "运行事件不接受正文、凭据或复杂对象。")
        return json.loads(_json(details))

    def _append_run_event(self, run_id, kind, details):
        sequence = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)).fetchone()[0]
        self._connection.execute("INSERT INTO run_events VALUES (?, ?, ?, ?, ?)",
                                 (run_id, sequence, self._now(), kind, _json(details)))
        step = details.get("step", 0)
        step = step if type(step) is int and step >= 0 else 0
        self._connection.execute("UPDATE session_runs SET last_step = MAX(last_step, ?), last_event = ? WHERE run_id = ?",
                                 (step, kind, run_id))

    def _run_summary(self, run_id):
        row = self._connection.execute("SELECT * FROM session_runs WHERE run_id = ?", (run_id,)).fetchone()
        result = {key: row[key] for key in ("run_id", "customer_id", "session_id", "request_id",
                  "started_at", "deadline", "max_model_attempts", "max_tool_calls", "model_attempts",
                  "tool_calls", "last_step", "last_event", "status", "finished_at", "mode")}
        result["remaining_seconds"] = max(0.0, row["deadline"] - self._now())
        result["usage_complete"] = bool(row["usage_complete"])
        result["usage_note"] = None if row["usage_complete"] else USAGE_NOTE
        return result

    def check_run(self, customer_id, session_id, run_id):
        with self._transaction(read_only=True):
            self._lease(customer_id, session_id, run_id)
            return self._run_summary(run_id)

    def current_user_message(self, customer_id, session_id, run_id):
        """The active run's user message, read under its lease."""
        with self._transaction(read_only=True):
            request = self._lease(customer_id, session_id, run_id)
            return json.loads(request["payload"])["user_message"]

    def reserve_model_attempt(self, customer_id, session_id, run_id):
        with self._transaction():
            self._lease(customer_id, session_id, run_id)
            runtime = self._run_summary(run_id)
            if runtime["model_attempts"] >= runtime["max_model_attempts"]:
                raise SessionError("MODEL_BUDGET", "运行已达到模型请求上限，重试也计入该上限。")
            self._connection.execute("UPDATE session_runs SET model_attempts = model_attempts + 1 WHERE run_id = ?", (run_id,))
            self._append_run_event(run_id, "model_reserved", {"attempt": runtime["model_attempts"] + 1})
            return self._run_summary(run_id)

    def reserve_tool_call(self, customer_id, session_id, run_id, *, tool_name, tool_call_id):
        details = self._safe_event_details({"tool_name": _text(tool_name), "tool_call_id": _text(tool_call_id)})
        with self._transaction():
            self._lease(customer_id, session_id, run_id)
            runtime = self._run_summary(run_id)
            if runtime["tool_calls"] >= runtime["max_tool_calls"]:
                raise SessionError("TOOL_BUDGET", "运行已达到工具调用上限。")
            self._connection.execute("UPDATE session_runs SET tool_calls = tool_calls + 1 WHERE run_id = ?", (run_id,))
            self._append_run_event(run_id, "tool_reserved", details)
            return self._run_summary(run_id)

    def record_run_event(self, customer_id, session_id, run_id, kind, details):
        if type(kind) is not str or not kind or len(kind) > 64 or not all(char.isalnum() or char == "_" for char in kind):
            raise SessionError("INVALID_ARGUMENTS", "事件类型必须为简短标识符。")
        details = self._safe_event_details(details)
        with self._transaction():
            self._lease(customer_id, session_id, run_id, allow_timeout=True)
            if kind == "run_context":
                row = self._connection.execute("SELECT context FROM session_runs WHERE run_id = ?", (run_id,)).fetchone()
                context = json.loads(row["context"])
                if "retrieval" in details:
                    context["retrieval"] = details["retrieval"]
                for key in ('knowledge_release', 'conversation_version'):
                    if key in details:
                        if key in context and context[key] != details[key]:
                            raise SessionError('INVALID_ARGUMENTS', '运行绑定的知识或会话版本不能变更。')
                        context[key] = details[key]
                self._connection.execute("UPDATE session_runs SET context = ? WHERE run_id = ?", (_json(context), run_id))
            self._append_run_event(run_id, kind, details)

    def get_run(self, customer_id, session_id, run_id):
        with self._transaction(read_only=True):
            self._owned(customer_id, session_id)
            row = self._connection.execute(
                "SELECT run_id FROM session_runs WHERE run_id = ? AND session_id = ? AND customer_id = ?",
                (run_id, session_id, customer_id)).fetchone()
            if row is None:
                raise SessionError("RUN_NOT_FOUND", "运行不存在或无权访问。")
            result = self._run_summary(run_id)
            result["events"] = [{"run_id": run_id, "session_id": session_id, "customer_id": customer_id,
                                  "request_id": result["request_id"],
                                  "sequence": event["sequence"], "created_at": event["created_at"],
                                  "kind": event["kind"], "details": json.loads(event["details"])}
                                 for event in self._connection.execute(
                                     "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,))]
            return result

    def _failure_streak(self, session_id):
        """K11: the run ids of this session's latest turns that failed in a row, newest first, read from session_runs
        (turns in the order they ran; any status outside FAILED_RUN_STATUSES ends the run of failures)."""
        streak = []
        for row in self._connection.execute(
                "SELECT run_id, status FROM session_runs WHERE session_id = ? ORDER BY rowid DESC", (session_id,)):
            if row["status"] not in FAILED_RUN_STATUSES:
                break
            streak.append(row["run_id"])
        return streak

    def _failure_next_step(self, customer_id, session_id, request_id, status):
        """B4b: the one next step of the failed turn being finalized, the sentence that states it, and the ticket card
        it declares (or None). Called after the run's status is written, inside the finalizing transaction."""
        streak = self._failure_streak(session_id)
        failures, open_ticket = len(streak), self._open_ticket(session_id)
        # HN7 (A6, S12c; B4d): a ticket withdrawal clicked after this run of failures began (the request log orders
        # turns and clicks) stands until a turn ends the run, unless the customer has asked again since.
        withdrawn = open_ticket is None and self._connection.execute(
            """SELECT 1 FROM session_requests WHERE session_id = ? AND entry = 'ticket_withdraw' AND status = 'completed'
               AND rowid > (SELECT rowid FROM session_requests WHERE run_id = ?) LIMIT 1""",
            (session_id, streak[-1])).fetchone() is not None
        # The second failure in a row opens the ticket (or appends to an open one), and so does every later failure
        # of the run. Before that the card offers the retry and, with no ticket open, "找人工" (HO4).
        if failures < FAILURES_FOR_TICKET or withdrawn:
            return ({"action": "retry", "consecutive_failures": failures,
                     "offer_handoff": open_ticket is None and not withdrawn}, RETRY_STEP, None)
        evidence = {"source": "consecutive_failed_turns", "request_id": request_id, "run_status": status,
                    "consecutive_failures": failures}
        view, outcome = self._open_or_append_ticket(customer_id, session_id, TECHNICAL_FAILURE, evidence)
        return ({"action": "ticket", "consecutive_failures": failures, "ticket_outcome": outcome},
                technical_failure_text(view, outcome), ticket_block(view))

    def _finalize_turn(self, customer_id, session_id, request, messages, response):
        run_id = request["run_id"]
        saved = dict(response)
        status = saved.get("status", "completed")
        if type(status) is not str:
            raise SessionError("INVALID_ARGUMENTS", "运行结果状态必须为文本。")
        reply = saved.get("reply", "")
        if type(reply) is not str:
            raise SessionError("INVALID_ARGUMENTS", "回复必须为文本。")
        before = self._run_summary(run_id)
        mode = saved.get("mode", "unknown") if before["mode"] == "unknown" else before["mode"]
        self._safe_event_details({"mode": mode})
        saved["mode"] = mode
        had_error = self._connection.execute(
            "SELECT 1 FROM run_events WHERE run_id = ? AND kind = 'model_attempt_error' LIMIT 1", (run_id,)).fetchone()
        responses = [json.loads(row[0]) for row in self._connection.execute(
            "SELECT details FROM run_events WHERE run_id = ? AND kind = 'model_response'", (run_id,))]
        complete_responses = len(responses) == before["model_attempts"] and all(
            item.get("usage_complete") is True and type(item.get("usage")) is dict
            and all(type(item["usage"].get(key)) is int and item["usage"][key] >= 0
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")) for item in responses)
        usage_complete = (status == "completed" and type(mode) is str and mode != "unknown"
                          and not had_error
                          and (mode.startswith("offline") or complete_responses))
        self._connection.execute("UPDATE session_runs SET status = ?, finished_at = ?, usage_complete = ?, mode = ? WHERE run_id = ?",
                                 (status, self._now(), int(bool(usage_complete)), mode, run_id))
        next_step = ticket_card = None
        if status in FAILED_RUN_STATUSES:
            next_step, step_text, ticket_card = self._failure_next_step(customer_id, session_id,
                                                                        request["request_id"], status)
            reply = (reply + "\n" if reply else "") + step_text
            if isinstance(saved.get("presentation"), dict) and "short_answer" in saved["presentation"]:
                saved["presentation"] = {**saved["presentation"], "short_answer": reply}
        # B8: a completed turn whose handoff, complaint or safety items ask for the ticket (decide.ticket_request): the
        # store opens or appends it here, in the finalizing transaction, and states it (its lines and its card).
        wanted = saved.pop("ticket_request", None)
        if status == "completed" and wanted is not None:
            view, outcome, lines = self._chat_ticket(customer_id, session_id, request["request_id"], wanted,
                                                     shown=reply)
            reply = (reply + "\n\n" if reply else "") + lines
            ticket_card = ticket_block(view)
            presentation = saved.get("presentation")
            if isinstance(presentation, dict):
                blocks = [*(presentation.get("blocks") or []), {"type": "text", "author": "code", "text": lines}]
                saved["presentation"] = {**presentation, "short_answer": reply, "blocks": blocks}
            saved["trace"] = [*(saved.get("trace") or []),
                              {"type": "ticket", "outcome": outcome, "short_no": view["short_no"],
                               "triggers": list(wanted["triggers"]), "urgency": view["urgency"]}]
        messages = [dict(message) for message in messages]
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            messages[-1]["content"] = reply
        else:
            messages.append({"role": "assistant", "content": reply})
        self._append_messages(session_id, messages)
        self._connection.execute(
            "UPDATE sessions SET busy_run_id = NULL, busy_since = NULL, last_reply = ? WHERE session_id = ?",
            (reply, session_id))
        self._append_run_event(run_id, "finished", {"status": status})
        runtime = self._run_summary(run_id)
        context = json.loads(self._connection.execute("SELECT context FROM session_runs WHERE run_id = ?", (run_id,)).fetchone()[0])
        for key in ("retrieval", 'knowledge_release', 'conversation_version'):
            if key in context:
                saved.setdefault(key, context[key])
        saved.setdefault("mode", runtime["mode"])
        for key, value in (("trace", []), ("usage", {})):
            saved.setdefault(key, value)
        saved.update(status=status, reply=reply, session=self._snapshot(customer_id, session_id), run_id=run_id,
                     request_id=request["request_id"], replayed=False, execution=runtime)
        # B4b: only a failed turn has a next step, and only the store states it (a caller's copy never counts).
        for key, value in (("next_step", next_step), ("ticket_card", ticket_card)):
            saved.pop(key, None)
            if value is not None:
                saved[key] = value
        if saved.get("schema_version") == "unified-conversation-v1":
            # Store the alias from authoritative state, not a caller's old view.
            saved["run_status"] = status
            saved.setdefault("policy_status", "not_requested")
        request_status = status if status in {"cancelled", "interrupted"} else "failed" if status in FAILED_RUN_STATUSES else "completed"
        self._connection.execute("UPDATE session_requests SET status = ?, response = ? WHERE run_id = ?",
                                 (request_status, _json(saved), run_id))
        return json.loads(_json(saved))

    def create_session(self, customer_id):
        _text(customer_id)
        session_id = "SES-" + uuid.uuid4().hex
        with self._transaction():
            self._connection.execute("INSERT INTO sessions(session_id, customer_id, created_at) VALUES (?, ?, ?)",
                                     (session_id, customer_id, self._now()))
            return self._snapshot(customer_id, session_id)

    def get_session(self, customer_id, session_id):
        with self._transaction(read_only=True):
            return self._snapshot(customer_id, session_id)

    def begin_turn(self, customer_id, session_id, request_id, user_message, *, run_timeout=120,
                   max_model_attempts=8, max_tool_calls=32, mode="unknown"):
        _text(user_message)
        if (type(run_timeout) not in {int, float} or not math.isfinite(run_timeout) or run_timeout <= 0
                or type(max_model_attempts) is not int or max_model_attempts < 1
                or type(max_tool_calls) is not int or max_tool_calls < 1):
            raise SessionError("INVALID_ARGUMENTS", "运行截止时间和调用上限必须为正数。")
        self._safe_event_details({"mode": mode})
        payload = {"session_id": session_id, "user_message": user_message}
        with self._transaction():
            session = self._owned(customer_id, session_id)
            previous = self._request(customer_id, "turn", request_id, payload)
            if previous and previous["status"] != "running":
                return {"replayed": True, "run_id": previous["run_id"], "history": self._history(session_id),
                        "response": json.loads(previous["response"])}
            self._not_busy(session)
            if previous:
                raise SessionError("BUSY", "请求仍未完成，当前版本不自动恢复中断轮次。")
            run_id = "RUN-" + uuid.uuid4().hex
            self._connection.execute(
                """INSERT INTO session_requests(customer_id, entry, request_id, session_id, payload, run_id, status)
                   VALUES (?, 'turn', ?, ?, ?, ?, 'running')""",
                (customer_id, request_id, session_id, _json(payload), run_id))
            self._connection.execute("UPDATE sessions SET busy_run_id = ?, busy_since = ? WHERE session_id = ?",
                                     (run_id, self._now(), session_id))
            now = self._now()
            self._connection.execute("""INSERT INTO session_runs(run_id, customer_id, session_id,
                request_id, started_at, deadline, max_model_attempts, max_tool_calls, status, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (run_id, customer_id, session_id, request_id,
                 now, now + run_timeout, max_model_attempts, max_tool_calls, mode))
            self._append_run_event(run_id, "started", {"mode": mode, "deadline": now + run_timeout})
            return {"replayed": False, "run_id": run_id, "history": self._history(session_id), "response": None}

    def finish_turn(self, customer_id, session_id, run_id, messages, response):
        if type(messages) is not list or not messages or type(response) is not dict:
            raise SessionError("INVALID_ARGUMENTS", "需要本轮消息列表和结果对象。")
        with self._transaction():
            request = self._lease(customer_id, session_id, run_id,
                                  allow_timeout=response.get("status") in FAILED_RUN_STATUSES)
            user_message = json.loads(request["payload"])["user_message"]
            if (any(type(message) is not dict for message in messages)
                    or messages[0] != {"role": "user", "content": user_message}
                    or any(message.get("role") not in {"assistant", "tool"} for message in messages[1:])):
                raise SessionError("INVALID_ARGUMENTS", "消息须为本轮增量，首条与本轮用户输入一致。")
            return self._finalize_turn(customer_id, session_id, request, messages, response)

    def fail_turn(self, customer_id, session_id, run_id, message, *, response=None):
        _text(message)
        if response is not None and type(response) is not dict:
            raise SessionError("INVALID_ARGUMENTS", "失败结果必须为对象。")
        with self._transaction():
            request = self._lease(customer_id, session_id, run_id, allow_timeout=True)
            user_message = json.loads(request["payload"])["user_message"]
            saved = dict(response or {})
            status = saved.get("status")
            saved.update(status=status if type(status) is str and status in FAILED_RUN_STATUSES else "failed", reply=message)
            self._finalize_turn(customer_id, session_id, request,
                                [{"role": "user", "content": user_message}], saved)

    def owned_order(self, customer_id, order_id):
        """Read one of this customer's orders; another customer's is reported as missing."""
        return self._owned_order(customer_id, order_id)

    def record_resolved_orders(self, customer_id, session_id, run_id, order_ids):
        """Keep the own orders this turn resolved with its run (the input of K5)."""
        order_ids = sorted(set(order_ids))
        if any(type(order_id) is not str or not order_id for order_id in order_ids):
            raise SessionError("INVALID_ARGUMENTS", "订单编号无效。")
        with self._transaction():
            self._lease(customer_id, session_id, run_id)
            self._connection.execute("UPDATE session_runs SET resolved_orders = ? WHERE run_id = ?",
                                     (_json(order_ids), run_id))

    def latest_resolved_orders(self, customer_id, session_id):
        """K5: the orders the latest completed turn of this session resolved (empty when no turn did)."""
        with self._transaction(read_only=True):
            self._owned(customer_id, session_id)
            row = self._connection.execute(
                """SELECT resolved_orders FROM session_runs WHERE customer_id = ? AND session_id = ?
                   AND status = 'completed' AND resolved_orders != '[]'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""", (customer_id, session_id)).fetchone()
            return json.loads(row["resolved_orders"]) if row else []

    def _command(self, customer_id, session_id, entry, request_id, payload, operation, *, allow_busy=False):
        error = None
        with self._transaction():
            session = self._owned(customer_id, session_id)
            previous = self._request(customer_id, entry, request_id, payload)
            if previous:
                result = json.loads(previous["response"])
                if previous["status"] == "failed":
                    error = SessionError(result["error"]["code"], result["error"]["message"])
            else:
                if not allow_busy:
                    self._not_busy(session)
                try:
                    result = operation()
                except SessionError as exc:
                    error = exc
                    result = {"error": {"code": exc.code, "message": exc.message}}
                self._connection.execute(
                    """INSERT INTO session_requests(customer_id, entry, request_id, session_id, payload, status, response)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (customer_id, entry, request_id, session_id, _json(payload), "failed" if error else "completed", _json(result)))
        if error:
            raise error
        return result

    def cancel(self, customer_id, session_id, request_id):
        """Stop this session's running turn, if one runs (plan §2.2: it controls the run only). B3b: no card is
        touched, here or by the interruption; the card has its own "暂不提交" click (request_void)."""
        return self._command(customer_id, session_id, "cancel", request_id, {"session_id": session_id},
                             lambda: self._interrupt_active(customer_id, session_id, cancelled=True,
                                                            reason_code="RUN_CANCELLED"), allow_busy=True)

    def _interrupt_active(self, customer_id, session_id, *, cancelled=False, reason_code="RUN_INTERRUPTED"):
        session = self._owned(customer_id, session_id)
        run_id = session["busy_run_id"]
        if run_id is None:
            return self._snapshot(customer_id, session_id)
        request = self._lease(customer_id, session_id, run_id, allow_timeout=True)
        # B3b (plan §4.4): an interrupted or cancelled turn leaves every card as it was.
        status = "cancelled" if cancelled else "interrupted"
        message = ("运行已取消；已提交的业务结果不会被撤销。" if cancelled else
                   "这一轮没有处理完，已经停止，不会自动重试；已有的确认卡和已提交的申请都保持原样。")
        self._append_run_event(run_id, "cancelled" if cancelled else "recovered", {"reason_code": reason_code})
        response = self._finalize_turn(customer_id, session_id, request,
            [{"role": "user", "content": json.loads(request["payload"])["user_message"]}],
            {"status": status, "reply": message, "error": {"code": reason_code, "message": message}})
        # B4d: the interrupted turn's one next step and ticket card come back as its own response states them.
        return {**response["session"], "execution": response["execution"],
                **{key: response[key] for key in ("next_step", "ticket_card") if key in response}}

    def recover(self, customer_id, session_id, request_id):
        return self._command(customer_id, session_id, "recover", request_id, {"session_id": session_id},
                             lambda: self._interrupt_active(customer_id, session_id), allow_busy=True)

    def recover_expired_runs(self):
        with self._transaction():
            rows = self._connection.execute("""SELECT r.customer_id, r.session_id
                FROM session_runs r JOIN sessions s ON s.busy_run_id = r.run_id
                WHERE r.status = 'running' AND r.deadline <= ? ORDER BY r.deadline, r.run_id""", (self._now(),)).fetchall()
            return [self._interrupt_active(row["customer_id"], row["session_id"], reason_code="RUN_TIMEOUT") for row in rows]
