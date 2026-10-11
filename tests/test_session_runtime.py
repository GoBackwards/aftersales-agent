"""Offline checks for durable budgets, cancellation races and the old-schema rebuild."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from support_agent import sessions
from support_agent.sessions import RETRY_STEP, SCHEMA_VERSION, SessionError, SessionStore, USAGE_NOTE


class SessionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.now = 1000.0
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.session = self.store.create_session("alice")
        self.sid = self.session["session_id"]

    def begin(self, request="turn-1", **kwargs):
        return self.store.begin_turn("alice", self.sid, request, "退货 O1001", **kwargs)["run_id"]

    def finish(self, run, status="completed", **kwargs):
        response = {"status": status, "reply": "本轮结果", **kwargs}
        return self.store.finish_turn("alice", self.sid, run,
            [{"role": "user", "content": "退货 O1001"}, {"role": "assistant", "content": "本轮结果"}], response)

    def failure_log(self, customer_id):
        """The handoffs rows (the old failure log) of one customer: B4b deleted their writer, so a failed turn adds
        none; B4a replaced GET /handoffs and SessionStore.list_handoffs with the tickets (GET /tickets)."""
        return [dict(row) for row in self.store._connection.execute(
            "SELECT * FROM handoffs WHERE customer_id = ? ORDER BY created_at, handoff_id", (customer_id,))]

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(SessionError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def prepare(self, run):
        self.store.prepare_card("alice", self.sid, run, "O1001")
        return self.store.get_session("alice", self.sid)

    def confirm(self, snapshot, request_id):
        card = snapshot["card"]
        return self.store.confirm_card("alice", self.sid, card["action_id"], card["version"], "changed_mind", "",
                                       request_id)

    def test_persisted_budget_and_replay_do_not_reset_counters(self):
        run = self.begin(mode="offline_scripted", max_model_attempts=2, max_tool_calls=1)
        self.assertEqual(self.store.check_run("alice", self.sid, run)["remaining_seconds"], 120)
        self.store.reserve_model_attempt("alice", self.sid, run)
        self.store.reserve_tool_call("alice", self.sid, run, tool_name="get_order", tool_call_id="call-one")
        self.error("TOOL_BUDGET", self.store.reserve_tool_call, "alice", self.sid, run,
                   tool_name="get_order", tool_call_id="call-two")
        self.store.close()
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.store.reserve_model_attempt("alice", self.sid, run)
        self.error("MODEL_BUDGET", self.store.reserve_model_attempt, "alice", self.sid, run)
        saved = self.finish(run)
        replay = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001", mode="live_agent")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], saved)
        self.assertEqual(saved["mode"], "offline_scripted")
        self.assertTrue(saved["execution"]["usage_complete"])
        self.assertEqual(saved["session"]["execution_totals"],
            {"runs": 1, "model_attempts": 2, "tool_calls": 1})
        next_run = self.begin("turn-2")
        self.store.reserve_model_attempt("alice", self.sid, next_run)
        totals = self.store.get_session("alice", self.sid)["execution_totals"]
        self.assertEqual((totals["runs"], totals["model_attempts"], totals["tool_calls"]), (2, 3, 1))

    def test_two_connections_cannot_over_reserve_last_attempt(self):
        run = self.begin(max_model_attempts=1)
        barrier = threading.Barrier(2)

        def attempt():
            with closing(SessionStore(self.path, clock=lambda: self.now)) as other:
                barrier.wait(timeout=5)
                try:
                    return other.reserve_model_attempt("alice", self.sid, run)["model_attempts"]
                except SessionError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, [1, "MODEL_BUDGET"])
        self.assertEqual(self.store.get_run("alice", self.sid, run)["model_attempts"], 1)

    def test_deadline_blocks_actions_but_allows_failed_finalization(self):
        run = self.begin(run_timeout=5, mode="live_agent")
        self.now += 5
        self.error("RUN_TIMEOUT", self.store.check_run, "alice", self.sid, run)
        self.error("RUN_TIMEOUT", self.store.reserve_model_attempt, "alice", self.sid, run)
        self.error("RUN_TIMEOUT", self.store.prepare_card, "alice", self.sid, run, "O1001")
        self.error("RUN_TIMEOUT", self.finish, run)
        saved = self.finish(run, "timed_out")
        self.assertFalse(saved["session"]["busy"])
        self.assertEqual(saved["execution"]["status"], "timed_out")
        self.assertEqual(saved["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertTrue(saved["reply"].endswith(RETRY_STEP))
        self.assertEqual(saved["reply"], saved["session"]["last_reply"])
        self.assertFalse(saved["execution"]["usage_complete"])
        self.assertEqual(saved["execution"]["usage_note"], USAGE_NOTE)
        replay = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")
        self.assertEqual(replay["history"][-1]["content"], saved["reply"])
        self.assertEqual(self.failure_log("alice"), [])

    def test_each_failure_status_ends_with_one_retry_step_and_writes_no_failure_record(self):
        for index, status in enumerate(("failed", "model_error", "step_limit", "tool_error", "runtime_error",
                                        "budget_exhausted", "interrupted")):
            with self.subTest(status=status):
                self.sid = self.store.create_session("alice")["session_id"]
                request = f"failure-{index}"
                run = self.begin(request, mode="offline_scripted")
                saved = self.finish(run, status)
                replay = self.store.begin_turn("alice", self.sid, request, "退货 O1001")
                self.assertEqual(saved, replay["response"])
                self.assertEqual(saved["reply"].count(RETRY_STEP), 1)
                self.assertEqual(saved["next_step"],
                                 {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
                self.assertNotIn("handoff", saved)
                self.assertFalse(saved["execution"]["usage_complete"])
        self.assertEqual(self.failure_log("alice"), [])
        self.assertEqual(self.store.list_tickets("alice"), [])

    def test_fail_turn_caches_provenance_and_safe_failure_history(self):
        run = self.begin(mode="offline_scripted")
        self.store.fail_turn("alice", self.sid, run, "运行未完成。", response={"trace": [], "usage": {}, "mode": "live_agent"})
        replay = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")
        saved = replay["response"]
        self.assertEqual(saved["mode"], "offline_scripted")
        self.assertEqual(saved["run_id"], run)
        self.assertEqual(saved["request_id"], "turn-1")
        self.assertEqual(saved["session"]["last_reply"], replay["history"][-1]["content"])
        self.assertEqual(saved["status"], "failed")

    def test_cancel_other_connection_fences_a_waiting_worker(self):
        run = self.begin(mode="live_agent")
        prepared = self.prepare(run)
        retrieval = {"enabled": True, "strategy": "top_k", "top_k": 3, "max_context_chars": 2400}
        self.store.record_run_event("alice", self.sid, run, "run_context", {"retrieval": retrieval})
        worker_started, cancellation_committed = threading.Event(), threading.Event()

        def late_worker():
            with closing(SessionStore(self.path, clock=lambda: self.now)) as worker:
                worker.reserve_model_attempt("alice", self.sid, run)
                worker_started.set()
                self.assertTrue(cancellation_committed.wait(timeout=5))
                failures = []
                operations = (
                    lambda: worker.check_run("alice", self.sid, run),
                    lambda: worker.record_resolved_orders("alice", self.sid, run, ["O1001"]),
                    lambda: worker.prepare_card("alice", self.sid, run, "O1004"),
                    lambda: worker.finish_turn("alice", self.sid, run, [{"role": "user", "content": "退货 O1001"}], {"status": "completed", "reply": "晚到回复"}),
                    lambda: worker.fail_turn("alice", self.sid, run, "晚到失败"),
                )
                for operation in operations:
                    try:
                        operation()
                    except SessionError as exc:
                        failures.append(exc.code)
                return failures

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(late_worker)
            self.assertTrue(worker_started.wait(timeout=5))
            cancelled = self.store.cancel("alice", self.sid, "cancel-1")
            cancellation_committed.set()
            self.assertEqual(future.result(timeout=5), ["RUN_CANCELLED"] * 5)
        self.assertFalse(cancelled["busy"])
        # B3b: cancelling a running turn leaves the card it prepared exactly as it was.
        self.assertEqual(cancelled["card"], prepared["card"])
        self.assertEqual(cancelled["card"]["status"], "pending")
        replay = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")["response"]
        self.assertEqual((replay["status"], replay["mode"], replay["retrieval"]), ("cancelled", "live_agent", retrieval))
        self.assertEqual(replay["execution"]["model_attempts"], 1)
        self.assertFalse(replay["execution"]["usage_complete"])
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0], 0)
        self.assertEqual(self.failure_log("alice"), [])

    def test_recovery_preserves_slots_and_old_recovery_replay_cannot_stop_new_run(self):
        run = self.begin()
        prepared = self.prepare(run)
        with closing(SessionStore(self.path, clock=lambda: self.now)) as other:
            recovered = other.recover("alice", self.sid, "recover-1")
        # B3b: recovering an interrupted turn leaves its card as it was.
        self.assertEqual(recovered["card"], prepared["card"])
        old_response = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")["response"]
        self.assertEqual(old_response["status"], "interrupted")
        self.assertNotIn("重新准备", old_response["reply"])
        next_run = self.begin("turn-2")
        # A click while the next turn runs is refused by the busy lock (what the hold used to do).
        self.error("BUSY", self.confirm, prepared, "confirm-while-busy")
        self.assertEqual(self.store.recover("alice", self.sid, "recover-1"), recovered)
        self.assertEqual(self.store.get_session("alice", self.sid)["active_run_id"], next_run)
        self.error("RUN_INTERRUPTED", self.store.prepare_card, "alice", self.sid, run, "O1004")
        self.assertTrue(old_response["reply"].endswith(RETRY_STEP))
        self.assertEqual(self.failure_log("alice"), [])

    def test_startup_recovers_only_expired_running_leases(self):
        expired = self.begin(run_timeout=5)
        other_sid = self.store.create_session("alice")["session_id"]
        live = self.store.begin_turn("alice", other_sid, "long-turn", "查询", run_timeout=50)["run_id"]
        self.now += 5
        self.store.close()
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.assertTrue(self.store.get_session("alice", self.sid)["busy"])
        recovered = self.store.recover_expired_runs()
        self.assertEqual([item["session_id"] for item in recovered], [self.sid])
        self.assertEqual(self.store.recover_expired_runs(), [])
        self.assertEqual(self.store.check_run("alice", other_sid, live)["status"], "running")
        self.error("RUN_INTERRUPTED", self.store.check_run, "alice", self.sid, expired)
        cached = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")["response"]
        self.assertEqual(cached["error"]["code"], "RUN_TIMEOUT")

    def test_recovery_and_cancel_preserve_committed_business_result(self):
        run = self.begin()
        prepared = self.prepare(run)
        self.finish(run)
        committed = self.confirm(prepared, "confirm-1")
        snapshot = {key: value for key, value in committed.items() if key != "confirmation_message"}
        recovered = self.store.recover("alice", self.sid, "recover-after-submit")
        cancelled = self.store.cancel("alice", self.sid, "cancel-after-submit")
        self.assertEqual(recovered, snapshot)
        self.assertEqual(cancelled, snapshot)
        self.assertEqual(cancelled["card"]["status"], "consumed")
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0], 1)
        self.assertEqual(self.failure_log("alice"), [])

    def test_customer_and_session_bound_run_and_handoff_access(self):
        run = self.begin()
        self.finish(run, "model_error")
        self.assertEqual(self.failure_log("bob"), [])
        self.error("SESSION_NOT_FOUND", self.store.get_run, "bob", self.sid, run)
        self.error("SESSION_NOT_FOUND", self.store.recover, "bob", self.sid, "steal-recover")
        self.error("SESSION_NOT_FOUND", self.store.request_card, "bob", self.sid, "O2001", "steal-card")
        other = self.store.create_session("alice")["session_id"]
        self.error("RUN_NOT_FOUND", self.store.get_run, "alice", other, run)
        self.assertEqual(self.store.get_run("alice", self.sid, run)["session_id"], self.sid)

    def test_events_are_ordered_metadata_only_and_model_identifiers_are_hashed(self):
        run = self.begin()
        private = "private-model-copied-user-text"
        self.store.reserve_tool_call("alice", self.sid, run, tool_name=private, tool_call_id=private)
        self.store.record_run_event("alice", self.sid, run, "tool_start",
                                    {"step": 2, "tool_name": private, "tool_call_id": private})
        self.store.record_run_event("alice", self.sid, run, "model_retry_deferred",
                                    {"step": 3, "attempt": 1, "delay_seconds": 12, "reason": "retry_after_exceeds_backoff_limit"})
        for invalid in ({"prompt": "private"}, {"api_key": "private"}, {"usage": {"raw": "body"}},
                        {"retrieval": {"strategy": []}}, {"mode": None}):
            self.error("INVALID_ARGUMENTS", self.store.record_run_event, "alice", self.sid, run, "test", invalid)
        snapshot = self.store.get_run("alice", self.sid, run)
        self.assertEqual(snapshot["last_step"], 3)
        self.assertEqual([event["sequence"] for event in snapshot["events"]], list(range(1, 5)))
        self.assertNotIn(private, json.dumps(snapshot))
        related = [event["details"] for event in snapshot["events"] if "tool_call_id" in event["details"]]
        expected_id = "call-sha256-" + hashlib.sha256(private.encode()).hexdigest()
        self.assertEqual([item["tool_call_id"] for item in related], [expected_id, expected_id])
        self.assertEqual([item["tool_name"] for item in related], ["unknown_tool", "unknown_tool"])

    def test_partial_api_usage_never_claims_complete_after_retry_error(self):
        run = self.begin(mode="live_agent")
        self.store.reserve_model_attempt("alice", self.sid, run)
        self.store.record_run_event("alice", self.sid, run, "model_attempt_error",
                                    {"error_category": "timeout", "retryable": True})
        self.store.reserve_model_attempt("alice", self.sid, run)
        saved = self.finish(run, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertFalse(saved["execution"]["usage_complete"])
        self.assertEqual(saved["execution"]["model_attempts"], 2)

    def test_usage_completeness_requires_every_attempt_response(self):
        tokens = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        for index, first_usage in enumerate(({}, tokens)):
            with self.subTest(first_usage=first_usage):
                self.sid = self.store.create_session("alice")["session_id"]
                run = self.begin(f"usage-{index}", mode="live_agent")
                for usage in (first_usage, tokens):
                    self.store.reserve_model_attempt("alice", self.sid, run)
                    self.store.record_run_event("alice", self.sid, run, "model_response",
                                                {"usage": usage, "usage_complete": bool(usage)})
                saved = self.finish(run, usage=tokens)
                self.assertEqual(saved["execution"]["usage_complete"], bool(first_usage))

    def test_fail_turn_preserves_timeout_classification_but_rejects_success(self):
        run = self.begin(run_timeout=1)
        self.now += 1
        self.store.fail_turn("alice", self.sid, run, "已超时。", response={"status": "timed_out"})
        cached = self.store.begin_turn("alice", self.sid, "turn-1", "退货 O1001")["response"]
        self.assertEqual(cached["status"], "timed_out")
        self.assertEqual(cached["execution"]["status"], "timed_out")
        next_run = self.begin("turn-2")
        self.store.fail_turn("alice", self.sid, next_run, "未完成。", response={"status": "completed"})
        cached = self.store.begin_turn("alice", self.sid, "turn-2", "退货 O1001")["response"]
        self.assertEqual(cached["status"], "failed")

    def test_a_failure_ticket_summary_keeps_the_failure_time_facts_after_later_success(self):
        # B4b: the ticket two failed turns open is the only record a failure leaves beyond its run; its summary is
        # written once, when it opens (the handoff summary of every failed turn is gone).
        run = self.begin()
        self.prepare(run)
        self.finish(run, "model_error")
        failed = self.finish(self.begin("turn-2"), "timed_out")
        self.assertEqual(failed["next_step"], {"action": "ticket", "consecutive_failures": 2, "ticket_outcome": "opened"})
        [ticket] = self.store.list_tickets("alice")
        original = ticket["summary"]
        self.assertEqual(original["trigger"][0]["trigger"], "technical_failure")
        self.assertEqual([card["status"] for card in original["task_state"]["cards"]], ["pending"])
        self.assertEqual(original["task_state"]["return_requests"], [])
        self.assertEqual([item["request_id"] for item in original["attempts"]], ["turn-1"])
        next_run = self.begin("turn-3")
        self.finish(next_run)
        committed = self.confirm(self.store.get_session("alice", self.sid), "confirm-later")
        self.assertEqual(committed["card"]["status"], "consumed")
        [ticket] = self.store.list_tickets("alice")
        self.assertEqual(ticket["summary"], original)
        self.assertEqual(self.failure_log("alice"), [])

    def test_only_graph_business_tool_names_are_recorded(self):
        run = self.begin()
        for name in ("get_order", "prepare_return", "search_public_policy", "compose_policy_answer"):
            self.store.reserve_tool_call("alice", self.sid, run, tool_name=name, tool_call_id="call-" + name)
        names = [event["details"]["tool_name"] for event in self.store.get_run("alice", self.sid, run)["events"]
                 if event["kind"] == "tool_reserved"]
        self.assertEqual(names, ["get_order", "prepare_return", "unknown_tool", "unknown_tool"])

    def mark_old_schema(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(f"PRAGMA user_version = {SCHEMA_VERSION - 1};")

    def test_old_schema_database_is_renamed_to_a_timestamped_backup_and_rebuilt(self):
        run = self.begin()
        self.prepare(run)
        self.finish(run)
        self.confirm(self.store.get_session("alice", self.sid), "confirm-before-rebuild")
        self.mark_old_schema()
        old_bytes = self.path.read_bytes()
        self.store = SessionStore(self.path, clock=lambda: self.now)
        backup = self.store.backup_path
        self.assertEqual(backup.parent, self.path.parent)
        self.assertRegex(backup.name, r"^runtime\.sqlite3\.bak-\d{8}T\d{12}Z(-\d+)?$")
        self.assertEqual(backup.read_bytes(), old_bytes)  # renamed, not rewritten
        with closing(sqlite3.connect(backup)) as old:
            self.assertEqual(old.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0], 1)
        connection = self.store._connection
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.error("SESSION_NOT_FOUND", self.store.get_session, "alice", self.sid)
        self.assertEqual(self.store.list_return_requests(), [])
        self.assertEqual([row[0] for row in connection.execute("SELECT order_id FROM orders ORDER BY order_id")],
                         ["O1001", "O1002", "O1003", "O1004", "O2001"])
        self.assertNotIn("legacy_imported", {row[1] for row in connection.execute("PRAGMA table_info(session_runs)")})
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"orders", "return_requests", "sessions", "session_messages",
                                  "session_requests", "return_cards", "session_runs", "run_events", "handoffs",
                                  "tickets", "ticket_events"})
        snapshot = self.store.create_session("alice")
        self.assertEqual(snapshot["execution_totals"], {"runs": 0, "model_attempts": 0, "tool_calls": 0})
        self.assertNotIn("policy_memory", snapshot)

    def test_each_rebuild_keeps_its_own_backup_and_a_current_database_is_left_alone(self):
        backups = []
        for _ in range(2):
            self.mark_old_schema()
            self.store = SessionStore(self.path, clock=lambda: self.now)
            backups.append(self.store.backup_path)
        self.assertEqual(len(set(backups)), 2)
        self.assertTrue(all(path.is_file() for path in backups))
        self.store.close()
        files, current = sorted(self.path.parent.iterdir()), self.path.read_bytes()
        self.store = SessionStore(self.path, clock=lambda: self.now)
        self.assertIsNone(self.store.backup_path)
        self.assertEqual(sorted(self.path.parent.iterdir()), files)
        self.assertEqual(self.path.read_bytes(), current)

    def test_backup_name_taken_in_the_same_microsecond_gets_a_suffix_and_is_not_overwritten(self):
        fixed = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)

        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        self.mark_old_schema()
        old_bytes = self.path.read_bytes()
        taken = self.path.with_name(self.path.name + ".bak-20260102T030405678901Z")
        taken.write_bytes(b"an earlier backup")
        with mock.patch.object(sessions, "datetime", FrozenClock):
            self.store = SessionStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.backup_path, taken.with_name(taken.name + "-1"))
        self.assertEqual(self.store.backup_path.read_bytes(), old_bytes)
        self.assertEqual(taken.read_bytes(), b"an earlier backup")

    def test_old_database_with_a_leftover_log_file_is_not_renamed(self):
        self.mark_old_schema()
        old_bytes = self.path.read_bytes()
        self.path.with_name(self.path.name + "-wal").write_bytes(b"")
        files = sorted(self.path.parent.iterdir())
        self.error("STORAGE_ERROR", SessionStore, self.path, clock=lambda: self.now)
        self.assertEqual(sorted(self.path.parent.iterdir()), files)
        self.assertEqual(self.path.read_bytes(), old_bytes)


if __name__ == "__main__":
    unittest.main()
