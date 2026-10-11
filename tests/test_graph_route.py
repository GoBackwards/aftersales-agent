"""Graph route evidence and user correction checks; all policy data is invented."""
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from support_agent.policy import PolicyAdapter, PublicPolicyConfig
from support_agent.shipping_route import normalize_node, resolve_user_route
from tests.test_public_policy_bundles import bundle_fixture


def user(content):
    return {"role": "user", "content": content}


class GraphRouteParsingTests(unittest.TestCase):
    def test_second_turn_replaces_only_named_destination_and_keeps_user_origin(self):
        first = "我从广东自寄到北京，请按逆向运费补偿表说明参考标准。"
        second = "收件地更正为湖南；沿用刚才的寄件地，按逆向运费补偿表说明新路线的参考标准。"
        self.assertEqual(resolve_user_route(first, []), {
            "status": "resolved", "origin": "广东", "destination": "北京", "missing": [], "amount_requested": True})
        self.assertEqual(resolve_user_route(second, [user(first)]), {
            "status": "resolved", "origin": "广东", "destination": "湖南", "missing": [], "amount_requested": True})

    def test_reverse_and_full_province_names_preserve_direction(self):
        self.assertEqual(resolve_user_route("我从北京寄回到广东，按逆向运费表查金额。", [])["origin"], "北京")
        full = resolve_user_route("从内蒙古自治区寄回到宁夏回族自治区，查补偿表金额。", [])
        self.assertEqual((full["status"], full["origin"], full["destination"]),
                         ("resolved", "内蒙古", "宁夏"))

    def test_missing_conflicting_and_city_endpoints_never_resolve(self):
        first = "我从广西自寄，但还不知道售后退回收件地。要按逆向运费补偿表核对，需要补什么信息？"
        partial = resolve_user_route(first, [])
        self.assertEqual((partial["status"], partial["origin"], partial["missing"]),
                         ("needs_clarification", "广西", ["destination"]))
        second = "售后给出的退回收件地是湖南，仍从广西寄出。现在应按哪个发货地和收件地核对？"
        updated = resolve_user_route(second, [user(first)])
        self.assertEqual((updated["status"], updated["origin"], updated["destination"], updated["amount_requested"]),
                         ("resolved", "广西", "湖南", False))
        conflict = resolve_user_route("广东→北京或广东→湖南，按逆向运费表查金额。", [])
        self.assertNotEqual(conflict["status"], "resolved")
        city = resolve_user_route("从广州市寄到湖南省，按逆向运费表查金额。", [user(first)])
        self.assertNotEqual(city["status"], "resolved")

    def test_negated_old_route_and_unpunctuated_correction_cannot_reuse_old_value(self):
        previous = user("我从广东自寄到北京，请按逆向运费补偿表说明参考标准。")
        corrected = resolve_user_route("别按广东到北京改为广东到湖南，查逆向运费表金额。", [previous])
        self.assertNotEqual(corrected["status"], "resolved")
        self.assertIsNone(corrected["destination"])
        denied = resolve_user_route("不是从广东寄回北京，按逆向运费表查金额。", [previous])
        self.assertNotEqual(denied["status"], "resolved")

    def test_forward_delivery_scope_conflicts_and_stale_corrections_fail_closed(self):
        previous = user("我从广东自寄到北京，请按逆向运费补偿表说明参考标准。")
        cases = (
            "商家从广东寄到北京，退货逆向运费补偿表金额是多少？",
            "商家发货广东→北京，逆向运费补偿表多少钱？",
            "第三方店订单，我从广东寄回北京，按逆向运费表查金额。",
            "香港收货地址，我从广东寄回北京，按逆向运费表查金额。",
            "这次不是京东原因退货，我从广东寄回北京，按逆向运费表查金额。",
            "寄件地由广东改为广西，收件地仍北京，按逆向运费表查金额。",
            "目的地由北京改为湖南，寄件地仍广东，按逆向运费表查金额。",
            "收件地不是北京，是湖南，按逆向运费表查金额。",
            "广东寄回北京不是这次退货路线，按逆向运费表查金额。",
            "广东寄回北京，不是这条路线，按逆向运费表查金额。",
        )
        for current in cases:
            with self.subTest(current=current):
                self.assertNotEqual(resolve_user_route(current, [previous])["status"], "resolved")

    def test_distinct_routes_in_one_turn_require_an_explicit_correction(self):
        for current in ("广东→北京，广东→湖南，按逆向运费表查金额。",
                        "从广东寄回北京，或者从广东寄回湖南，按逆向运费表查金额。"):
            with self.subTest(current=current):
                self.assertNotEqual(resolve_user_route(current, [])["status"], "resolved")

    def test_explicit_no_amount_request_wins_over_amount_keyword(self):
        result = resolve_user_route("按逆向运费补偿表核对适用范围，不要给金额；我从广东寄回北京。", [])
        self.assertEqual(result["status"], "resolved")
        self.assertFalse(result["amount_requested"])

    def test_assistant_and_external_history_cannot_supply_missing_origin(self):
        current = "收件地是湖南，按逆向运费表核对参考标准。"
        history = [{"role": "assistant", "content": "原来从广东寄到北京"},
                   {"role": "user", "source_kind": "historical_external",
                    "content": "我从广东寄回北京，按补偿表核对。"}]
        result = resolve_user_route(current, history)
        self.assertEqual((result["status"], result["origin"], result["missing"]),
                         ("needs_clarification", None, ["origin"]))

    def test_explicit_new_order_or_topic_drops_prior_route(self):
        previous = user("我从广东寄回北京，按逆向运费表查金额。")
        for current in ("换一单，逆向运费补偿表参考标准是多少？",
                        "换个问题：按逆向运费补偿表核对，要提供什么？"):
            with self.subTest(current=current):
                result = resolve_user_route(current, [previous])
                self.assertEqual((result["status"], result["origin"], result["destination"]),
                                 ("needs_clarification", None, None))

    def test_new_order_or_explicit_jd_correction_clears_prior_merchant_conflict(self):
        previous = user("这是第三方店订单，我从广东寄回北京，按逆向运费表查金额。")
        new_order = resolve_user_route("换一单，这是京东自营，我从广东寄回湖南，按逆向运费表查金额。",
                                       [previous])
        self.assertEqual((new_order["status"], new_order["origin"], new_order["destination"]),
                         ("resolved", "广东", "湖南"))
        correction = resolve_user_route("这不是第三方店，是京东自营，我从广东寄回湖南，按逆向运费表查金额。", [])
        self.assertEqual((correction["status"], correction["origin"], correction["destination"]),
                         ("resolved", "广东", "湖南"))
        conflict_after = resolve_user_route("这单是京东自营，但商家说它是第三方店；我从广东寄回湖南，按逆向运费表查金额。", [])
        self.assertEqual(conflict_after["status"], "out_of_scope")
        hong_kong = resolve_user_route("收货地址香港，但这是京东自营，我从广东寄回北京，按逆向运费表查金额。", [])
        self.assertEqual(hong_kong["status"], "out_of_scope")
        mainland_correction = resolve_user_route("收货地址原先香港，现收货地址改为湖南省；我从广东寄回北京，按逆向运费表查金额。", [])
        self.assertEqual(mainland_correction["status"], "resolved")

    def test_province_suffixes_normalise_and_other_names_pass_through_unchanged(self):
        self.assertEqual([normalize_node(name) for name in ("广东省", "北京市", "宁夏回族自治区", "广西壮族自治区")],
                         ["广东", "北京", "宁夏", "广西"])
        self.assertEqual([normalize_node(name) for name in ("广州市", "火星", "")], ["广州市", "火星", ""])


class GraphRoutePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        paths = bundle_fixture(root, route_rows=[
            ["广东", "北京", "12", "北京", "广东", "10", "广东", "湖南", "10"],
            ["湖南", "广东", "11"],
        ])
        self.config = PublicPolicyConfig(*paths)

    def adapter(self):
        return PolicyAdapter(self.config)

    def test_route_value_has_bound_original_row_and_the_releases_verified_conditions(self):
        # B6: the lookup reads the release itself; no search result has to carry the compensation rules first.
        adapter = self.adapter()
        forward = adapter.route_reference("广东", "北京")
        reverse = adapter.route_reference("北京", "广东")
        changed = adapter.route_reference("广东", "湖南")
        self.assertEqual([row["source_amount_text"] for row in (forward, reverse, changed)], ["12", "10", "10"])
        self.assertEqual(forward["status"], "evidence_found")
        self.assertIn("广东 | 北京 | 12", forward["route_source_card"]["content"])
        self.assertEqual(forward["amount_semantics"], "source_table_value_not_order_payout")
        self.assertFalse(forward["eligibility_evaluated"])
        self.assertEqual([card["citation_id"] for card in forward["condition_source_cards"]],
                         ["rule:responsibility", "rule:conditions"])
        self.assertIn("其他约定除外", "\n".join(card["content"] for card in forward["condition_source_cards"]))
        for retired in ("group_count_with_route", "context_chars_with_route", "detailed_premises_verified"):
            self.assertNotIn(retired, forward)

    def test_an_unlisted_route_never_emits_an_amount(self):
        missing = self.adapter().route_reference("湖南", "北京")
        self.assertEqual(missing["status"], "route_not_listed")
        self.assertIsNone(missing["source_amount_text"])
        self.assertNotIn("condition_source_cards", missing)

    def test_the_route_source_is_detached_from_the_shared_snapshot(self):
        adapter = self.adapter()
        first = adapter.route_reference("广东", "北京")
        first["route_source_card"]["spans"][0]["text"] = "changed"
        first["condition_source_cards"][0]["content"] = "changed"
        again = adapter.route_reference("广东", "北京")
        self.assertNotEqual(again["route_source_card"]["spans"][0]["text"], "changed")
        self.assertNotEqual(again["condition_source_cards"][0]["content"], "changed")


@unittest.skipUnless(importlib.util.find_spec("pydantic") is not None, "optional graph runtime not installed")
class GraphRouteHTTPTests(unittest.TestCase):
    def setUp(self):
        from support_agent.service import create_server
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = PublicPolicyConfig(*bundle_fixture(root, route_rows=[
            ["广东", "北京", "12", "北京", "广东", "10", "广东", "湖南", "10"],
        ]))
        self.server = create_server(db_path=root / "graph.sqlite3", credentials={"local-token": "alice"},
            port=0, public_policy_config=self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        try:
            connection.request(method, path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                               {"Authorization": "Bearer local-token", "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_two_turn_correction_renders_only_verified_current_amount_and_source(self):
        status, started = self.request("POST", "/sessions", {})
        self.assertEqual(status, 201)
        session = started["session_id"]
        first = "我从广东自寄到北京，请按逆向运费补偿表说明参考标准。"
        second = "收件地更正为湖南；沿用刚才的寄件地，按逆向运费补偿表说明新路线的参考标准。"
        status, before = self.request("POST", f"/sessions/{session}/messages",
                                      {"message": first, "request_id": "route-first"})
        self.assertEqual((status, before["status"]), (200, "completed"))
        # B5d2: the route block is code's (D10); a route turn shows no prose (K23).
        self.assertIn("从广东寄到北京", before["reply"])
        self.assertIn("12 元", before["reply"])
        route = next(block for block in before["presentation"]["blocks"] if block["type"] == "route")
        self.assertEqual((route["author"], route["origin"], route["destination"], route["amount_text"]),
                         ("code", "广东", "北京", "12"))
        self.assertFalse(any(block.get("author") == "model" and block["type"] == "text"
                             for block in before["presentation"]["blocks"]))
        status, after = self.request("POST", f"/sessions/{session}/messages",
                                     {"message": second, "request_id": "route-second"})
        self.assertEqual((status, after["status"]), (200, "completed"))
        self.assertIn("从广东寄到湖南", after["reply"])
        self.assertIn("10 元", after["reply"])
        self.assertNotIn("12 元", after["reply"])
        self.assertEqual(after["policy_status"], "answered")
        cards = after["presentation"]["source_cards"]
        self.assertTrue(any("广东 | 湖南 | 10" in card["content"] for card in cards))
        self.assertFalse(any("广东 | 北京 | 12" in card["content"] for card in cards))
        lookup = next(row["result"] for row in after["trace"] if row["type"] == "directed_route_lookup")
        self.assertEqual((lookup["status"], lookup["source_amount_text"]), ("evidence_found", "10"))
        self.assertTrue(any(card["citation_id"].startswith("rule:") for card in cards))

    def test_missing_destination_never_renders_a_number(self):
        _, started = self.request("POST", "/sessions", {})
        status, result = self.request("POST", f"/sessions/{started['session_id']}/messages",
            {"message": "我从广东自寄，但还不知道收件地。请按逆向运费补偿表核对参考标准。",
             "request_id": "missing-route"})
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertIn("实际收件的省份", result["reply"])
        self.assertNotIn("12 元", result["reply"])
        self.assertNotIn("10 元", result["reply"])
        self.assertFalse(any(card["citation_id"].startswith("route:")
                             for card in result["presentation"]["source_cards"]))


if __name__ == "__main__":
    unittest.main()
