"""T3 golden offline scenarios (scripts/eval/golden.py, tests/golden/*.json) and checks of the framework.

Each scenario runs the real service over loopback HTTP with a scripted model stand-in and asserts the
response projection and the database rows; one test per scenario file. The framework checks below make
sure a wrong expectation, an unused or unexpected model script and a malformed scenario are reported.
Offline engineering evidence only: not task success, not semantic accuracy.
"""
from copy import deepcopy
import importlib.util
import json
import unittest

from scripts.eval import golden

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
SCENARIOS = {scenario["scenario_id"]: scenario for scenario in golden.load_scenarios()}
# Every endpoint of the current service. B3a replaced the task read and the v2 confirm with the two card
# endpoints; B3b added the void click; B4a added the "找人工" button, the ticket withdrawal and GET /tickets
# (which replaced GET /handoffs), here and in G02/G04 (I15).
SESSION_SCOPED_ENDPOINTS = {"GET /sessions/{}", "GET /sessions/{}/history", "GET /sessions/{}/runs/{}",
                            "POST /sessions/{}/messages", "POST /sessions/{}/cards", "POST /sessions/{}/cards/{}/confirm",
                            "POST /sessions/{}/cards/{}/void", "POST /sessions/{}/handoff",
                            "POST /sessions/{}/tickets/{}/withdraw", "POST /sessions/{}/cancel",
                            "POST /sessions/{}/recover"}
LISTING_ENDPOINTS = {"GET /me/orders", "GET /me/sessions", "GET /tickets"}
OTHER_ENDPOINTS = {"GET /client-info", "POST /sessions", "GET /demo/identities", "POST /demo/login",
                   "GET /kb/cards", "GET /"}


def template(call):
    method, path = call.split(" ", 1)
    parts = ["{}" if part.startswith(("{", "RUN-", "ACT-", "TKT-")) else part for part in path.split("/")]
    return method + " " + "/".join(parts)


def calls(scenario, step_ids=None):
    found = set()
    for step in scenario["steps"]:
        if step_ids is None or step["id"] in step_ids:
            found.update(template(call) for call in ([step["call"]] if "call" in step else step["calls"]))
    return found


class GoldenScenarioFileTests(unittest.TestCase):
    """Structure only; runs in the standard-library environment too."""

    def test_scenarios_exist_and_cover_the_existing_endpoints_for_i15(self):
        self.assertGreaterEqual(len(SCENARIOS), 5)
        cross = SCENARIOS["G02-cross-owner-existing-endpoints"]
        foreign = {step["id"] for step in cross["steps"] if step.get("as") == "alice" and step["id"] not in
                   {"alice-open"}}
        self.assertLessEqual(SESSION_SCOPED_ENDPOINTS | LISTING_ENDPOINTS, calls(cross, foreign))
        origin = SCENARIOS["G04-host-origin-every-endpoint"]
        for step_id in ("wrong-host", "cross-origin"):
            with self.subTest(step=step_id):
                self.assertEqual(calls(origin, {step_id}),
                                 SESSION_SCOPED_ENDPOINTS | LISTING_ENDPOINTS | OTHER_ENDPOINTS)

    def test_every_scenario_declares_what_it_covers_and_is_unchanged_by_validation(self):
        for scenario_id, scenario in SCENARIOS.items():
            with self.subTest(scenario=scenario_id):
                self.assertTrue(scenario["covers"] and scenario["summary"].strip())
                before = deepcopy(scenario)
                golden.validate_scenario(scenario)
                self.assertEqual(scenario, before)

    def test_malformed_scenarios_are_rejected_before_running(self):
        base = deepcopy(SCENARIOS["G07-request-id-conflict"])
        cases = {
            "assertion outside the projection": ("response", {"presentation.blocks": "x"}),
            "unknown table": ("db", {"counts": {"no_such_table": 0}}),
            "unchanged with unchanged_except": ("db", {"unchanged": "all", "unchanged_except": ["sessions"]}),
            "unknown model request field (B5a)": ("model_requests", [{"tools": "plan_turn"}]),
            "model requests not a list (B5a)": ("model_requests", {"tool": "plan_turn"}),
        }
        for label, (field, value) in cases.items():
            with self.subTest(case=label):
                scenario = deepcopy(base)
                scenario["steps"][1]["expect"][field] = value
                with self.assertRaises(golden.ScenarioError):
                    golden.validate_scenario(scenario)
        for label, change in {"bad call": {"call": "FETCH sessions"},
                              "unknown customer": {"as": "carol"},
                              "bad model entry": {"model": [{"plan": {}, "answer": {}}]},
                              "call and calls": {"calls": ["GET /tickets"]}}.items():
            with self.subTest(case=label):
                scenario = deepcopy(base)
                scenario["steps"][1].update(change)
                with self.assertRaises(golden.ScenarioError):
                    golden.validate_scenario(scenario)

    def test_projection_and_placeholders(self):
        response = {"status": "completed", "session": {"card": None, "task": {"status": "collecting"}},
                    "reply": "O1002 & O1001", "trace": [{"type": "business", "result": {"status": "order_found"}}],
                    "presentation": {"blocks": [{"type": "business"}, {"type": "text"}]}, "unrelated": 1}
        projected = golden.project(response)
        # Projection v4 (B3a): card paths replace the task and pending-action paths; v5 (B3c) lists the card blocks.
        self.assertEqual(projected, {"status": "completed", "reply": "O1002 & O1001", "session.card": None,
                                     "@business_status": "order_found", "@trace_types": ["business"],
                                     "@business_error": None, "@block_types": ["business", "text"],
                                     "@card_block_orders": [], "@order_fact_orders": [],
                                     "@block_authors": [["business", None], ["text", None]], "@card_blocks": [],
                                     "@text_blocks": [[None, None, None]], "@dropped": [],
                                     "@reply_order_ids": ["O1001", "O1002"], "@demo_words": 0})
        several = golden.project({
            "session": {"open_cards": [{"order_id": "O1001"}, {"order_id": "O1004"}]},
            "confirmation": {"status": "available", "order_id": "O1004",
                             "cards": [{"order_id": "O1001"}, {"order_id": "O1004"}]},
            "presentation": {"blocks": [{"type": "business"}, {"type": "return_card", "order_id": "O1001"},
                                        {"type": "return_card", "order_id": "O1004"}]},
            "trace": [{"type": "business", "result": {"status": "awaiting_confirmation", "return_outcomes": [
                {"order_id": "O1001", "outcome": "created"}, {"order_id": "O1004", "outcome": "reshown"}]}}]})
        self.assertEqual((several["@open_card_orders"], several["@confirmation_card_orders"],
                          several["@card_block_orders"], several["@return_outcomes"]),
                         (["O1001", "O1004"], ["O1001", "O1004"], ["O1001", "O1004"],
                          [["O1001", "created"], ["O1004", "reshown"]]))
        self.assertEqual(golden.project({"open_cards": []})["@open_card_orders"], [])   # a command's snapshot
        card = {"type": "return_card", "action_id": "ACT-1", "version": 2, "order_id": "O1001",
                "reasons": {"options": [{"code": "changed_mind"}, {"code": "other"}]}}
        self.assertEqual(golden.project({"presentation": {"blocks": [card]}})["@card_block"],
                         {"order_id": "O1001", "version": 2, "reason_codes": ["changed_mind", "other"],
                          "carries_reason": False})
        # v6 (B3e): a "申请退货" click declares its card at the top level with the same block.
        clicked = golden.project({"card_outcome": "reshown", "return_card": card})
        self.assertEqual((clicked["@card_block"]["order_id"], clicked["card_outcome"]), ("O1001", "reshown"))
        self.assertNotIn("@card_block_orders", clicked)
        self.assertEqual(golden.PROJECTION_VERSION, "golden-projection-v14")
        # v12 (B5d2): the author of every block, the cited cards (selected and folded outcomes) and the fallback;
        # v13 (B5e): the return card declares its author too and is listed with it.
        assembled = golden.project({"answer_validation": {"fallback": True}, "presentation": {"blocks": [
            {"type": "policy_card", "author": "code", "card_id": "c1", "selected": [{"outcome_index": 1}],
             "other_outcomes": [{"outcome_index": 0}, {"outcome_index": 2}]},
            {"type": "return_card", "author": "code", "order_id": "O1001"}, {"type": "text", "author": "model"}]}})
        self.assertEqual((assembled["@block_authors"], assembled["@card_blocks"], assembled["@fallback"]),
                         ([["policy_card", "code"], ["return_card", "code"], ["text", "model"]],
                          [["c1", [1], [0, 2]]], True))
        # v11 (B5b): the own orders each order_facts block shows, in order.
        facts = golden.project({"presentation": {"blocks": [
            {"type": "business", "orders": [{"order_id": "O1004"}]},
            {"type": "order_facts", "orders": [{"order_id": "O1003"}, {"order_id": "O1001"}]}]}})
        self.assertEqual(facts["@order_fact_orders"], ["O1003", "O1001"])
        # v9 (B4c): the message limit /client-info hands to the page.
        self.assertEqual(golden.project({"customer_id": "alice", "message_limit": 2000})["message_limit"], 2000)
        # v7 (B4a): GET /tickets and a ticket click's declared card.
        listed = golden.project({"tickets": [
            {"customer_id": "bob", "short_no": "H-0001", "status": "cancelled",
             "events": [{"kind": "opened", "trigger": "technical_failure"}, {"kind": "withdrawn", "trigger": "user_request"}]},
            {"customer_id": "bob", "short_no": "H-0002", "status": "pending"}]})
        self.assertEqual((listed["@ticket_count"], listed["@ticket_customer_ids"], listed["@ticket_statuses"]),
                         (2, ["bob"], [["H-0001", "cancelled"], ["H-0002", "pending"]]))
        # v8 (B4b): the events of GET /tickets with their triggers, and a failed turn's one next step.
        self.assertEqual(listed["@ticket_events"], [["H-0001", "opened", "technical_failure"],
                                                    ["H-0001", "withdrawn", "user_request"]])
        # v10 (B4d): each ticket's summary need as written, and the failure card's own "找人工".
        self.assertEqual(listed["@ticket_needs"], [["H-0001", golden.ABSENT], ["H-0002", golden.ABSENT]])
        noted = golden.project({"tickets": [{"short_no": "H-0003", "summary": {"user_need": {"text": "核对 ****"}}}]})
        self.assertEqual(noted["@ticket_needs"], [["H-0003", "核对 ****"]])
        offered = golden.project({"next_step": {"action": "retry", "consecutive_failures": 1, "offer_handoff": True}})
        self.assertIs(offered["next_step.offer_handoff"], True)
        failed = golden.project({"status": "model_error", "next_step": {"action": "ticket", "consecutive_failures": 2,
                                                                        "ticket_outcome": "opened"},
                                 "ticket_card": {"type": "ticket", "short_no": "H-0001", "status": "pending",
                                                 "actions": ["withdraw"]}})
        self.assertEqual((failed["next_step.action"], failed["next_step.consecutive_failures"],
                          failed["next_step.ticket_outcome"], failed["@ticket_card"]["short_no"]),
                         ("ticket", 2, "opened", "H-0001"))
        self.assertNotIn("handoff.reason_code", golden.PROJECTION_KEYS)
        clicked = golden.project({"ticket_outcome": "opened", "ticket": {"short_no": "H-0001", "status": "pending"},
                                  "last_reply": "工单句", "ticket_card": {"type": "ticket", "short_no": "H-0001",
                                                                        "status": "pending", "actions": ["withdraw"]}})
        self.assertEqual((clicked["ticket_outcome"], clicked["ticket.short_no"], clicked["@ticket_card"]),
                         ("opened", "H-0001", {"short_no": "H-0001", "status": "pending", "actions": ["withdraw"]}))
        self.assertNotIn("@handoff_count", golden.PROJECTION_KEYS)
        self.assertEqual(golden.project({"reply": "模拟", "notice": "演示", "error": {"message": "教学"},
                                         "presentation": {"blocks": [{"note": "场景日"}]},
                                         "orders": [{"eligibility_text": "模拟"}]})["@demo_words"], 4)
        saved = {"version": 3, "sid": "SES-1"}
        self.assertEqual(golden.fill({"v": "{version}", "p": "/sessions/{sid}/x", "n": ["{sid}"]}, saved),
                         {"v": 3, "p": "/sessions/SES-1/x", "n": ["SES-1"]})
        with self.assertRaises(golden.ScenarioError):
            golden.fill("{missing}", saved)
        # B4c: a body value of an exact length, written once in the scenario file.
        self.assertEqual(golden.fill({"message": {"$repeat": ["长", 3]}, "request_id": "{sid}"}, saved),
                         {"message": "长长长", "request_id": "SES-1"})
        for malformed in (["长"], ["", 3], ["长", -1], ["长", True], "长", ["长", 100_001]):
            with self.subTest(repeat=malformed), self.assertRaises(golden.ScenarioError):
                golden.fill({"$repeat": malformed}, saved)
        self.assertTrue(golden.matches(golden.ABSENT, {"$absent": True}))
        self.assertFalse(golden.matches(None, {"$absent": True}))
        self.assertTrue(golden.matches(["b", "a"], {"$set": ["a", "b"]}))
        self.assertTrue(golden.matches("abc", {"$contains": "b"}))
        self.assertTrue(golden.matches([1, 2], {"$len": 2}))
        self.assertTrue(golden.matches(1, {"$ne": 2}))
        self.assertTrue(golden.matches("abc", {"$lacks": "d"}))
        self.assertFalse(golden.matches("abc", {"$lacks": "b"}))
        # B5f: a list lacks an item too (["b"] lacks "d"; a list holding it does not).
        self.assertTrue(golden.matches(["b"], {"$lacks": "d"}))
        self.assertFalse(golden.matches(["b", "d"], {"$lacks": "d"}))
        self.assertFalse(golden.matches(3, {"$lacks": "d"}))
        self.assertTrue(golden.matches("a-b-c", {"$ordered": ["a", "b", "c"]}))
        self.assertFalse(golden.matches("a-c-b", {"$ordered": ["a", "b", "c"]}))
        self.assertFalse(golden.matches("a-b", {"$ordered": ["a", "z"]}))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class GoldenScenarioTests(unittest.TestCase):
    """One test per scenario file under tests/golden."""


def _scenario_test(scenario_id):
    def test(self):
        result = golden.run_scenario(SCENARIOS[scenario_id])
        if result.get("skipped"):   # B6: a scenario on the local frozen release, where that release is absent
            self.skipTest(result["skipped"])
        self.assertTrue(result["passed"], "\n".join(result["failures"]))
        self.assertGreater(len(result["steps"]), 1)
    return test


for _identifier in SCENARIOS:
    setattr(GoldenScenarioTests, "test_" + _identifier.replace("-", "_").lower(), _scenario_test(_identifier))


@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class GoldenFrameworkTests(unittest.TestCase):
    """The framework reports what is wrong instead of passing silently."""

    def mutate(self, scenario_id, step_id, change):
        scenario = deepcopy(SCENARIOS[scenario_id])
        step = next(step for step in scenario["steps"] if step["id"] == step_id)
        change(step)
        return golden.run_scenario(scenario)

    def failures_of(self, result, step_id):
        return [failure for failure in result["failures"] if failure.startswith(step_id + ":")]

    def test_wrong_status_projection_and_rows_are_each_reported(self):
        def edit(step):
            step["expect"]["status"] = 201
            step["expect"]["response"]["confirmation.reason"] = "别的原因"
            step["expect"]["db"]["counts"]["return_requests"] = 1
        result = self.mutate("G01-card-click-only", "ask", edit)
        self.assertFalse(result["passed"])
        problems = self.failures_of(result, "ask")
        self.assertTrue(any("status: 200 != 201" in p for p in problems), problems)
        self.assertTrue(any("response.confirmation.reason" in p for p in problems), problems)
        self.assertTrue(any("db.counts.return_requests: 0 != 1" in p for p in problems), problems)

        def rows(step):
            step["expect"]["db"]["rows"]["return_requests"][0]["reason"] = "别的原因"
        problems = self.failures_of(self.mutate("G01-card-click-only", "click", rows), "click")
        self.assertTrue(any(p.startswith("click: db.rows.return_requests") for p in problems), problems)

    def test_unexpected_unused_and_mismatched_model_scripts_fail_the_step(self):
        unused = self.mutate("G07-request-id-conflict", "same-id-same-text",
                             lambda step: step.update(model=[{"answer": {"prose": "不会被调用"}}]))
        self.assertIn("same-id-same-text: 1 scripted model entries not used", unused["failures"])
        missing = self.mutate("G07-request-id-conflict", "first", lambda step: step["model"].pop())
        self.assertTrue(any("unexpected model call for respond" in p
                            for p in self.failures_of(missing, "first")), missing["failures"])
        swapped = self.mutate("G07-request-id-conflict", "first", lambda step: step["model"].reverse())
        self.assertTrue(any("model asked for plan_turn, script has respond" in p
                            for p in self.failures_of(swapped, "first")), swapped["failures"])

    def test_wrong_model_request_expectations_are_reported(self):
        step_id = "a-strict-answer-carries-only-the-offered-fields"

        def wrong(step):
            step["expect"]["model_requests"][1].update(strict=False, offered=["prose"])
        problems = self.failures_of(self.mutate("G19-answer-strict-mode", step_id, wrong), step_id)
        self.assertTrue(any(p.startswith(f"{step_id}: model_requests[1].strict") for p in problems), problems)
        self.assertTrue(any(p.startswith(f"{step_id}: model_requests[1].offered") for p in problems), problems)
        short = self.mutate("G19-answer-strict-mode", step_id, lambda step: step["expect"]["model_requests"].pop())
        self.assertIn(f"{step_id}: model_requests: 2 calls != 1 expected", short["failures"])
        # B5b: the order ids an answer may show, and the order ids a request's context carries.
        step_id = "only-own-orders-are-offered-and-shown"

        def foreign(step):
            step["expect"]["model_requests"][1].update(order_choices=["O1001", "O2001"], context_order_ids=["O1001"])
            step["expect"]["model_requests"][0]["order_choices"] = ["O2001"]
        problems = self.failures_of(self.mutate("G20-own-order-facts-in-answer", step_id, foreign), step_id)
        for wrong in ("model_requests[1].order_choices", "model_requests[1].context_order_ids",
                      "model_requests[0].order_choices"):
            self.assertTrue(any(p.startswith(f"{step_id}: {wrong}") for p in problems), (wrong, problems))
        # B5c: the field names of the registered questions a request's context carries; B5d2: the turn items' kinds
        # and the context's own keys.
        step_id = "the-planner-mark-never-reaches-the-answer"

        def planner_field(step):
            step["expect"]["model_requests"][1].update(question_fields=["id", "quote", "required_clarification_id"],
                                                       item_kinds=["message"], context_keys=["turn_items"])
            step["expect"]["model_requests"][0]["question_fields"] = ["id", "quote"]
        problems = self.failures_of(self.mutate("G21-clarification-decided-by-answer", step_id, planner_field), step_id)
        for wrong in ("model_requests[1].question_fields", "model_requests[1].item_kinds",
                      "model_requests[1].context_keys", "model_requests[0].question_fields"):
            self.assertTrue(any(p.startswith(f"{step_id}: {wrong}") for p in problems), (wrong, problems))

    def test_unchanged_database_claims_are_checked(self):
        def claim_unchanged(step):
            step["expect"]["db"] = {"unchanged": "all"}
        result = self.mutate("G01-card-click-only", "click", claim_unchanged)
        problems = self.failures_of(result, "click")
        for table in ("return_requests", "return_cards", "session_messages", "session_requests"):
            self.assertTrue(any(f"db.{table}: expected unchanged" in p for p in problems), (table, problems))

    def test_reports_carry_the_projection_and_changed_tables(self):
        report = golden.run_all([SCENARIOS["G05-same-order-two-sessions"]])
        self.assertEqual((report["scenarios"], report["passed"]), (1, 1))
        self.assertEqual(report["projection_version"], golden.PROJECTION_VERSION)
        click = next(row for row in report["results"][0]["steps"] if row["step"] == "click-second")
        self.assertEqual(click["projection"]["return_request.order_id"], "O1001")
        self.assertIn("return_cards", click["db_changed_tables"])
        self.assertNotIn("return_requests", click["db_changed_tables"])
        json.dumps(report, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
