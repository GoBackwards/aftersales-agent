"""Persistent execution limits for the conversation entry points.

Reservations precede external calls, so an unknown/lost response still costs one
attempt. Recovery never resets or resumes that run's budget.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from .sessions import SessionError, SessionStore


class RunStopped(Exception):
    """A controller's sanitized, intentional runtime termination."""

    def __init__(self, status: str, public_message: str):
        self.status = status
        self.public_message = public_message
        super().__init__(public_message)


@dataclass(frozen=True)
class RuntimeLimits:
    run_timeout: float = 120.0
    model_timeout: float = 45.0
    max_model_attempts: int = 8
    max_tool_calls: int = 32

    def __post_init__(self):
        for name, maximum in (("run_timeout", 600), ("model_timeout", 45)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be finite, positive and <= {maximum}")
        for name, maximum in (("max_model_attempts", 8), ("max_tool_calls", 32)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


STOP_CODES = {
    "RUN_CANCELLED": ("cancelled", "本轮已取消；已经提交的业务结果以任务状态为准。"),
    "RUN_INTERRUPTED": ("interrupted", "本轮已中断；请查看任务状态后用新请求继续。"),
    "RUN_TIMEOUT": ("timed_out", "本轮已达到运行时间上限，请查看任务状态。"),
    "MODEL_BUDGET": ("budget_exhausted", "本轮模型调用次数已用尽，任务停止。"),
    "TOOL_BUDGET": ("budget_exhausted", "本轮工具调用次数已用尽，任务停止。"),
}


class SessionRunControl:
    def __init__(self, store: SessionStore, customer_id: str, session_id: str,
                 run_id: str, limits: RuntimeLimits, *, sleeper=time.sleep):
        self.store = store
        self.identity = (customer_id, session_id, run_id)
        self.limits = limits
        self.sleeper = sleeper

    def _invoke(self, method, *args, **kwargs):
        try:
            return method(*self.identity, *args, **kwargs)
        except SessionError as exc:
            if exc.code in STOP_CODES:
                raise RunStopped(*STOP_CODES[exc.code]) from None
            raise

    def check(self):
        self._invoke(self.store.check_run)

    def before_model(self):
        state = self._invoke(self.store.reserve_model_attempt)
        return min(self.limits.model_timeout, state["remaining_seconds"])

    def event(self, kind, details):
        self._invoke(self.store.record_run_event, kind, details)

    def sleep(self, seconds):
        # Polling is limited to retry backoff, never a database transaction. The
        # provider call itself remains bounded by its socket timeout.
        remaining = max(0.0, min(float(seconds), 2.0))
        self.check()
        while remaining > 0:
            chunk = min(remaining, 0.05)
            self.sleeper(chunk)
            remaining -= chunk
            self.check()
