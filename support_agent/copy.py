"""Every fixed sentence the code writes into a chat reply, and the data catalogues the answer chooses from (plan §5.6).

B5d1 wrote the first sentences; B5d2's single assembler (assemble.py) is their only reader on the chat path and
retires the copy that graph_presentation, graph_response_text and turn used to hold. Production voice (D8):
the replies never name a real platform, never say "simulated" or "teaching", never promise a time or a person,
and never call a business rule law (D16); the one statement of the demo environment is the persistent notice. A
reason is only ever a preset label ("其他" stays "其他", the customer's own text never appears, I2). Code
sentences state what the records say, so a sentence that reports an action is used only when its record exists
(I6). Templates use str.format fields.

The ticket and failure sentences live with their records (tickets.py, sessions.py); B8 deleted the unused copies
B5d1 had drafted here. B8 wires the safety lines: a safety item's first line, by its subkind, before anything else.
"""
from __future__ import annotations

from .domain import RETURN_WINDOW_DAYS, SCENARIO_DATE

# ----- the return card (§4.2, §4.4)
# The confirmation rule next to every card a reply prepares (I1): only the customer's click submits.
SUBMIT_BY_CLICK = "点按“确认提交”，申请才会生成"
CARD_READY = ("订单 {order_id}（{product}）的退货确认卡在下方，还没有提交。"
              "请先在卡上挑一个原因，然后" + SUBMIT_BY_CLICK + "。")
CARD_KEPT = "订单 {order_id} 的确认卡仍在下方，内容没变，提交仍要你本人点按。"
CARD_VOIDED = "订单 {order_id} 的确认卡已作废，这一单没有生成退货申请。"
CARD_FOLDED_AFTER_FAILURE = "上一条消息没有处理完。请先展开下方的确认卡，核对它仍是你要办理的那一单，再决定要不要提交。"
# Open cards this reply does not state otherwise (moved unchanged from turn._envelope; also on failed turns).
OPEN_CARDS_WAITING = "待确认：订单 {order_ids} 的退货确认卡仍然有效，尚未提交。"
# Decision D4: an eligible order past the per-turn card limit keeps its "申请退货" button.
CARD_LIMIT = "这一次最多备好 {limit} 张确认卡，订单 {order_id} 请点它旁边的“申请退货”。"
# A chat withdrawal that names no one card while several are open (B3e): each card has its own button.
CARD_CHOOSE = "要作废哪一张，就点那张卡下方的“暂不提交”按钮。"
NO_OPEN_CARD = "眼下没有还没提交的退货确认卡。"

# ----- the return request
REQUEST_EXISTS = "订单 {order_id} 已经有一条退货申请，当前状态：{status}。这次不再重复生成。"
REQUEST_SUBMITTED = "退货申请已生成：订单 {order_id}，退货原因：{reason}。"
# Decision D4: no undo after a submission; return_cards reads these two for its click errors (wording unchanged).
CANCEL_AFTERSALES_PATH = "要撤销原平台上已经提交的售后申请，请在原平台这笔订单的售后详情里查看撤销入口。"
SUBMITTED_NO_UNDO = "退货申请提交后不能在这里撤回。" + CANCEL_AFTERSALES_PATH

# ----- finding the order
ORDER_NOT_FOUND = "在你当前登录的账号里，找不到这个订单号。可以核对一下号码，或直接在下方的订单里点选。"
# The message named order ids this account does not have (identity boundary, I4): the ids are the user's own words.
ORDERS_NOT_IN_ACCOUNT = "当前登录的账号里没有订单 {order_ids}。我这里只能查看和办理这个账号名下的订单。"
CANDIDATES_LEAD = "你说的可能是下面这几笔订单，请点选要办理的那一笔："
NO_RETURNABLE_ORDER = "你当前登录的账号里，眼下没有可以申请退货的订单。"
ASK_WHICH_ORDER = "要看哪一笔订单？写上订单号，或者说一下商品名都可以。"
MENTION_MATCHED = "你说的“{span}”是订单 {order_id}（{product}）。"
# B7r: the same when the outcome carries no order view (a withdrawal, a card choice): no empty brackets.
MENTION_MATCHED_ID = "你说的“{span}”是订单 {order_id}。"

# ----- one order's facts (the order block and the order panel; domain.order_rule_code decides, D3)
ORDER_LINE = "订单 {order_id}：{product}，实付 {amount} 元，签收日 {delivered_at}（{day}）。"
RECEIPT_DAY_ZERO = "签收当天"
RECEIPT_DAY = "签收后第 {days} 天"
RETURNABLE = "可以申请退货，最后可退日是 {last_date}。"
RETURNABLE_LAST_DAY = "可以申请退货，最后可退日就是今天。"
ORDERS_LEAD = "你名下共有 {count} 笔订单，按签收日从近到远排列："
NO_ORDERS = "当前登录的账号下没有订单。"
APPLY_HINT = "要退哪一单，点它旁边的“申请退货”就会出确认卡；在卡上选好原因、点按“确认提交”后，申请才会生成。"
REQUEST_STATUS = {"pending": "待处理"}
# B7r: a return the user wants later (return_request, subkind later) lists the facts and prepares nothing.
RETURN_NOT_STARTED = "这次没有备确认卡，只把订单情况列在这里。"

# ----- why an order cannot be returned here (domain.eligibility_code: rule v2, D6)
NOT_RETURNABLE = {
    "outside_window": "这一单签收到现在已超过 " + str(RETURN_WINDOW_DAYS) + " 天，最后可退日是 {last_date}，这里不能再为它申请退货。",
    "not_delivered": "这一单还没有签收，签收以后才能在这里申请退货。",
    "food_opened": "这一单是食品，拆封以后不能在这里申请退货。",
    "used_or_impaired": "这一单的商品已经用过或不再完好，这里不能为它申请退货。",
}
# D6: past the window, a quality problem goes to the after-sales entry of the original platform, by code.
QUALITY_PROBLEM_PATH = "商品自身有质量毛病的话，请到原平台这笔订单的售后入口按质量问题提交，能否受理以平台的审核为准。"

# ----- item-level degradation (K20). B7: a message may carry several items and only the dropped one was not done,
# so the degradation sentence speaks of that one item only.
ITEM_DEGRADED = "这条消息里有一项要办的事这次没有办理。可以把要办的订单写清楚再发一次，或者点订单旁边的按钮。"

# ----- an answer item left with nothing of its own (B5f: no outcome, no note); its path, if any, follows the line
ITEM_LINES = {
    "gap": "这一点我这边的资料里没有写到。",
    "refused": "这件事我这里办不了。",
    "routed": "这件事要到对应的办理入口去办。",
    "partial": "这一点我这边的资料只写到一部分，其余的还要另外核实。",
    "asked": "这一点还缺一个关键情况，方便的话把具体情况再说一下。",
}

# ----- when no answer item has anything to show (§5.5; B5f). B5d2: worded to stand alone (a turn may show nothing
# else and no path), with the one next step the user always has.
FALLBACK = "这一部分我暂时给不出准确的说明，可以先到原平台这笔订单的页面或在线客服核实。"

# ----- the directed route table (D10: the model names the places, the code looks up; K23: no prose on route turns)
ROUTE_AMOUNT = "按保存的退货运费补偿价目，从{origin}寄到{destination}，首重一栏的参考数是 {amount} 元。"
ROUTE_DIRECTION = "补偿价目要按实际退回的方向查：从{origin}寄到{destination}。"
ROUTE_REFERENCE_ONLY = "这个数只是价目上的参考，并非快递实际收取的费用，也说明不了这一单一定能拿到补偿。"
ROUTE_CONDITIONS = "能不能拿到补偿，还要看下方附带来源列出的条件。"
ROUTE_NEEDS_PLACES = "要查补偿价目，请告诉我{missing}。"
ROUTE_PLACE = {"origin": "实际寄出的省份", "destination": "实际收件的省份"}
ROUTE_NOT_FOUND = "补偿价目里没有查到这个方向的数，我不会拿反方向的数来代替；请以订单页面的说明为准。"
# A fee question about a home pickup with no other service named is read as a returned item's pickup
# (understand.settle_fee_subjects); the reply states the assumption so the user can correct it.
ASSUMED_RETURN_PICKUP = "这里按退货时上门取件寄回商品来说明；要是你问的是别的上门取件，请告诉我。"

# ----- safety (B8, minimal version, D9): the first line of a reply whose message reports danger, by the safety item's
# subkind (understand.SUBKINDS); only the standard emergency numbers; no medical or legal advice; never touches a card.
SAFETY = {
    "product_hazard": "请先停止使用并断开电源，把它放到远离易燃物的地方；如果冒烟或起火，请先离开并拨打 119。",
    "medical_emergency": "如果有人受伤或身体出现紧急状况，请立即拨打 120。",
    "fraud_or_theft": "如果有人冒充客服让你转账或照他说的操作手机，请立刻停止；已经付款的，请联系银行或支付机构止付，并拨打 110。",
    "harm_occurred": "请先远离仍有危险的商品，保留商品、碎片、票据和照片，方便后续核实。",
    "crisis": "听到你这么说，我很担心你。如果你有伤害自己或他人的念头，或者已经处在危险中，请立即拨打 110 或 120。",
    "none": "如果你或身边的人正面临危险，请立即拨打 110。",
}

# ----- the persistent notice (D8, D13): the one place that states the demo environment (not wired in B5d2)
ENVIRONMENT_NOTICE = (
    "这个售后助手自动回复：可以查看本账号的订单、说明售后规则、准备退货申请，但不能退款，也不能操作其他平台上的订单。"
    "你在确认卡里点按“确认提交”之后，退货申请才会生成；聊天里说的话代替不了这一步。"
    "当前为演示环境：账号、订单、退货申请和人工工单都是模拟出来的，不产生真实交易，也没有真人查看或回电。"
    "演示业务日期为 " + SCENARIO_DATE.isoformat() + "。规则内容来自保存的公开网页快照，生效日期没有核实。")

# ----- data catalogue 1: where to do or check something (respond.path_id; §5.1 "去处只能通过 path_id 给出").
# Each names the entry to use; none promises a time, a person or an outcome.
PATHS = {
    "policy_page": "打开商品详情页，查看售后说明和服务标识，以页面写明的为准；页面没写清楚的，问原平台的在线客服。",
    "after_sales_progress": "打开原平台这笔订单的售后详情，看当前进度和页面提示。",
    "refund_progress": "在原平台订单的退款详情里看退款状态和去向；显示已退但还没收到的，再对一下原支付渠道的账单。",
    "shipping_progress": "在原平台订单的物流详情里看发货和配送记录。",
    "courier_contact": "在物流详情里找到承运快递和它的联系入口，说明要联系配送或取件的人员。",
    "pickup_change": "在原平台这笔售后的详情里找到取件预约，按页面提示修改或取消。",
    "cancel_order": "在原平台订单详情里找“取消订单”；已经发货的，按页面提示申请拦截，或收货后再退回。",
    "cancel_aftersales": CANCEL_AFTERSALES_PATH,
    "exchange": "在原平台这笔订单的售后页面找“换货”入口，按页面提示说明问题。",
    "repair": "在原平台这笔订单的售后页面找“维修”入口，按页面提示提交。",
    "invoice": "在原平台订单的发票详情里找申请、补开或修改的入口。",
    "address_change": "在原平台订单详情里找“修改地址”；已经发货的，联系承运快递改派。",
    "price_protection": "在原平台订单详情里找“价格保护”入口；没有这个入口的，联系商家说明情况。",
    "merchant_contact": "在原平台订单页点“联系商家”，把情况和你想要的处理方式说清楚。",
    # Also the channel a ticket reply names (tickets.PLATFORM_CHANNEL); worded as before B5d2.
    "platform_human": "请通过原平台对应订单或服务页面的客服渠道，说明当前问题和已完成的步骤，核实下一步可以怎样处理。",
    "quality_after_window": QUALITY_PROBLEM_PATH,
    # B8 (standard HR3): where a complaint about a decision already made goes, beside the ticket.
    "platform_dispute": "在原平台这笔订单的售后记录里申请平台介入，附上截图和沟通记录。",
}
PATH_LABEL = "办理路径："
# B5g (service standard M6): a gap or a refusal always shows where to check. When the answer names no path, the code
# gives the catalogue's verification entry for the item's domain: a policy question's is the product page's after-sales
# notes; every other item's is the general one (B7: the planner's kind; a comparison is a policy question's subkind).
VERIFY_PATHS = {"policy_question": "policy_page"}
VERIFY_PATH_GENERAL = "platform_human"

# ----- data catalogue 2: clickable options for the one follow-up question (respond.ask.option_set_id). None is
# about the return reason: the reason is chosen on the card (decision 3, G8).
OPTION_SETS = {
    "product_category": {"kind": "fact", "options": ["手机或数码", "家用电器", "衣服鞋包", "食品", "别的品类"]},
    "item_condition": {"kind": "fact", "options": ["还没拆封", "只拆开看过", "已经用过"]},
    "receipt_date": {"kind": "fact", "options": ["今天刚签收", "签收不到 7 天", "签收超过 7 天"]},
    "service_marker": {"kind": "fact", "options": ["页面写了七天无理由", "页面没有写", "不确定"]},
    "fee_type": {"kind": "fact", "options": ["退回商品的运费", "寄去维修的运费", "下单时的配送费"]},
    "return_method": {"kind": "fact", "options": ["上门取件", "自己寄回"]},
    "merchant_scope": {"kind": "fact", "options": ["平台自营的商品", "第三方店铺的商品", "不确定"]},
}

# Every sentence above, by name (tests read this list).
TEXTS = {
    "CARD_READY": CARD_READY, "CARD_KEPT": CARD_KEPT, "CARD_VOIDED": CARD_VOIDED,
    "CARD_FOLDED_AFTER_FAILURE": CARD_FOLDED_AFTER_FAILURE, "OPEN_CARDS_WAITING": OPEN_CARDS_WAITING,
    "CARD_LIMIT": CARD_LIMIT, "CARD_CHOOSE": CARD_CHOOSE, "NO_OPEN_CARD": NO_OPEN_CARD,
    "REQUEST_EXISTS": REQUEST_EXISTS, "REQUEST_SUBMITTED": REQUEST_SUBMITTED,
    "CANCEL_AFTERSALES_PATH": CANCEL_AFTERSALES_PATH, "SUBMITTED_NO_UNDO": SUBMITTED_NO_UNDO,
    "ORDER_NOT_FOUND": ORDER_NOT_FOUND, "ORDERS_NOT_IN_ACCOUNT": ORDERS_NOT_IN_ACCOUNT,
    "CANDIDATES_LEAD": CANDIDATES_LEAD, "NO_RETURNABLE_ORDER": NO_RETURNABLE_ORDER,
    "ASK_WHICH_ORDER": ASK_WHICH_ORDER, "MENTION_MATCHED": MENTION_MATCHED, "MENTION_MATCHED_ID": MENTION_MATCHED_ID,
    "ORDER_LINE": ORDER_LINE, "RECEIPT_DAY_ZERO": RECEIPT_DAY_ZERO, "RECEIPT_DAY": RECEIPT_DAY,
    "RETURNABLE": RETURNABLE, "RETURNABLE_LAST_DAY": RETURNABLE_LAST_DAY, "ORDERS_LEAD": ORDERS_LEAD,
    "NO_ORDERS": NO_ORDERS, "APPLY_HINT": APPLY_HINT, "RETURN_NOT_STARTED": RETURN_NOT_STARTED,
    **{"NOT_RETURNABLE." + code: text for code, text in NOT_RETURNABLE.items()},
    "QUALITY_PROBLEM_PATH": QUALITY_PROBLEM_PATH,
    "ITEM_DEGRADED": ITEM_DEGRADED,
    **{"ITEM_LINES." + disposition: line for disposition, line in ITEM_LINES.items()}, "FALLBACK": FALLBACK,
    "ROUTE_AMOUNT": ROUTE_AMOUNT, "ROUTE_DIRECTION": ROUTE_DIRECTION, "ROUTE_REFERENCE_ONLY": ROUTE_REFERENCE_ONLY,
    "ROUTE_CONDITIONS": ROUTE_CONDITIONS, "ROUTE_NEEDS_PLACES": ROUTE_NEEDS_PLACES,
    **{"ROUTE_PLACE." + side: text for side, text in ROUTE_PLACE.items()},
    "ROUTE_NOT_FOUND": ROUTE_NOT_FOUND, "ASSUMED_RETURN_PICKUP": ASSUMED_RETURN_PICKUP,
    **{"SAFETY." + kind: text for kind, text in SAFETY.items()},
    "ENVIRONMENT_NOTICE": ENVIRONMENT_NOTICE,
}
# The sentences no chat reply uses yet (see the module docstring).
NOT_WIRED = frozenset({"REQUEST_SUBMITTED", "CARD_FOLDED_AFTER_FAILURE", "ENVIRONMENT_NOTICE"})
