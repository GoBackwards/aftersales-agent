"""Offline regression scenarios using the real loopback HTTP service and temporary SQLite databases.

    python -X utf8 scripts/eval/golden.py [--scenarios tests/golden]
        [--only <scenario_id> ...] [--out <new json file>]

Every model response is scripted. No provider, external network address, .env file or API key is used.
Each scenario asserts a versioned response projection and database state. Unexpected or unused model
calls, incorrect HTTP responses and incorrect writes fail the scenario. These are engineering checks,
not measurements of real-model task success or semantic accuracy.

A scenario with "knowledge": "local_release" needs a user-provided frozen knowledge release at
var/knowledge-store. If absent, that scenario is explicitly reported as skipped, never as passed.
Policy unit/integration tests use separate invented sources; no real policy is fabricated here.

Scenario files contain customers and ordered steps. A step declares a request, scripted plan_turn /
respond results, optional saved response fields, and expected status, response projection, model calls,
model request schemas and database rows/counts. Placeholders such as {session_id} use saved values.
Assertion operators include $absent, $ne, $set, $contains, $lacks, $len and $ordered. Validation rejects
unknown scenario fields and projection keys before any request is executed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import http.client
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading

APP = Path(__file__).resolve().parents[2]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

SCHEMA = "golden-scenario-v1"
REPORT_SCHEMA = "golden-report-v1"
DEFAULT_SCENARIOS = APP / "tests" / "golden"
PROJECTION_VERSION = "golden-projection-v14"
# B6: the local frozen release a scenario may ask for ("knowledge": "local_release"); never modified here.
LOCAL_RELEASE = APP / "var" / "knowledge-store"
KNOWLEDGE_OPTIONS = ("local_release",)
ABSENT = "<absent>"
PLAN_TOOL, ANSWER_TOOL = "plan_turn", "respond"
# Minimal scripted proposals; the agent's own schema validation fills every other field.
PLAN_SKELETON = {"goal": "按脚本给出的需求", "items": [], "order_stage": "unknown", "fee_flow": "unknown"}
ITEM_SKELETON = {"subkind": "none", "history_index": None, "order_quote": "", "order_id": "none", "fee_subject": "none"}
# The response projection: dotted paths (messages return an envelope, commands a session snapshot).
PROJECTED_PATHS = (
    "ok", "error.code", "error.message", "reply",
    "status", "run_status", "policy_status", "replayed", "conversation_version",
    "confirmation.status", "confirmation.protocol", "confirmation.order_id", "confirmation.version",
    "confirmation.reason",
    "session.customer_id", "session.busy",
    "session.card", "session.card.status", "session.card.order_id", "session.card.version",
    "session.card.void_reason", "session.return_request", "session.return_request.order_id",
    "session.return_request.status",
    "handoff", "next_step.action", "next_step.consecutive_failures", "next_step.ticket_outcome",
    "next_step.offer_handoff",
    "customer_id", "busy", "notice", "message_limit",
    "card", "card.status", "card.order_id", "card.version", "card.void_reason",
    "card_outcome", "confirmation_message", "already_submitted",
    "return_request", "return_request.order_id", "return_request.status",
    "ticket_outcome", "ticket.short_no", "ticket.status", "last_reply",
)
COMPUTED_KEYS = ("@block_types", "@business_status", "@business_error", "@reply_order_ids",
                 "@listed_order_ids", "@listed_session_ids", "@ticket_count", "@ticket_customer_ids",
                 "@ticket_statuses", "@ticket_events", "@ticket_needs", "@ticket_card",
                 "@history_entries", "@demo_words", "@trace_types", "@card_block",
                 "@open_card_orders", "@confirmation_card_orders", "@card_block_orders", "@return_outcomes",
                 "@order_fact_orders", "@block_authors", "@card_blocks", "@fallback", "@text_blocks", "@dropped")
# User-visible text fields counted by @demo_words (B1); presentation blocks add text, lead and note.
DEMO_WORD_PATHS = ("reply", "error.message", "last_reply", "session.last_reply")
PROJECTION_KEYS = frozenset(PROJECTED_PATHS) | frozenset(COMPUTED_KEYS)
STEP_FIELDS = {"id", "as", "call", "calls", "body", "headers", "model", "save", "expect", "note"}
EXPECT_FIELDS = {"status", "response", "model_calls", "model_requests", "db"}
MODEL_REQUEST_FIELDS = {"tool", "strict", "offered", "order_choices", "context_order_ids", "question_fields", "plan_kinds",
                        "item_kinds", "context_keys", "card_choices", "context_card_ids", "item_orders"}
DB_FIELDS = {"unchanged", "unchanged_except", "changed", "counts", "rows"}
SCENARIO_FIELDS = {"schema_version", "scenario_id", "summary", "covers", "customers", "steps", "knowledge"}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ORDER_ID = re.compile(r"(?<![A-Za-z0-9_-])O\d{4}(?![A-Za-z0-9_-])")
_CALL = re.compile(r"(GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) (/\S*)\Z")
_COUNT_KEY = re.compile(r"([a-z_]+)(?:\[([^\]]+)\])?\Z")


class ScenarioError(ValueError):
    """The scenario file itself is malformed (not a behaviour failure)."""


def tables():
    from support_agent.sessions import TABLES
    return TABLES


# ----- loading and validation --------------------------------------------------------------------------
def load_scenarios(directory=DEFAULT_SCENARIOS, *, only=None):
    paths = sorted(Path(directory).glob("*.json"))
    scenarios = []
    for path in paths:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        validate_scenario(scenario, source=path.name)
        if only and scenario["scenario_id"] not in only:
            continue
        scenarios.append(scenario)
    ids = [s["scenario_id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ScenarioError("duplicate scenario_id")
    return scenarios


def _check(condition, message, source):
    if not condition:
        raise ScenarioError(f"{source}: {message}")


def validate_scenario(scenario, *, source="<scenario>"):
    known_tables = set(tables())
    _check(type(scenario) is dict and set(scenario) <= SCENARIO_FIELDS
           and scenario.get("schema_version") == SCHEMA, "unknown fields or schema_version", source)
    _check(type(scenario.get("scenario_id")) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", scenario["scenario_id"]),
           "scenario_id", source)
    _check(scenario.get("knowledge", KNOWLEDGE_OPTIONS[0]) in KNOWLEDGE_OPTIONS, "knowledge", source)
    customers = scenario.get("customers")
    _check(type(customers) is list and customers and all(type(c) is str and re.fullmatch(r"[a-z]{1,20}", c)
                                                          for c in customers), "customers", source)
    steps = scenario.get("steps")
    _check(type(steps) is list and steps, "steps", source)
    seen = set()
    for step in steps:
        where = f"{source} step {step.get('id') if type(step) is dict else '?'}"
        _check(type(step) is dict and set(step) <= STEP_FIELDS and type(step.get("id")) is str
               and step["id"] not in seen, "fields or duplicate id", where)
        seen.add(step["id"])
        _check(("call" in step) != ("calls" in step), "exactly one of call/calls", where)
        calls = [step["call"]] if "call" in step else step["calls"]
        _check(type(calls) is list and calls and all(type(c) is str and _CALL.match(c) for c in calls),
               "call must be 'METHOD /path'", where)
        _check(step.get("as") is None or step["as"] in customers, "as must name a customer or be null", where)
        for entry in step.get("model", []):
            _check(type(entry) is dict and len(set(entry) & {"plan", "answer", "tool", "raise"}) == 1,
                   "model entries are plan/answer/tool/raise", where)
            if "tool" in entry:
                _check(set(entry) == {"tool", "arguments"} and type(entry["arguments"]) is dict, "tool entry", where)
            if "raise" in entry:
                _check(entry["raise"] == "model_error" and set(entry) <= {"raise", "retryable", "status_code"},
                       "raise entry", where)
        _check(type(step.get("save", {})) is dict, "save", where)
        expect = step.get("expect", {})
        _check(type(expect) is dict and set(expect) <= EXPECT_FIELDS, "expect fields", where)
        requests = expect.get("model_requests", [])
        _check(type(requests) is list and all(type(entry) is dict and entry and set(entry) <= MODEL_REQUEST_FIELDS
                                              for entry in requests), "model_requests entries", where)
        _check(set(expect.get("response", {})) <= PROJECTION_KEYS,
               "response assertions must name projection keys: "
               + ", ".join(sorted(set(expect.get("response", {})) - PROJECTION_KEYS)), where)
        db = expect.get("db", {})
        _check(type(db) is dict and set(db) <= DB_FIELDS, "db fields", where)
        _check(not ({"unchanged", "unchanged_except"} <= set(db)), "unchanged and unchanged_except together", where)
        named = []
        if db.get("unchanged") not in (None, "all"):
            named += db["unchanged"]
        named += db.get("unchanged_except", []) + db.get("changed", []) + list(db.get("rows", {}))
        named += [_COUNT_KEY.match(key)[1] if _COUNT_KEY.match(key) else key for key in db.get("counts", {})]
        _check(set(named) <= known_tables, "unknown table: " + ", ".join(sorted(set(named) - known_tables)), where)
    return scenario


# ----- the scripted model stand-in ---------------------------------------------------------------------
class ScriptedModel:
    """Answers each call from the current step's script; records every call and every mismatch."""
    mode = "offline_golden_scripted"

    def __init__(self):
        self.queue, self.calls, self.problems = [], [], []
        self.lock = threading.Lock()

    def load(self, script):
        with self.lock:
            self.queue = [deepcopy(entry) for entry in script]

    def complete(self, messages, tools):
        return self.complete_with_timeout(messages, tools, timeout=None)

    def complete_with_timeout(self, messages, tools, *, timeout):
        from support_agent.models import ModelError, ModelResponse, ToolCall
        name = tools[0]["function"]["name"] if tools else None
        offered = tools[0]["function"].get("parameters", {}).get("properties", {}) if tools else {}
        call = {"tool": name, "strict": name in getattr(tools, "strict_functions", ()), "offered": sorted(offered),
                "context_order_ids": sorted(set(_ORDER_ID.findall(messages[1]["content"] if len(messages) > 1 else "")))}
        if "show_order_ids" in offered:
            call["order_choices"] = offered["show_order_ids"]["items"]["enum"]
        kinds = ((offered.get("items") or {}).get("items") or {}).get("properties", {}).get("kind")
        if kinds:
            call["plan_kinds"] = kinds["enum"]
        claims = ((offered.get("items") or {}).get("items") or {}).get("properties", {}).get("claims")
        if claims:
            call["card_choices"] = claims["items"]["properties"]["card_id"]["enum"]
        try:
            context = json.loads(messages[1]["content"]) if len(messages) > 1 else None
        except (TypeError, ValueError):
            context = None
        questions = context.get("policy_questions") if isinstance(context, dict) else None
        if isinstance(questions, list):
            call["question_fields"] = sorted({key for item in questions if isinstance(item, dict) for key in item})
        items = context.get("turn_items") if isinstance(context, dict) else None
        if isinstance(items, list):
            call["item_kinds"] = [item.get("kind") for item in items if isinstance(item, dict)]
            call["item_orders"] = [[item.get("index"), item.get("orders")] for item in items if isinstance(item, dict)]
        if isinstance(context, dict):
            call["context_keys"] = sorted(context)
        cards = context.get("policy_cards") if isinstance(context, dict) else None
        if cards:
            call["context_card_ids"] = [card.get("id") for card in cards if isinstance(card, dict)]
        with self.lock:
            self.calls.append(call)
            if not self.queue:
                self.problems.append(f"unexpected model call for {name}")
                raise ModelError("脚本没有准备这次模型调用。")
            entry = self.queue.pop(0)
        if "raise" in entry:
            raise ModelError("脚本注入的模型错误。", retryable=entry.get("retryable", False),
                             status_code=entry.get("status_code"))
        if "plan" in entry:
            tool, arguments = PLAN_TOOL, {**deepcopy(PLAN_SKELETON), **deepcopy(entry["plan"])}
            arguments["items"] = [{**deepcopy(ITEM_SKELETON), **item} for item in arguments["items"]]
        elif "answer" in entry:
            tool, arguments = ANSWER_TOOL, offered_only(respond_arguments(entry["answer"], context), tools)
        else:
            tool, arguments = entry["tool"], deepcopy(entry["arguments"])
        if tool != name:
            with self.lock:
                self.problems.append(f"model asked for {name}, script has {tool}")
            raise ModelError("脚本与本次请求的函数不一致。")
        return ModelResponse(tool_calls=[ToolCall("golden-" + tool, tool, arguments)], model_id="golden-scripted",
                             usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


def respond_arguments(script, context):
    """A ``respond`` result over the call's own turn items (see the module docstring, model entries)."""
    from support_agent.respond import ANSWER_KINDS
    rows, first, later = [], True, list(script.get("notes", []))
    for item in (context or {}).get("turn_items") or []:
        if item.get("kind") not in ANSWER_KINDS:
            rows.append({"index": item["index"], "disposition": "handled_by_system", "claims": [], "path_id": "none",
                         "note": ""})
            continue
        claims = deepcopy(script.get("claims", [])) if first else []
        default = "answered" if claims else "gap"
        rows.append({"index": item["index"], "disposition": script.get("disposition", default) if first else "gap",
                     "claims": claims, "path_id": script.get("path", "none") if first else "none",
                     "note": script.get("note", "") if first else (later.pop(0) if later else "")})
        first = False
    return {"items": rows, "show_order_ids": list(script.get("show", [])), "next_step": script.get("next_step", ""),
            "ask": deepcopy(script.get("ask", {"text": "", "option_set_id": "none"}))}


def offered_only(value, tools):
    """Only the fields the call's schema offers, as a strict provider returns them (B5a)."""
    properties = tools[0]["function"].get("parameters", {}).get("properties", {}) if tools else {}
    value = {key: item for key, item in value.items() if key in properties}
    item_schema = (properties.get("items") or {}).get("items", {}).get("properties", {})
    if "items" in value and "claims" not in item_schema:
        value["items"] = [{key: item for key, item in row.items() if key != "claims"} for row in value["items"]]
    return value


class ScriptedFactory:
    mode = ScriptedModel.mode

    def __init__(self, model):
        self.model = model

    def __call__(self):
        return self.model


# ----- projection, placeholders, comparisons ---------------------------------------------------------
def dotted(value, path):
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return ABSENT
    return value


def project(response):
    """The projection of one HTTP response: projected paths that are present, plus computed keys."""
    result = {}
    for path in PROJECTED_PATHS:
        value = dotted(response, path)
        if value != ABSENT:
            result[path] = value
    if not isinstance(response, dict):
        return result
    blocks = (response.get("presentation") or {}).get("blocks")
    if isinstance(blocks, list):
        result["@block_types"] = [block.get("type") for block in blocks if isinstance(block, dict)]
    declared = (blocks if isinstance(blocks, list) else []) + [response.get("return_card")]
    card = next((block for block in declared if isinstance(block, dict) and block.get("type") == "return_card"), None)
    if card is not None:
        result["@card_block"] = {"order_id": card.get("order_id"), "version": card.get("version"),
                                 "reason_codes": [option.get("code") for option in
                                                  (card.get("reasons") or {}).get("options", [])],
                                 "carries_reason": any(key in card for key in ("reason", "reason_code", "reason_text"))}
    if isinstance(blocks, list):
        result["@block_authors"] = [[block.get("type"), block.get("author")] for block in blocks
                                    if isinstance(block, dict)]
        result["@card_blocks"] = [[block.get("card_id"), [row.get("outcome_index") for row in block.get("selected") or []],
                                   [row.get("outcome_index") for row in block.get("other_outcomes") or []]]
                                  for block in blocks if isinstance(block, dict) and block.get("type") == "policy_card"]
        result["@card_block_orders"] = [block.get("order_id") for block in blocks
                                        if isinstance(block, dict) and block.get("type") == "return_card"]
        result["@order_fact_orders"] = [order.get("order_id") for block in blocks
                                        if isinstance(block, dict) and block.get("type") == "order_facts"
                                        for order in block.get("orders") or [] if isinstance(order, dict)]
        result["@text_blocks"] = [[block.get("author"), block.get("item", "next_step" if block.get("next_step") else
                                                                   "fallback" if block.get("fallback") else None),
                                   block.get("disposition")]
                                  for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
    snapshot = response.get("session") if isinstance(response.get("session"), dict) else response
    if isinstance(snapshot.get("open_cards"), list):
        result["@open_card_orders"] = [card.get("order_id") for card in snapshot["open_cards"] if isinstance(card, dict)]
    confirmation = response.get("confirmation")
    if isinstance(confirmation, dict) and isinstance(confirmation.get("cards"), list):
        result["@confirmation_card_orders"] = [card.get("order_id") for card in confirmation["cards"]
                                               if isinstance(card, dict)]
    validation = response.get("answer_validation")
    if isinstance(validation, dict) and "fallback" in validation:
        result["@fallback"] = validation["fallback"]
    trace = response.get("trace")
    if isinstance(trace, list):
        result["@trace_types"] = [entry.get("type") for entry in trace if isinstance(entry, dict)]
        result["@dropped"] = sorted({(row.get("code"), row.get("part")) for entry in trace if isinstance(entry, dict)
                                     for row in (entry.get("hits") if entry.get("type") == "guards" else
                                                 entry.get("dropped") if entry.get("type") == "structure_tidied"
                                                 else None) or []})
        result["@dropped"] = [list(pair) for pair in result["@dropped"]]
        business = next((entry.get("result") for entry in reversed(trace)
                         if isinstance(entry, dict) and entry.get("type") == "business"), None)
        if isinstance(business, dict):
            result["@business_status"] = business.get("status")
            result["@business_error"] = (business.get("error") or {}).get("code")
            if isinstance(business.get("return_outcomes"), list):
                result["@return_outcomes"] = [[row.get("order_id"), row.get("outcome")]
                                              for row in business["return_outcomes"] if isinstance(row, dict)]
    if isinstance(response.get("reply"), str):
        result["@reply_order_ids"] = sorted(set(_ORDER_ID.findall(response["reply"])))
    if isinstance(response.get("orders"), list):
        result["@listed_order_ids"] = sorted(order.get("order_id") for order in response["orders"])
    if isinstance(response.get("sessions"), list):
        result["@listed_session_ids"] = sorted(item.get("session_id") for item in response["sessions"])
    if isinstance(response.get("tickets"), list):
        result["@ticket_count"] = len(response["tickets"])
        result["@ticket_customer_ids"] = sorted({item.get("customer_id") for item in response["tickets"]})
        result["@ticket_statuses"] = [[item.get("short_no"), item.get("status")] for item in response["tickets"]]
        result["@ticket_events"] = [[item.get("short_no"), event.get("kind"), event.get("trigger")]
                                    for item in response["tickets"] for event in item.get("events") or []]
        result["@ticket_needs"] = [[item.get("short_no"), dotted(item, "summary.user_need.text")]
                                   for item in response["tickets"]]
    ticket = response.get("ticket_card")
    if isinstance(ticket, dict) and ticket.get("type") == "ticket":
        result["@ticket_card"] = {key: ticket.get(key) for key in ("short_no", "status", "actions")}
    if isinstance(response.get("entries"), list):
        result["@history_entries"] = [entry.get("entry") for entry in response["entries"]]
    result["@demo_words"] = demo_words(response)
    return result


def demo_words(response):
    """A0 word occurrences in the user-visible text of one response (the environment notice excluded)."""
    from support_agent.guards import A0_WORDS
    texts = [dotted(response, path) for path in DEMO_WORD_PATHS]
    blocks = (response.get("presentation") or {}).get("blocks")
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict):
            texts += [block.get(key) for key in ("text", "lead", "note")]
    orders = response.get("orders")
    for order in orders if isinstance(orders, list) else []:
        if isinstance(order, dict):
            texts.append(order.get("eligibility_text"))
    return sum(text.count(word) for text in texts if isinstance(text, str) for word in A0_WORDS)


def fill(value, saved):
    """Replace {name} placeholders; a string that is exactly one placeholder keeps the saved type.

    {"$repeat": [text, n]} becomes text written n times."""
    if isinstance(value, dict) and set(value) == {"$repeat"}:
        repeat = value["$repeat"]
        if not (isinstance(repeat, list) and len(repeat) == 2 and isinstance(repeat[0], str) and repeat[0]
                and type(repeat[1]) is int and 0 <= repeat[1] <= 100_000):
            raise ScenarioError("$repeat takes [non-empty text, count from 0 to 100000]")
        return repeat[0] * repeat[1]
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            if whole[1] not in saved:
                raise ScenarioError(f"placeholder {{{whole[1]}}} used before it was saved")
            return deepcopy(saved[whole[1]])

        def one(match):
            if match[1] not in saved:
                raise ScenarioError(f"placeholder {{{match[1]}}} used before it was saved")
            return str(saved[match[1]])
        return _PLACEHOLDER.sub(one, value)
    if isinstance(value, list):
        return [fill(item, saved) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, saved) for key, item in value.items()}
    return value


def matches(actual, expected):
    if isinstance(expected, dict) and len(expected) == 1 and next(iter(expected)).startswith("$"):
        operator, operand = next(iter(expected.items()))
        if operator == "$absent":
            return (actual == ABSENT) is bool(operand)
        if operator == "$ne":
            return actual != operand
        if operator == "$set":
            return isinstance(actual, list) and sorted(map(json.dumps, actual)) == sorted(map(json.dumps, operand))
        if operator == "$contains":
            return isinstance(actual, (str, list)) and operand in actual
        if operator == "$lacks":
            return isinstance(actual, (str, list)) and operand not in actual
        if operator == "$ordered":
            positions = [actual.find(part) if isinstance(actual, str) else -1 for part in operand]
            return -1 not in positions and positions == sorted(positions)
        if operator == "$len":
            return isinstance(actual, (str, list, dict)) and len(actual) == operand
        raise ScenarioError(f"unknown operator {operator}")
    return actual == expected


# ----- database snapshots ------------------------------------------------------------------------------
def snapshot(db_path):
    """All rows of every session-schema table, read through a read-only connection."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables()}


def _count(rows, filters):
    if not filters:
        return len(rows)
    pairs = [item.split("=", 1) for item in filters.split(",")]
    return sum(all(str(row.get(column.strip())) == value.strip() for column, value in pairs) for row in rows)


def check_db(expect, before, after, saved):
    problems = []
    changed = sorted(table for table in after if after[table] != before.get(table))
    unchanged = expect.get("unchanged")
    if unchanged is not None:
        names = list(after) if unchanged == "all" else unchanged
        for table in names:
            if table in changed:
                problems.append(f"db.{table}: expected unchanged")
    for table in [t for t in changed if t not in expect.get("unchanged_except", changed)]:
        problems.append(f"db.{table}: changed, only {expect['unchanged_except']} may change")
    for table in expect.get("changed", []):
        if table not in changed:
            problems.append(f"db.{table}: expected a change")
    for key, number in expect.get("counts", {}).items():
        table, filters = _COUNT_KEY.match(key).groups()
        actual = _count(after[table], fill(filters, saved) if filters else None)
        if actual != number:
            problems.append(f"db.counts.{key}: {actual} != {number}")
    for table, rows in expect.get("rows", {}).items():
        rows = fill(rows, saved)
        actual = [{column: row.get(column, ABSENT) for column in wanted} for row, wanted in zip(after[table], rows)]
        if len(after[table]) != len(rows) or actual != rows:
            problems.append(f"db.rows.{table}: {json.dumps(actual, ensure_ascii=False)[:300]}")
    return changed, problems


# ----- running ------------------------------------------------------------------------------------------
class Harness:
    """A fresh database, scripted model and loopback server for one scenario."""

    def __init__(self, customers, knowledge=None):
        from support_agent.service import create_server
        config = None
        if knowledge == "local_release":
            from support_agent.policy import resolve_policy_config
            config = resolve_policy_config(LOCAL_RELEASE)
        self.temporary = tempfile.TemporaryDirectory(prefix="golden-")
        self.db_path = Path(self.temporary.name) / "golden.sqlite3"
        self.tokens = {name: f"golden-{name}-{secrets.token_hex(12)}" for name in customers}
        self.model = ScriptedModel()
        self.server = create_server(db_path=self.db_path, credentials={t: n for n, t in self.tokens.items()},
                                    port=0, model_factory=ScriptedFactory(self.model), public_policy_config=config,
                                    log_path=Path(self.temporary.name) / "requests.jsonl")
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.temporary.cleanup()

    def request(self, customer, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        sent = {}
        if customer is not None:
            sent["Authorization"] = "Bearer " + self.tokens[customer]
        encoded = None
        if body is not None and method == "POST":
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        try:
            connection.request(method, path, encoded, sent)
            raw = connection.getresponse()
            data = raw.read()
            try:
                payload = json.loads(data) if data else None
            except ValueError:
                payload = {"<non-json>": len(data)}
            return raw.status, payload
        finally:
            connection.close()


def run_scenario(scenario):
    validate_scenario(scenario)
    if scenario.get("knowledge") == "local_release" and not LOCAL_RELEASE.exists():
        return {"scenario_id": scenario["scenario_id"], "covers": scenario.get("covers", []), "passed": True,
                "skipped": "local frozen release not present", "steps": [], "failures": []}
    harness = Harness(scenario["customers"], scenario.get("knowledge"))
    saved, rows = {}, []
    try:
        saved["host"] = harness.host
        for step in scenario["steps"]:
            calls = [step["call"]] if "call" in step else step["calls"]
            for call in calls:
                rows.append(run_step(harness, step, call, saved, label=step["id"] if len(calls) == 1 else f"{step['id']}:{call}"))
    finally:
        harness.close()
    failed = [row for row in rows if row["failures"]]
    return {"scenario_id": scenario["scenario_id"], "covers": scenario.get("covers", []),
            "passed": not failed, "steps": rows,
            "failures": [f"{row['step']}: {problem}" for row in failed for problem in row["failures"]]}


def run_step(harness, step, call, saved, *, label):
    expect = step.get("expect", {})
    method, path = _CALL.match(fill(call, saved)).groups()
    body = fill(step.get("body"), saved) if "body" in step else ({} if method == "POST" else None)
    headers = fill(step.get("headers", {}), saved)
    harness.model.load(step.get("model", []))
    harness.model.problems.clear()
    calls_before = len(harness.model.calls)
    before = snapshot(harness.db_path)
    status, response = harness.request(step.get("as"), method, path, body, headers)
    after = snapshot(harness.db_path)
    failures = list(harness.model.problems)
    if harness.model.queue:
        failures.append(f"{len(harness.model.queue)} scripted model entries not used")
    model_calls = len(harness.model.calls) - calls_before
    projection = project(response)
    if "status" in expect and status != expect["status"]:
        failures.append(f"status: {status} != {expect['status']}")
    if "model_calls" in expect and model_calls != expect["model_calls"]:
        failures.append(f"model_calls: {model_calls} != {expect['model_calls']}")
    requests = harness.model.calls[calls_before:]
    if "model_requests" in expect:
        wanted_requests = fill(expect["model_requests"], saved)
        if len(requests) != len(wanted_requests):
            failures.append(f"model_requests: {len(requests)} calls != {len(wanted_requests)} expected")
        for index, (actual, wanted) in enumerate(zip(requests, wanted_requests)):
            for key, value in wanted.items():
                if not matches(actual.get(key, ABSENT), value):
                    failures.append(f"model_requests[{index}].{key}: {json.dumps(actual.get(key), ensure_ascii=False)}"
                                    f" != {json.dumps(value, ensure_ascii=False)}")
    for key, wanted in fill(expect.get("response", {}), saved).items():
        actual = projection.get(key, ABSENT)
        if not matches(actual, wanted):
            failures.append(f"response.{key}: {json.dumps(actual, ensure_ascii=False)[:200]} != "
                            f"{json.dumps(wanted, ensure_ascii=False)[:200]}")
    changed, problems = check_db(expect.get("db", {}), before, after, saved)
    failures += problems
    for name, source in step.get("save", {}).items():
        value = dotted(response, source)
        if value == ABSENT:
            failures.append(f"save.{name}: {source} is absent")
        else:
            saved[name] = value
    return {"step": label, "call": f"{method} {path}",
            "http_status": status, "model_calls": model_calls, "model_requests": requests, "projection": projection,
            "db_changed_tables": changed, "failures": failures}


def run_all(scenarios):
    results = [run_scenario(scenario) for scenario in scenarios]
    return {"schema_version": REPORT_SCHEMA, "projection_version": PROJECTION_VERSION,
            "scenarios": len(results), "passed": sum(r["passed"] and not r.get("skipped") for r in results),
            "skipped": sum(bool(r.get("skipped")) for r in results),
            "steps": sum(len(r["steps"]) for r in results),
            "notice": "离线黄金场景：脚本化模型替身加真实 HTTP 与 SQLite；只是工程与协议证据，不是任务成功率或语义准确率。",
            "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--only", action="append")
    parser.add_argument("--out", type=Path, help="new JSON report; an existing file is never replaced")
    args = parser.parse_args(argv)
    if args.out is not None and args.out.exists():
        parser.error(f"{args.out} exists; reports are never overwritten")
    report = run_all(load_scenarios(args.scenarios, only=args.only))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
    for result in report["results"]:
        print(("SKIP " if result.get("skipped") else "PASS " if result["passed"] else "FAIL ") + result["scenario_id"])
        for failure in result["failures"]:
            print("  " + failure)
    print(json.dumps({key: report[key] for key in ("scenarios", "passed", "skipped", "steps")}))
    return 0 if report["passed"] + report["skipped"] == report["scenarios"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
