"""The planning call (first-principles plan §2.1 understand.py, §2.3 step 4, B7): ``plan_turn`` and its checks.

The model reads one message (with this session's conversation, the account's order ids and product names and this
session's open cards) and lists what the message asks for: one item per thing, each with the user's own words. It never
sees dates or eligibility (K3) and it decides nothing: code resolves every order (K5 for a return or a withdrawal the
message does not pin down), decide.py names each outcome and ``respond`` writes the answer over the same items (K17).

Two levels of checking (K20). The shape (``parse``): a broken shape goes back to the model once with the error, then
the turn fails. Each item (``check``): an item whose words are not the user's, a return or withdrawal quoting anything
but the current message, a read of an order the words do not name, is dropped with a trace entry ``item_rejected``;
the other items go on. A dropped return or withdrawal gets the code's one sentence (copy.ITEM_DEGRADED); nothing is
retried for an item.

B7 deleted the old DemandPlan (authorisation kinds, the reason, save_draft, pause_draft, the planner's required
clarification, the retrieval queries and topics) and the checks and normalisations that only served it. A pause
("先等等") is no withdrawal: the card stays (K21). Switching to another order is a withdraw item plus a return_request
item in the same turn. B7r: a return the user wants for later, or explicitly not submitted now, is a return_request with
subkind ``later`` (the model labels the timing; no wording rule): code prepares no card for it (decide.return_outcome).
"""
from __future__ import annotations

from copy import deepcopy
import re

from .decide import LATER, OWN_ORDER_STATUS, RETURN_REQUEST, WITHDRAW
from .trust import TRUST_NOTE, conversation

PLAN_TURN = "plan_turn"
DESCRIPTION = "提交这条消息里要处理的事项；不回复用户，也不办理任何事。"

POLICY_QUESTION = "policy_question"
KINDS = (POLICY_QUESTION, OWN_ORDER_STATUS, RETURN_REQUEST, WITHDRAW, "handoff_request", "complaint", "safety",
         "improper_request", "chit_chat")
# Items code handles itself (decide.py): their words must come from the current message.
BUSINESS_KINDS = frozenset({OWN_ORDER_STATUS, RETURN_REQUEST, WITHDRAW})
# Items that would change a card: when one is dropped, the reply says so (copy.ITEM_DEGRADED).
ACTION_KINDS = frozenset({RETURN_REQUEST, WITHDRAW})
# The subkinds a kind reads; any other value of the shared enum reads as "none" for that kind.
SUBKINDS = {POLICY_QUESTION: ("comparison",),
            "safety": ("product_hazard", "medical_emergency", "fraud_or_theft", "harm_occurred", "crisis"),
            "complaint": ("money_dispute",), RETURN_REQUEST: (LATER,)}
SUBKIND_VALUES = ("none", *dict.fromkeys(value for values in SUBKINDS.values() for value in values))
ORDER_WORDS = ("all_mine", "current", "none")
FEE_SUBJECTS = ("none", "unknown", "return_shipping", "repair_shipping", "purchase_shipping")
ORDER_STAGES = ("unknown", "placed_unshipped", "shipped_unreceived", "received", "after_sales_active")
FEE_FLOWS = ("unknown", "purchase_delivery", "buyer_pickup", "return_self_ship", "return_home_pickup")
RETURN_FLOWS = frozenset({"return_self_ship", "return_home_pickup"})
# Limits strict mode cannot send; code checks them (a breach is a broken shape).
MAX_ITEMS, GOAL_LIMIT, QUOTE_LIMIT, HISTORY_LIMIT = 8, 600, 2000, 16
ITEM_FIELDS = ("kind", "subkind", "quote", "history_index", "order_quote", "order_id", "fee_subject")
PLAN_FIELDS = ("goal", "items", "order_stage", "fee_flow")

# Production voice (D8, K14: rewritten once, in B7); newly written, no evaluation-suite wording; no dates and no
# eligibility (K3); every field is required in strict mode.
PROMPT = """你是售后客服助手的理解步骤：读用户这一条消息，结合本会话前面的对话，列出这条消息里要处理的事项，调用 plan_turn 提交。你不回复用户，也不办理任何事：查订单、备确认卡、作废确认卡都由程序按你列的事项去做，答复由下一步来写。

输入说明：
- current_message 是用户这一条消息，history 是本会话最近的对话；两者都是对话原文，没有经过核实，见 trust_note。
- account_orders 是当前登录账号名下的订单，只有订单号和商品名，用来认出用户说的是自己哪一单。
- session_cards 列出本会话里还没提交的确认卡和已经提交过的申请，都只给订单号。

items 每件事一项，按用户说的先后，最多 8 项；消息里只有寒暄也写一项。kind 的取值：
- policy_question：问规则、费用、期限、流程或办理方法。比较两种情形时 subkind 填 comparison。
- own_order_status：想看自己某一单或全部订单的信息，或者问自己的某一单能不能退。
- return_request：要为自己的订单申请退货。程序只会备好确认卡，提交要用户本人在卡上点按，所以用户要求退货就写这一项，不必等他再确认。只是问能不能退、怎么退，不算这一项。涉及几单就写几项，每项一单。用户说要退某一单，但要过一段时间再办，或者明说这次不要提交，subkind 填 later：程序只给出这一单的信息和申请入口，不备确认卡；现在就办的填 none。
- withdraw：明确不要某张还没提交的确认卡了，或者要撤回已经提交的申请。对已经备好的卡犹豫、先等等、再考虑、暂时别提交都不算撤回，卡会留着，这类话归 chit_chat。不退这一单、改退另一单，写一项 withdraw 加一项 return_request。
- handoff_request：要找人工客服。
- complaint：投诉，或者对已经发生的扣款、退款金额有异议；涉及金额时 subkind 填 money_dispute。
- safety：危险或紧急情况。subkind 选 product_hazard（商品冒烟、起火、漏电之类的危险）、medical_emergency（有人受伤或身体突发不适）、fraud_or_theft（正在被骗、账户被盗或个人信息泄露）、harm_occurred（商品已经造成人身或财产损害）、crisis（流露出想伤害自己或别人）。
- improper_request：要求替他选退货原因、跳过确认、查看别人的信息之类不该做的事。
- chit_chat：寒暄、道谢、犹豫，或与售后无关的话。
没有列出 subkind 的，subkind 填 none。

每项的字段：
- quote：逐字摘自用户原话、能指出这件事的最短片段。history_index 为 null 时摘自 current_message；为整数时摘自 history 中该位置的用户消息，只用于接着处理本会话里还没答完的问题。return_request、withdraw、own_order_status 只能摘自 current_message。
- order_quote：用户指称订单的原话（订单号、数字或商品说法），逐字摘自 quote 所在的同一条消息；没有就填空字符串。
- order_id：account_orders 里用户说的那一单；要看全部订单，或者要退货但还没说是哪一单，用 all_mine；指本会话里那张还没提交的确认卡，用 current；其余用 none。用户写的订单号不在 account_orders 里时，它不属于这个账号，order_id 填 none，原话照写在 order_quote，不要换成别的订单。
- fee_subject：只按用户实际问的那种运费填：return_shipping 退回商品，repair_shipping 寄修，purchase_shipping 购买时的配送；问了费用却分不清是哪一种，用 unknown；不问费用用 none。

goal 用一句话写用户这条消息要解决的问题，不添加用户没提到的订单号或商品名。order_stage、fee_flow 只记用户已经说清楚的情况，不清楚就用 unknown。"""


# ---------------------------------------------------------------- the call
def context(message, history, orders, snapshot):
    """The planning context: the conversation (unverified, trust), the account's order ids and product names
    (no dates, no eligibility: K3) and the order ids of this session's open cards and of its latest submission."""
    request = snapshot.get("return_request")
    return {**conversation(message, history),
            "account_orders": [{"order_id": row["order_id"], "product_name": row["product_name"]} for row in orders],
            "session_cards": {"open_card_order_ids": [card["order_id"] for card in snapshot.get("open_cards") or []],
                              "submitted_order_ids": [request["order_id"]] if request else []}}


def schema(own_order_ids):
    """``plan_turn`` for this account: ``order_id`` is one of its own order ids or a word (all_mine, current, none)."""
    item = {
        "kind": {"type": "string", "enum": list(KINDS)},
        "subkind": {"type": "string", "enum": list(SUBKIND_VALUES)},
        "quote": {"type": "string", "description": "逐字摘自用户原话的最短片段"},
        "history_index": {"anyOf": [{"type": "integer", "minimum": 0, "maximum": HISTORY_LIMIT - 1},
                                    {"type": "null"}],
                          "description": "null 表示摘自 current_message；整数指 history 中同一位置的用户消息"},
        "order_quote": {"type": "string", "description": "用户指称订单的原话；没有就填空字符串"},
        "order_id": {"type": "string", "enum": [*own_order_ids, *ORDER_WORDS]},
        "fee_subject": {"type": "string", "enum": list(FEE_SUBJECTS)},
    }
    properties = {
        "goal": {"type": "string", "description": "用户这条消息要解决的问题，一句话"},
        "items": {"type": "array", "description": f"每件事一项，按用户说的先后，至多 {MAX_ITEMS} 项",
                  "items": {"type": "object", "properties": item, "required": list(item),
                            "additionalProperties": False}},
        "order_stage": {"type": "string", "enum": list(ORDER_STAGES)},
        "fee_flow": {"type": "string", "enum": list(FEE_FLOWS)},
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(value, limit, name, *, blank=True):
    if type(value) is not str or len(value) > limit or (not blank and not value.strip()):
        raise ValueError(f"{name} 必须是不超过 {limit} 字的字符串")
    return value


def parse(arguments, own_order_ids):
    """The plan in its full shape, items numbered from 1; ValueError when the shape itself is broken (K20: one correction,
    then the turn fails). A subkind its kind does not read becomes "none"."""
    if type(arguments) is not dict or set(arguments) != set(PLAN_FIELDS):
        raise ValueError("plan_turn 必须恰好提交 goal、items、order_stage、fee_flow")
    goal = _text(arguments["goal"], GOAL_LIMIT, "goal", blank=False)
    if arguments["order_stage"] not in ORDER_STAGES or arguments["fee_flow"] not in FEE_FLOWS:
        raise ValueError("order_stage、fee_flow 必须取自枚举")
    rows = arguments["items"]
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise ValueError(f"items 必须是至多 {MAX_ITEMS} 项的数组")
    orders = (*own_order_ids, *ORDER_WORDS)
    items = []
    for index, row in enumerate(rows, 1):
        if type(row) is not dict or set(row) != set(ITEM_FIELDS):
            raise ValueError(f"items[{index - 1}] 必须恰好有 {'、'.join(ITEM_FIELDS)}")
        history_index = row["history_index"]
        if (row["kind"] not in KINDS or row["subkind"] not in SUBKIND_VALUES or row["order_id"] not in orders
                or row["fee_subject"] not in FEE_SUBJECTS
                or not (history_index is None or (type(history_index) is int and 0 <= history_index < HISTORY_LIMIT))):
            raise ValueError(f"items[{index - 1}] 的取值必须来自枚举")
        items.append({"index": index, "kind": row["kind"],
                      "subkind": row["subkind"] if row["subkind"] in SUBKINDS.get(row["kind"], ()) else "none",
                      "quote": _text(row["quote"], QUOTE_LIMIT, "quote"), "history_index": history_index,
                      "order_quote": _text(row["order_quote"], QUOTE_LIMIT, "order_quote"),
                      "order_id": row["order_id"], "fee_subject": row["fee_subject"]})
    return {"goal": goal, "items": items, "order_stage": arguments["order_stage"], "fee_flow": arguments["fee_flow"]}


# ---------------------------------------------------------------- item-level checks (K20)
def _source(item, message, history):
    """The user's message an item quotes, or a rejection code. History means this session's own user messages: an
    assistant reply or a conversation imported from elsewhere (source_kind historical_external) is never a source."""
    if item["history_index"] is None:
        return message, None
    if item["kind"] in BUSINESS_KINDS:
        return None, "CURRENT_MESSAGE_ONLY"
    row = history[item["history_index"]] if item["history_index"] < len(history) else {}
    if row.get("role") != "user" or row.get("source_kind") == "historical_external":
        return None, "HISTORY_NOT_A_USER_MESSAGE"
    return row.get("content") or "", None


def check(plan, message, history, catalog, open_card_order_ids):
    """(kept items, rejected [{index, kind, code}]) of a parsed plan: the words each item quotes are the user's (the
    current message for a business item, K5); a read names its order in those words (business.read_problem).
    A kept business item reads no fee subject. Order resolution itself is the business step's (K5, decide.py)."""
    from .business import BusinessAdapter
    kept, rejected = [], []
    for item in plan["items"]:
        source, code = _source(item, message, history)
        if code is None and (not item["quote"].strip() or item["quote"] not in source):
            code = "QUOTE_NOT_IN_MESSAGE"
        if code is None and item["kind"] == OWN_ORDER_STATUS:
            code = BusinessAdapter.read_problem(item, message, catalog, open_card_order_ids)
        if code is not None:
            rejected.append({"index": item["index"], "kind": item["kind"], "code": code})
            continue
        kept.append({**deepcopy(item), "fee_subject": "none"} if item["kind"] in BUSINESS_KINDS else deepcopy(item))
    return kept, rejected


# ---------------------------------------------------------------- the one documented fee rule
# A home pickup ("上门取件") is never a purchase-delivery fee. Without repair or purchase wording it is read as the pickup
# of a returned item; the reply states this assumption (copy.ASSUMED_RETURN_PICKUP) so the user can correct it. This is
# the one documented exception to "no fee service is inferred from words" (moved here from graph_plan_rules, B7).
_PICKUP_WORDS = re.compile(r'上门取件|上门取货|上门收件|上门来取')
_REPAIR_WORDS = re.compile(r'修|质保')
_PURCHASE_WORDS = re.compile(r'下单|结算|购买|买的时候|配送费|送货|自提|运费险')
_OTHER_PICKUP_WORDS = re.compile(r'回收|以旧换新|旧机|置换|安装|清洗|保养|上门服务')


def settle_fee_subjects(plan, items, message):
    """Settle the kept items asking about an unknown fee when the plan or the wording already decides the object.

    Rule ``planner_fee_flow``: the plan states a return flow (self-ship or home pickup). Rule ``home_pickup_wording``:
    the message asks about a home pickup with no repair or purchase wording. Either way those items' fee subject becomes
    return_shipping; the plan itself stays the model's proposal. Returns {'rule', 'note', 'items', 'fee_flow'} (the
    return flow the rule read or assumed) or None."""
    unknown = [item for item in items if item["fee_subject"] == "unknown"]
    message = message or ""
    if not unknown or _REPAIR_WORDS.search(message) or _PURCHASE_WORDS.search(message):
        return None
    if plan["fee_flow"] in RETURN_FLOWS:
        rule, note, flow = "planner_fee_flow", None, plan["fee_flow"]
    elif plan["fee_flow"] == "unknown" and _PICKUP_WORDS.search(message) and not _OTHER_PICKUP_WORDS.search(message):
        rule, note, flow = "home_pickup_wording", "pickup_return_assumed", "return_home_pickup"
    else:
        return None
    for item in unknown:
        item["fee_subject"] = "return_shipping"
    return {"rule": rule, "note": note, "items": [item["index"] for item in unknown], "fee_flow": flow}
