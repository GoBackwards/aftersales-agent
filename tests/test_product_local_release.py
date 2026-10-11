"""Product demo on the local frozen release; skipped where that release is absent.

Kept apart from the portable Graph CI selection because the frozen policy files
are not distributed with the source. The keyword script proposes the plan and
card choice, so these checks cover wiring, not answer quality.
"""
import importlib.util
from pathlib import Path
import unittest
from urllib.parse import quote

from tests.test_graph_product_api import ProductServerCase

HAS_GRAPH = importlib.util.find_spec("pydantic") is not None
STORE = Path(__file__).resolve().parents[1] / "var/knowledge-store"


@unittest.skipUnless(HAS_GRAPH and STORE.exists(), "existing local frozen release is not distributed with source")
class FrozenReleaseAppTests(ProductServerCase):
    """The `app` demo wiring on the local frozen release and repository card catalog.

    The keyword script proposes the plan and card choice; everything after that
    (quote checks, rendering, business state) is the production path.
    """

    def setUp(self):
        super().setUp()
        from support_agent.cli import _release_policy_config
        from support_agent.demo_model import KeywordDemoModel
        self.serve(demo_login=True, public_policy_config=_release_policy_config(STORE),
                   model_factory=KeywordDemoModel, kb_store=self.root / "kb")
        self.alice = self.login("alice")
        self.admin = self.login("kb-admin")

    def turn(self, sid, message, request_id):
        status, result = self.request("POST", f"/sessions/{sid}/messages", {"message": message, "request_id": request_id},
                                      token=self.alice)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "completed", result)
        return result

    def session(self):
        return self.request("POST", "/sessions", {}, token=self.alice)[1]["session_id"]

    def test_policy_answer_cites_verbatim_quotes_from_the_active_version(self):
        result = self.turn(self.session(), "京东自营的赠品需要和主商品一起退回吗？", "gift")
        # B5d2: each cited card is one policy_card block, its selected outcomes open in the reply (D7).
        cards = [block for block in result["presentation"]["blocks"] if block["type"] == "policy_card"]
        self.assertIn("gift_general", [card["card_id"] for card in cards])
        self.assertEqual(result["card_catalog"]["kb_version_id"],
                         self.request("GET", "/kb/overview", token=self.admin)[1]["active"]["version_id"])
        for card in cards:
            for row in card["selected"]:
                self.assertIn(row["outcome"], result["reply"])
            for reference in card["references"]:
                status, source = self.request("GET", "/kb/sources/" + quote(reference["evidence_id"], safe="")
                                              + "?scope=active", token=self.admin)
                self.assertEqual(status, 200)
                for text in reference["quotes"]:
                    self.assertIn(text, source["source"]["text"])

    def test_route_table_and_confirmed_simulated_return(self):
        route = self.turn(self.session(), "我从广西寄回北京，退货运费补偿表列金额是多少？", "route")
        block = next(block for block in route["presentation"]["blocks"] if block["type"] == "route")
        self.assertEqual((block["origin"], block["destination"], block.get("amount_text")), ("广西", "北京", "13"))
        sid = self.session()
        prepared = self.turn(sid, "我要退货 O1001，原因：尺寸不合适", "return")
        business = next(block for block in prepared["presentation"]["blocks"] if block["type"] == "business")
        self.assertEqual(business["status"], "awaiting_confirmation")
        self.assertIsNone(self.request("GET", "/me/orders", token=self.alice)[1]["orders"][0]["return_request"])
        card = prepared["session"]["card"]
        self.assertNotIn("reason", card)
        meta = self.request("GET", "/me/orders", token=self.alice)[1]
        self.assertEqual(meta["return_shipping_rule"]["card_id"], "return_shipping_responsibility")
        self.assertEqual(len(meta["return_shipping_rule"]["branches"]), 3)
        status, confirmed = self.request("POST", f"/sessions/{sid}/cards/{card['action_id']}/confirm",
                                         {"version": card["version"], "reason_code": "size_or_style",
                                          "reason_text": "", "request_id": "confirm"}, token=self.alice)
        self.assertEqual(status, 200, confirmed)
        orders = {order["order_id"]: order for order in self.request("GET", "/me/orders", token=self.alice)[1]["orders"]}
        self.assertEqual(orders["O1001"]["return_request"]["id"], "RET-O1001")
        self.assertNotIn("reason", orders["O1001"]["return_request"])


if __name__ == "__main__":
    unittest.main()
