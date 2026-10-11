"""Offline behavioral tests for durable conversations and click-only card confirmation (protocol v3)."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from itertools import count
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from support_agent.domain import SEED_ORDERS
from support_agent.sessions import RETRY_STEP, SCHEMA_VERSION, SessionError, SessionStore


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "sessions.sqlite3"
        self.now = 1_000.0
        self.ids = count()
        self.store = SessionStore(self.db_path, clock=lambda: self.now, confirmation_ttl=60)
        self.addCleanup(lambda: self.store.close())

    def request_id(self):
        return f"request-{next(self.ids)}"

    def assert_session_error(self, code, call, *args, **kwargs):
        with self.assertRaises(SessionError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertTrue(raised.exception.message)
        return raised.exception

    def start(self, session_id, customer="alice", message="我要退货"):
        return self.store.begin_turn(customer, session_id, self.request_id(), message)

    def finish(self, session_id, turn, customer="alice", message="我要退货", reply="请确认"):
        messages = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ]
        return self.store.finish_turn(
            customer, session_id, turn["run_id"], messages,
            {"reply": reply, "status": "completed"},
        )

    def ready(self, customer="alice", order_id="O1001"):
        """A session with a card for one order, prepared inside a finished chat turn."""
        session_id = self.store.create_session(customer)["session_id"]
        turn = self.start(session_id, customer)
        self.store.prepare_card(customer, session_id, turn["run_id"], order_id)
        self.finish(session_id, turn, customer)
        return self.store.get_session(customer, session_id)

    def confirm(self, snapshot, request_id=None, customer="alice", reason_code="changed_mind", reason_text=""):
        card = snapshot["card"]
        return self.store.confirm_card(
            customer, snapshot["session_id"], card["action_id"], card["version"], reason_code, reason_text,
            request_id or self.request_id(),
        )

    def change_order(self, order_id, **changes):
        allowed = {"condition", "delivered_at", "status", "customer_id"}
        self.assertTrue(changes and set(changes) <= allowed)
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                f"UPDATE orders SET {assignments} WHERE order_id = ?",
                (*changes.values(), order_id),
            )
            connection.commit()

    def test_session_access_is_bound_to_customer(self):
        own = self.store.create_session("alice")
        session_id = own["session_id"]
        forbidden = self.assert_session_error(
            "SESSION_NOT_FOUND", self.store.get_session, "bob", session_id,
        )
        missing = self.assert_session_error(
            "SESSION_NOT_FOUND", self.store.get_session, "bob", "missing-session",
        )
        self.assertEqual(forbidden.message, missing.message)
        self.assert_session_error(
            "SESSION_NOT_FOUND", self.store.begin_turn,
            "bob", session_id, self.request_id(), "读取别人的会话",
        )
        self.assert_session_error(
            "SESSION_NOT_FOUND", self.store.cancel,
            "bob", session_id, self.request_id(),
        )
        self.assertEqual(self.store.get_session("alice", session_id), own)

    def test_completed_turn_history_survives_reopen_and_appends_only_delta(self):
        session_id = self.store.create_session("alice")["session_id"]
        first = self.start(session_id, message="查询 O1001")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["history"], [])
        first_messages = [
            {"role": "user", "content": "查询 O1001"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {
                    "name": "get_order", "arguments": '{"order_id":"O1001"}',
                }},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
            {"role": "assistant", "content": "订单可以退货。"},
        ]
        self.store.finish_turn(
            "alice", session_id, first["run_id"], first_messages,
            {"reply": "订单可以退货。", "status": "completed"},
        )
        self.store.close()
        self.store = SessionStore(self.db_path, clock=lambda: self.now, confirmation_ttl=60)
        second = self.start(session_id, message="原因是尺寸不合适")
        self.assertEqual(second["history"], first_messages)
        second_messages = [
            {"role": "user", "content": "原因是尺寸不合适"},
            {"role": "assistant", "content": "已记下原因。"},
        ]
        self.store.finish_turn(
            "alice", session_id, second["run_id"], second_messages,
            {"reply": "已记下原因。", "status": "completed"},
        )
        third = self.start(session_id, message="继续")
        self.assertEqual(third["history"], first_messages + second_messages)

    def test_one_active_turn_per_session_across_connections(self):
        session_id = self.store.create_session("alice")["session_id"]
        first = self.start(session_id)
        with closing(SessionStore(self.db_path, clock=lambda: self.now)) as other:
            self.assert_session_error(
                "BUSY", other.begin_turn, "alice", session_id,
                self.request_id(), "另一个并发请求",
            )
            self.assertTrue(other.get_session("alice", session_id)["busy"])
            self.finish(session_id, first)
            next_turn = other.begin_turn(
                "alice", session_id, self.request_id(), "现在继续",
            )
            self.assertFalse(next_turn["replayed"])

    def test_completed_message_request_replays_and_changed_body_conflicts(self):
        session_id = self.store.create_session("alice")["session_id"]
        request_id = self.request_id()
        first = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        response = self.finish(session_id, first, message="查询订单", reply="查到了")
        self.assertEqual(response["reply"], "查到了")
        self.assertFalse(response["session"]["busy"])
        replay = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], response)
        self.assert_session_error(
            "REQUEST_CONFLICT", self.store.begin_turn,
            "alice", session_id, request_id, "改成创建退货",
        )
        self.assertFalse(self.store.get_session("alice", session_id)["busy"])

    def test_failed_turn_is_cached_and_releases_busy(self):
        session_id = self.store.create_session("alice")["session_id"]
        request_id = self.request_id()
        turn = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.store.fail_turn("alice", session_id, turn["run_id"], "模型暂时不可用")
        self.assertFalse(self.store.get_session("alice", session_id)["busy"])
        replay = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["response"])
        again = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.assertEqual(again["response"], replay["response"])
        self.assert_session_error(
            "REQUEST_CONFLICT", self.store.begin_turn,
            "alice", session_id, request_id, "另一段正文",
        )
        self.assertFalse(self.start(session_id)["replayed"])

    def test_fail_turn_preserves_fallback_metadata_but_overrides_result_and_session(self):
        session_id = self.store.create_session("alice")["session_id"]
        request_id = self.request_id()
        turn = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        message = "本轮执行失败，请重新发起请求。"
        fallback = {
            "mode": "offline_simulation",
            "run_id": "forged-run", "request_id": "forged-request", "replayed": True,
            "trace": [{"tool": "get_order", "result": {"ok": True}}],
            "usage": {"input_tokens": 17, "output_tokens": 5},
            "error": {"code": "SIMULATED_FAILURE", "message": "模拟执行异常"},
            "status": "succeeded", "reply": "这不是应缓存的回复",
            "session": {"session_id": "forged-session", "busy": True, "forged": True},
        }
        self.store.fail_turn(
            "alice", session_id, turn["run_id"], message, response=fallback,
        )
        self.store.close()
        self.store = SessionStore(self.db_path, clock=lambda: self.now, confirmation_ttl=60)
        replay = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.assertTrue(replay["replayed"])
        saved = replay["response"]
        for key in ("mode", "trace", "usage", "error"):
            self.assertEqual(saved[key], fallback[key])
        self.assertEqual(saved["run_id"], turn["run_id"])
        self.assertEqual(saved["request_id"], request_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["reply"], message + "\n" + RETRY_STEP)   # B4b: one next step, written by the store
        self.assertEqual(saved["next_step"], {"action": "retry", "consecutive_failures": 1, "offer_handoff": True})
        self.assertIs(saved["replayed"], False)
        self.assertEqual(saved["session"], self.store.get_session("alice", session_id))
        self.assertEqual(saved["session"]["session_id"], session_id)
        self.assertFalse(saved["session"]["busy"])
        self.assertNotIn("forged", saved["session"])

    def test_model_error_finish_preserves_response_and_history_and_allows_next_turn(self):
        session_id = self.store.create_session("alice")["session_id"]
        request_id = self.request_id()
        turn = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        messages = [
            {"role": "user", "content": "查询订单"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "before-error", "type": "function", "function": {
                    "name": "get_order", "arguments": '{"order_id":"O1001"}',
                }},
            ]},
            {"role": "tool", "tool_call_id": "before-error", "content": '{"ok":true}'},
        ]
        response = {
            "status": "model_error", "reply": "模型暂时不可用，请稍后重试。",
            "error": {"code": "MODEL_UNAVAILABLE", "message": "模拟模型失败"},
            "usage": {"input_tokens": 17, "output_tokens": 5},
            "trace": [{"tool": "get_order", "result": {"ok": True}}],
        }
        saved = self.store.finish_turn("alice", session_id, turn["run_id"], messages, response)
        self.assertEqual({key: saved[key] for key in response if key != "reply"},
                         {key: value for key, value in response.items() if key != "reply"})
        self.assertEqual(saved["reply"], response["reply"] + "\n" + RETRY_STEP)
        self.assertFalse(saved["session"]["busy"])
        self.store.close()
        self.store = SessionStore(self.db_path, clock=lambda: self.now, confirmation_ttl=60)
        replay = self.store.begin_turn("alice", session_id, request_id, "查询订单")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], saved)
        next_turn = self.start(session_id, message="再试一次")
        self.assertFalse(next_turn["replayed"])
        self.assertEqual(next_turn["history"][:len(messages)], messages)
        self.assertEqual(next_turn["history"][-1], {"role": "assistant", "content": saved["reply"]})
        self.finish(session_id, next_turn, message="再试一次")
        self.assertFalse(self.store.get_session("alice", session_id)["busy"])

    def test_run_id_cannot_modify_another_session_or_release_its_busy(self):
        one = self.store.create_session("alice")["session_id"]
        two = self.store.create_session("alice")["session_id"]
        first = self.start(one)
        second = self.start(two)
        for operation in (
            lambda: self.store.prepare_card("alice", two, first["run_id"], "O1001"),
            lambda: self.store.record_resolved_orders("alice", two, first["run_id"], ["O1001"]),
            lambda: self.store.fail_turn("alice", two, first["run_id"], "错误"),
            lambda: self.finish(two, first),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(SessionError):
                    operation()
                self.assertTrue(self.store.get_session("alice", two)["busy"])
        self.assertIsNone(self.store.get_session("alice", two)["card"])
        self.finish(two, second)
        self.assertFalse(self.store.get_session("alice", two)["busy"])

    def test_a_card_needs_an_own_eligible_order_without_a_request(self):
        session_id = self.store.create_session("alice")["session_id"]
        turn = self.start(session_id)
        for order_id, code in (
            ("O2001", "ORDER_NOT_FOUND"),
            ("unknown-order", "ORDER_NOT_FOUND"),
            ("O1002", "RETURN_NOT_ELIGIBLE"),
        ):
            with self.subTest(order_id=order_id):
                self.assert_session_error(
                    code, self.store.prepare_card, "alice", session_id, turn["run_id"], order_id,
                )
        self.assertIsNone(self.store.get_session("alice", session_id)["card"])
        prepared = self.store.prepare_card("alice", session_id, turn["run_id"], "O1001")
        self.assertEqual(prepared["outcome"], "created")
        self.assertEqual(prepared["card"]["order_id"], "O1001")
        self.assertEqual(prepared["card"]["status"], "pending")
        self.assertNotIn("reason", prepared["card"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_confirmation_is_bound_to_customer_session_card_and_version(self):
        prepared = self.ready()
        session_id = prepared["session_id"]
        card = prepared["card"]
        another = self.store.create_session("alice")["session_id"]
        for customer, target, action_id, version, code in (
            ("bob", session_id, card["action_id"], card["version"], "SESSION_NOT_FOUND"),
            ("alice", another, card["action_id"], card["version"], "CONFIRMATION_INVALID"),
            ("alice", session_id, "different-action", card["version"], "CONFIRMATION_INVALID"),
            ("alice", session_id, card["action_id"], card["version"] + 1, "CONFIRMATION_INVALID"),
        ):
            with self.subTest(customer=customer, target=target, version=version):
                self.assert_session_error(
                    code, self.store.confirm_card, customer, target, action_id, version, "changed_mind", "",
                    self.request_id(),
                )
        self.assertEqual(self.store.list_return_requests(), [])
        confirmed = self.confirm(prepared)
        self.assertEqual(confirmed["card"]["status"], "consumed")
        self.assertEqual(confirmed["return_request"]["order_id"], "O1001")

    def test_preparing_the_same_order_again_shows_the_same_card(self):
        session_id = self.store.create_session("alice")["session_id"]
        turn = self.start(session_id)
        first = self.store.prepare_card("alice", session_id, turn["run_id"], "O1001")
        self.now += 10
        again = self.store.prepare_card("alice", session_id, turn["run_id"], "O1001")
        self.assertEqual(again["outcome"], "reshown")
        self.assertEqual(again["card"], first["card"])
        self.assertEqual(self.store.list_return_requests(), [])

    def test_the_reason_comes_only_from_the_click(self):
        prepared = self.ready()
        result = self.confirm(prepared, reason_code="size_or_style")
        self.assertEqual(result["return_request"]["order_id"], "O1001")
        row = self.store.list_return_requests()[0]
        self.assertEqual((row["reason_code"], row["reason_text"], row["source_action_id"]),
                         ("size_or_style", None, prepared["card"]["action_id"]))

    def test_a_switch_voids_the_dropped_card_and_prepares_the_new_order(self):
        # B7: "不退那单，改退这单" is a chat withdrawal plus a return in one turn.
        prepared = self.ready()
        session_id = prepared["session_id"]
        turn = self.start(session_id, message="不退这单了，改退 O1004")
        self.store.void_cards("alice", session_id, turn["run_id"], [prepared["card"]["action_id"]], "chat_withdraw")
        replaced = self.store.prepare_cards("alice", session_id, turn["run_id"], ["O1004"])[0]
        self.finish(session_id, turn, message="不退这单了，改退 O1004")
        self.assertEqual(replaced["outcome"], "created")
        old = self.store._connection.execute("SELECT status, void_reason FROM return_cards WHERE action_id = ?",
                                             (prepared["card"]["action_id"],)).fetchone()
        self.assertEqual(tuple(old), ("voided", "chat_withdraw"))
        self.assert_session_error("CONFIRMATION_INVALID", self.confirm, prepared)
        refreshed = self.store.get_session("alice", session_id)
        self.confirm(refreshed)
        self.assertEqual([row["order_id"] for row in self.store.list_return_requests()], ["O1004"])

    def test_an_expired_card_is_replaced_by_a_fresh_one(self):
        prepared = self.ready()
        session_id = prepared["session_id"]
        self.now = prepared["card"]["expires_at"] + 1
        self.assert_session_error("CONFIRMATION_EXPIRED", self.confirm, prepared)
        self.assertEqual(self.store.list_return_requests(), [])
        turn = self.start(session_id)
        renewed = self.store.prepare_card("alice", session_id, turn["run_id"], "O1001")
        self.finish(session_id, turn)
        self.assertEqual(renewed["outcome"], "created")
        refreshed = self.store.get_session("alice", session_id)
        self.assertNotEqual(refreshed["card"]["action_id"], prepared["card"]["action_id"])
        self.assertEqual(refreshed["card"]["version"], prepared["card"]["version"] + 1)
        self.assertGreater(refreshed["card"]["expires_at"], self.now)
        self.confirm(refreshed)
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_confirmation_rechecks_eligibility_after_prepare(self):
        prepared = self.ready()
        self.change_order("O1001", condition="used_or_impaired")
        error = self.assert_session_error("RETURN_NOT_ELIGIBLE", self.confirm, prepared)
        self.assertIn("商品已使用或不再完好", error.message)
        self.assertNotIn("教学", error.message)
        self.assertEqual(self.store.list_return_requests(), [])
        card = self.store.get_session("alice", prepared["session_id"])["card"]
        self.assertEqual((card["status"], card["void_reason"]), ("voided", "eligibility_changed"))

    def test_failed_confirmation_replays_error_until_a_new_request_is_used(self):
        prepared = self.ready()
        request_id = self.request_id()
        failed = self.assert_session_error("INVALID_REASON", self.confirm, prepared, request_id, reason_code="other",
                                           reason_text="   ")
        replayed = self.assert_session_error("INVALID_REASON", self.confirm, prepared, request_id, reason_code="other",
                                             reason_text="   ")
        self.assertEqual(replayed.message, failed.message)
        self.assert_session_error("REQUEST_CONFLICT", self.confirm, prepared, request_id)
        self.assertEqual(self.store.list_return_requests(), [])
        result = self.confirm(prepared, self.request_id())
        self.assertEqual(result["card"]["status"], "consumed")
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_confirmation_expires_at_exact_boundary(self):
        prepared = self.ready()
        self.now = prepared["card"]["expires_at"]
        self.assert_session_error("CONFIRMATION_EXPIRED", self.confirm, prepared)
        self.assertEqual(self.store.list_return_requests(), [])
        expired = self.store.get_session("alice", prepared["session_id"])
        self.assertEqual(expired["card"]["status"], "expired")
        self.assertIsNone(expired["return_request"])

    def test_snapshot_shows_a_lapsed_card_as_expired_without_writing(self):
        prepared = self.ready()
        action_id = prepared["card"]["action_id"]
        self.now = prepared["card"]["expires_at"] + 1
        snapshot = self.store.get_session("alice", prepared["session_id"])
        self.assertEqual(snapshot["card"]["action_id"], action_id)
        self.assertEqual(snapshot["card"]["status"], "expired")
        stored = self.store._connection.execute(
            "SELECT status FROM return_cards WHERE action_id = ?", (action_id,)).fetchone()
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(self.store.list_return_requests(), [])

    def test_confirmation_rechecks_changed_owner_even_when_another_session_created_return(self):
        for existing_return, order_id in ((False, "O1001"), (True, "O1004")):
            with self.subTest(existing_return=existing_return):
                prepared = self.ready(order_id=order_id)
                if existing_return:
                    # Another session of the same customer already submitted this order.
                    self.confirm(self.ready(order_id=order_id))
                before = self.store.list_return_requests()
                self.change_order(order_id, customer_id="bob")
                self.assert_session_error("ORDER_NOT_FOUND", self.confirm, prepared)
                self.assertEqual(self.store.list_return_requests(), before)
                after = self.store.get_session("alice", prepared["session_id"])
                self.assertEqual(after["card"]["status"], "pending")
                self.assertIsNone(after["return_request"])

    def test_two_sessions_with_cards_for_one_order_keep_the_first_request(self):
        first_prepared = self.ready()
        second_prepared = self.ready()
        first = self.confirm(first_prepared, reason_code="quality_issue")
        second = self.confirm(second_prepared, reason_code="changed_mind")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["card"]["status"], "consumed")
        self.assertEqual(second["return_request"], first["return_request"])
        rows = self.store.list_return_requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason_code"], "quality_issue")

    def test_confirm_replay_returns_original_result_even_if_order_later_ineligible(self):
        prepared = self.ready()
        request_id = self.request_id()
        first = self.confirm(prepared, request_id)
        self.change_order("O1001", condition="used_or_impaired", delivered_at="2026-09-01")
        self.store.close()
        self.store = SessionStore(self.db_path, clock=lambda: self.now, confirmation_ttl=60)
        self.assertEqual(self.confirm(prepared, request_id), first)
        again = self.confirm(prepared, self.request_id(), reason_code="other", reason_text="换一个说法")
        self.assertTrue(again["already_submitted"])
        self.assertEqual({key: value for key, value in again.items() if key not in {"already_submitted", "notice"}},
                         first)
        self.assertEqual(len(self.store.list_return_requests()), 1)
        self.assertEqual(self.store.list_return_requests()[0]["reason_code"], "changed_mind")
        self.assert_session_error("REQUEST_CONFLICT", self.confirm, prepared, request_id, reason_code="quality_issue")

    def test_cancel_without_a_running_turn_changes_no_card_and_replays(self):
        # B3b: cancel only controls a run; the open card keeps its state and stays confirmable by a click.
        prepared = self.ready()
        request_id = self.request_id()
        cancelled = self.store.cancel("alice", prepared["session_id"], request_id)
        self.assertEqual(cancelled["card"], prepared["card"])
        self.assertEqual(self.store.cancel("alice", prepared["session_id"], request_id), cancelled)
        self.assertEqual(self.store.list_return_requests(), [])
        self.assertFalse(self.start(prepared["session_id"])["replayed"])

    def test_confirm_before_cancel_preserves_successful_return(self):
        prepared = self.ready()
        first = self.confirm(prepared)
        self.store.cancel("alice", prepared["session_id"], self.request_id())
        after = self.store.get_session("alice", prepared["session_id"])
        self.assertEqual(after["card"]["status"], "consumed")
        self.assertEqual(after["return_request"], first["return_request"])
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_a_session_goes_on_after_a_submission(self):
        # B3c (D4): no terminal state; a new turn and a card for another order are both accepted.
        prepared = self.ready()
        session_id = prepared["session_id"]
        submitted = self.confirm(prepared)
        self.assertFalse(self.start(session_id)["replayed"])
        self.store.recover("alice", session_id, self.request_id())
        card = self.store.request_card("alice", session_id, "O1004", self.request_id())
        self.assertEqual((card["card_outcome"], card["card"]["order_id"]), ("created", "O1004"))
        self.assertEqual([row["order_id"] for row in card["open_cards"]], ["O1004"])
        self.assertEqual(card["return_request"], submitted["return_request"])

    def test_confirmation_is_busy_during_an_active_turn(self):
        prepared = self.ready()
        turn = self.start(prepared["session_id"])
        request_id = self.request_id()
        self.assert_session_error("BUSY", self.confirm, prepared, request_id)
        self.assertEqual(self.store.list_return_requests(), [])
        self.finish(prepared["session_id"], turn)
        self.confirm(prepared, request_id)
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_concurrent_confirm_retry_uses_one_durable_result(self):
        prepared = self.ready()
        request_id = self.request_id()
        barrier = threading.Barrier(2)

        def confirm_from_connection():
            with closing(SessionStore(self.db_path, clock=lambda: self.now)) as other:
                barrier.wait(timeout=10)
                card = prepared["card"]
                return other.confirm_card(
                    "alice", prepared["session_id"], card["action_id"], card["version"], "changed_mind", "",
                    request_id,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm_from_connection) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["card"]["status"], "consumed")
        self.assertEqual(len(self.store.list_return_requests()), 1)

    def test_distinct_concurrent_confirm_requests_cannot_duplicate_return(self):
        prepared = self.ready()
        barrier = threading.Barrier(2)
        request_ids = [self.request_id(), self.request_id()]

        def confirm_from_connection(request_id):
            with closing(SessionStore(self.db_path, clock=lambda: self.now)) as other:
                barrier.wait(timeout=10)
                card = prepared["card"]
                try:
                    return other.confirm_card(
                        "alice", prepared["session_id"], card["action_id"], card["version"], "changed_mind", "",
                        request_id,
                    )
                except SessionError as error:
                    return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm_from_connection, request_id) for request_id in request_ids]
            results = [future.result(timeout=20) for future in futures]
        successes = [result for result in results if isinstance(result, dict)]
        self.assertGreaterEqual(len(successes), 1)
        for result in successes:
            self.assertEqual(result["card"]["status"], "consumed")
        self.assertEqual(len(self.store.list_return_requests()), 1)


class SessionSchemaTests(unittest.TestCase):
    """S3-3c and B3a (plan D2): no migration; an older database is renamed aside and a fresh one is seeded."""

    def test_an_older_database_is_renamed_aside_and_replaced_by_a_fresh_seeded_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            # A store-only database of an earlier version: orders, and requests with a free-text reason.
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("""CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    product_name TEXT NOT NULL, category TEXT NOT NULL, condition TEXT NOT NULL,
                    delivered_at TEXT NOT NULL, paid_amount_cents INTEGER NOT NULL, status TEXT NOT NULL)""")
                connection.execute("""CREATE TABLE return_requests (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    order_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE (customer_id, order_id))""")
                connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       [tuple(asdict(order).values()) for order in SEED_ORDERS])
                connection.execute("UPDATE orders SET product_name = '保留的旧商品' WHERE order_id = 'O1004'")
                connection.execute("INSERT INTO return_requests VALUES "
                                   "('RET-O1001','alice','O1001','迁移前的申请','pending','2026-09-18')")
                connection.commit()
                old_orders = connection.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            old_bytes = path.read_bytes()
            before_files = set(Path(directory).iterdir())
            with closing(SessionStore(path)) as rebuilt:
                backup = rebuilt.backup_path
                self.assertEqual(rebuilt.list_return_requests(), [])
                created = rebuilt.create_session("alice")
                self.assertEqual(rebuilt.get_session("alice", created["session_id"])["session_id"], created["session_id"])
            self.assertEqual(set(Path(directory).iterdir()) - before_files, {backup})
            self.assertTrue(backup.name.startswith("legacy.sqlite3.bak-"))
            self.assertEqual(backup.read_bytes(), old_bytes)
            with closing(sqlite3.connect(backup)) as old:
                self.assertEqual(old.execute("SELECT * FROM orders ORDER BY order_id").fetchall(), old_orders)
                self.assertEqual(old.execute("SELECT reason FROM return_requests").fetchall(), [("迁移前的申请",)])
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(connection.execute("SELECT * FROM orders ORDER BY order_id").fetchall(),
                                 [tuple(asdict(order).values()) for order in sorted(SEED_ORDERS, key=lambda o: o.order_id)])
            existing_files = set(Path(directory).iterdir())
            with closing(SessionStore(path)):
                pass
            self.assertEqual(set(Path(directory).iterdir()), existing_files)

    def test_unrecognised_database_is_rejected_without_rename_or_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foreign.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE notes (value TEXT NOT NULL)")
                connection.execute("INSERT INTO notes VALUES ('preserve this')")
                connection.commit()
            before_bytes, before_files = path.read_bytes(), set(Path(directory).iterdir())
            with self.assertRaises(SessionError) as caught:
                SessionStore(path)
            self.assertEqual(caught.exception.code, "INVALID_SCHEMA")
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(set(Path(directory).iterdir()), before_files)

    def test_future_schema_version_is_rejected_without_changing_database_or_creating_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE future_data (value TEXT NOT NULL)")
                connection.execute("INSERT INTO future_data VALUES ('preserve this')")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
                connection.commit()
            before_bytes = path.read_bytes()
            before_files = set(Path(directory).iterdir())
            with self.assertRaises(SessionError):
                SessionStore(path)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(set(Path(directory).iterdir()), before_files)


if __name__ == "__main__":
    unittest.main()
