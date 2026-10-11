"""Output guards G1–G10 and the A0 counter on the answer's own words (first-principles plan §5.4), guards-v3.

B5d1 built this module, B5d2 wired it after the answer call, B5e tuned it after the R106 diagnosis. A guard reads only
the text it checks and a context code builds before the answer call: the identity's orders and their facts, the events
of this turn and earlier turns, the confirmation card on the turn, the selected cards, the turn's card catalogue (rule
names and the names its sources are published under), the path catalogue and the system's capabilities. No guard
changes business state or decides whether something gets done (principle 6). G1 checks records said found or absent
against the facts and events and never lets the user's paste be a result (B5e). B5f: the words come in pieces (a note
per item a card cannot answer, one next step, one follow-up), each checked on its own; a hit drops that piece only.
G10 (D8): the words never name a source publisher of the catalogue (a data-derived lexicon, like G3's rule names).

Shared rules, as the T10 corpora state them (tests/data/guard_corpus_v1.json rule_notes RN1–RN16):
- Clauses are cut at ，。；！？ and line breaks, and at the half-width , ; ? ! and at a half-width full stop
  followed by a space or the end (RN8).
- A quotation is left out only when it is attributed to the user, to a page or to a card (RN4), and only
  for G1, G2, G3, G5 and G8; G4, G6 and G7 read the text as written.
- Question exemption (RN1, RN2): only G1–G5, and only for what follows a marker (是否、能否、可否、该不该、
  应否、由谁, or 核实/确认/查看/询问/了解 followed within 24 characters by 是否、能否、可否、由谁、多少、
  什么时候) in the same clause. 吗, 为什么 and 能不能-style questions are not markers. Offering to do what
  the system cannot do is never exempt (RN3).
- Negation (RN7): only for G1, G2 and G7. A negation word (不、没、未、无法、没法、尚未、还没、暂未) counts
  only when it modifies the checked verb directly: modal words, adverbs, 你/您 and short prepositional
  phrases (替你、给仓库、把申请、按它 ...) may stand between, another verb or a contrast word may not; G1's claims and
  G2 also read a negated verb of saying as governing (RN17–RN23: guard_corpus_b5e.json). No condition exemption (RN14).
At most 46 regular expressions (principle 8); each lexicon compiled from the catalogue (G3's rule names, G10's source
publishers) counts as one. tests/test_guards.py counts them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
import re

from .catalog import FEE_EFFECT_SUBJECT, claim_premises

GUARDS_VERSION = "guards-v3"
MAX_PATTERNS = 46
# A0 (plan §5.4): words that announce the demo environment; only counted, never blocked.
A0_WORDS = ("模拟", "演示", "教学", "场景日")
# The only source kind whose card may carry law wording (G5). D16: no card has it today.
LAW_SOURCE_KIND = "law"
# The fixed gap sentence of the prose slot (§5.1); rule names may stand only in its clause (G3, RN11).
GAP_PHRASE = "我这边的资料没写到"
# The system's action catalogue: what an offer of the assistant may name (G1, RN3).
CAPABILITIES = ("show_return_card", "void_return_card", "open_ticket", "append_ticket_event", "withdraw_ticket",
                "show_order_facts")
CONTRAST = ("但", "不过", "可是", "却", "然而")   # RN7: a negation never reaches past these
SCOPE_ENDS = CONTRAST + ("也", "的话")   # RN17: nor a negated saying verb past 不说……也、没说错的话
NOTE_LIMIT, ASK_LIMIT = 100, 120   # B5f: a note or the next step, each (D15's 300-character prose slot is gone)
CLAIMS_PER_ITEM, CLAIMS_PER_ANSWER = 3, 6
DISPOSITIONS = ("answered", "partial", "gap", "refused", "routed", "asked", "handled_by_system")
# B5f: what a card cannot say may carry a note (a gap, a refusal, a routing); an answered item is never restated.
NOTE_DISPOSITIONS = ("gap", "refused", "routed")
# B5g (service standard M6): a gap or a refusal always shows where to check; "none" there is tidied to the item's entry.
PATH_DISPOSITIONS = ("gap", "refused")
FEE_SUBJECTS_UNKNOWN = ("unknown", "none")
REASON_OPTION_KIND = "return_reason"

# ---------------------------------------------------------------- shared patterns
_CLAUSE = re.compile(r"[，。；！？,;!?\n]|\.(?=\s|$)")
_QUOTE = re.compile(r"[“\"「『]([^”\"」』]*)[”\"」』]")
_QUOTE_OWNER = re.compile(r"(?:(?:你|您)(?:提到|说|发来|粘?贴|转述|消息)|页面|卡片)[^，。；！？]{0,10}$")
_QUESTION = re.compile(r"是否|能否|可否|该不该|应否|由谁"
                       r"|(?:核实|确认|查看|询问|了解)(?=[^，。；！？]{0,24}(?:是否|能否|可否|由谁|多少|什么时候))")
_NEG = r"(?:不|没|未|无法|没法|没办法|没有办法|尚未|还没|暂未)"
_FILLER_SAY = (r"能|会|要|可以|有|再|又|也|都|还|曾|就|直接|主动|自动|一并|一直|[你您]"
               r"|[给跟向和对替帮为把按照据凭][^，。；！？\s]{0,6}?")
_NEGATED = re.compile(_NEG + r"(?:用|必|需要|" + _FILLER_SAY + r")*$")
# B5e (G1's claims, G2): a negated verb of saying, concluding or committing (also 保证不了; not 做不了) governs the rest
# of its clause up to SCOPE_ENDS, a relation also the V了 phrase before it; 不必说、不用说、不需要说 affirm (RN17, RN18).
_SAY = r"承诺|保证|确定|确认|认定|断定|断言|判断|说|讲|告诉|答复"
_NEG_GOVERNS = re.compile(_NEG + r"(?:" + _FILLER_SAY + r")*(?:(?P<relation>等于|代表|意味着|说明|证明)|" + _SAY
                          + r"|说准|当作|当成|视为|算作|作为)|(?:" + _SAY + r")不[出了准]|给不出|下不了|定不了")
_ORDER_ID = re.compile(r"(?<![A-Za-z0-9_-])O\d{4}(?![A-Za-z0-9_-])")
_TICKET_ID = re.compile(r"H-\d{4}")
_DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)+")
_NUMBER = re.compile(r"\d{1,2}月\d{1,2}日|(?:\d+(?:\.\d+)?\s?|[一二两三四五六七八九十百千]+)"
                     r"(?:个工作日|工作日|个小时|小时|个月|月|年|天|日|元|%|次)")
_URL = re.compile(r"(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[A-Za-z0-9._~%/-]*)?")
_LAW = re.compile(r"法律|法规|法定|依法|国家规定|《[^》]*(?:法|条例)》|消费者权益保护法|电子商务法|产品质量法|民法典")

# ---------------------------------------------------------------- G1: actions claimed, states, proxies
# A completed action with 已/已经 (§5.4: 已提交、已退款、已取消、已撤销、已安排、已联系、已转、已登记、已为你……; the
# "performed action" probe of the old guard, 已（为您）完成退货, and the paid-result probe of trust, 已成功
# 退款, both belong here, RN6; only their own words, since a bare 完成 also names steps and premises), with the
# short phrases that may stand between 已 and the verb (替你、在订单上、把这单的申请、成功 ...). 已为你/帮你/替你 is
# itself the claim, whatever verb follows: when no listed verb does, the match ends at the proxy and the verb
# group stays empty (_open_claim_types).
_DONE_YI = re.compile(
    r"(?:已经?|已經)(?:[帮替为给幫為][你您]|在[^，。；！？]{1,8}?[上里中]|把[^，。；！？]{1,16}?|一并|也|都|直接|顺利|成功)*"
    r"(?:(?P<verb>提交|递交|交上|交了|退回|退还|退给|退到|退款|回到你手上|到账|撤销|撤回|撤掉|取消|登记|记下|记成|备注"
    r"|联系|转给|转到|转交|转人工|安排|受理|通过|批下|批准|办好|办完|辦好|辦完|处理|搞定|走完|在走|建好|建了|开了"
    r"|新开|新建|创建|生成|出好|改成|改为|改好|拦下|拦截|补发|作废|追加|完成退货|完成退款|转)|(?<=[帮替为幫為][你您]))")
# A completed action without 已: a result complement plus 了 (搞定了、退回去了、交上去了、改成……了 ...).
_DONE_LE = re.compile(
    r"(?P<verb>搞定|退回去|退回来|退回到|交上去|提交好|提交上去|撤掉|撤回|撤销|登记好|记下来?|办好|办完|辦好|辦完|建好"
    r"|开好|出好|生成|受理|通过|批下来?|批准|走完|安排好|处理好|处理完|是成|成功|交)了"
    r"|(?P<verb2>退给|退到|改成|改为|转给|转到|备注|递交给|记成|新开)[^，。；！？]{0,8}?了")
# Progress and state the system has no record of, contact with someone, an invented lookup, a refund result
# (the system never refunds; trust's paid-result probe without 已), English.
_STATE = re.compile(
    r"(?P<progress>正在(?:退回|退款|办理|安排|发出|处理)|(?:钱|退款|款项?)[^，。；！？]{0,8}?在[^，。；！？]{0,4}?路上"
    r"|(?:在走|走了[^，。；！？]{0,8}?)流程|等着发出)"
    r"|(?P<ticket_status>处理中|待处理)"
    r"|(?P<card_status>卡(?:片)?[^，。；！？]{0,2}?(?:仍然|仍|依然|还)?(?:有效|可用))"
    r"|(?P<contact>(?:那边|方面)(?:已经?)?(?:回复|答复|反馈)?(?:说|表示|答应)|打过招呼|(?:联系|沟通)过"
    r"|查到了?[^，。；！？]{0,8}?(?:平台|售后单|退款记录))"
    r"|(?P<paid>退款(?:已经?)?(?:成功|完成))"
    r"|(?P<english>(?i:refund(?:ed)?\s+(?:processed|completed|issued|approved)"
    r"|(?:payment|refund)\s+(?:has\s+been|is)\s+(?:processed|completed|received)))")
# What the user, their paste or a page says is theirs (RN4, also unquoted), not after SYSTEM_SIDE (RN19); 你说的X names X.
_SAID_BY_USER = re.compile(r"(?:你|您)(?:说|提到|转述)(?!的)"
                           r"|(?:页面?|记录|对话|聊天记录|短信|截图|消息)(?:里|上|中)?(?:说|写着|写的是|称|显示|提示)")
SYSTEM_SIDE = ("系统", "后台", "我们", "这边", "这里")
# Who acts: the last of these before the verb. A bare 你 (no preposition before it, not a possessive) is the user.
_AGENT = re.compile(r"我们?|系统|后台|商家|平台|客服|仓库"
                    r"|(?P<prep>[帮替给为向把让跟和对问幫為])?(?P<you>[你您])(?P<own>的|这(?:单|笔|件|个)|那(?:单|笔|件|个|张)|名下|账户)?")
_NOW = re.compile(r"新开|新建|这次|本轮|刚刚?")
# The assistant offering to act and the action it names (capabilities first; RN22: a lookup of _NOT_HELD is external).
_OFFER = re.compile(r"需要我|要不要我|让我|我们?(?:这边|这里)?(?:会|将|这就|马上|立刻|现在就|来|去|可以|能|再)"
                    r"(?!理解|明白|体会|建议|提醒|说明|解释|告诉)"
                    r"|我们?(?:这边|这里)?[^，。；！？]{0,3}?(?:帮你|替你|帮您|替您|给你去)")
_NOT_HELD, _LOOKUP = r"(?:退款(?!申请)|到账|支付|物流)", r"查看|看看|看一下|查一下|查查|查到|列出|核对"
_ACTION = re.compile(
    r"(?P<show_return_card>调出|重新调|出(?:一张)?(?:退货)?确认卡|展示|显示)"
    r"|(?P<void_return_card>作废|取消(?:这张)?(?:确认)?卡)"
    r"|(?P<open_ticket>(?:建立?|开|提交)(?:一张)?(?:人工)?工单|转人工)"
    r"|(?P<append_ticket_event>追加)"
    r"|(?P<withdraw_ticket>撤回(?:这张)?(?:人工)?工单|工单撤回)"
    r"|(?P<show_order_facts>(?:" + _LOOKUP + r")(?![^，。；！？]*?" + _NOT_HELD + r"))"
    r"|(?P<external>" + _LOOKUP + r"|打电话|致电|催|留言|联系|沟通|跟进|反馈|改(?:一下)?(?:收货)?地址|修改|盯|提交|递交"
    r"|申请|退款|取消|撤销|撤回|补发|备注|拦|让(?:专员|客服|商家|人工|同事)|安排(?:专员|客服|人工|人员)|通知(?:商家|仓库|快递))")
# B5e (G1, RN20): an order or a return request found or absent is checked against the facts and events, a record this
# system does not hold never stands; the state is about the last record noun (这单的物流信息), a bare 有 counts for those.
_REQUEST, _EXTERNAL = r"(?:退货|售后|退款)申请|申请记录", r"(?:退款|到账|支付|物流|售后|处理|执行)记录|支付流水|物流信息|售后单"
_RECORD_KIND = (r"(?:(?P<order>(?:[这那]一?[单笔](?![钱款退费账])|[这那该]个?订单)(?![^，。；！？]*?(?:" + _REQUEST + "|" + _EXTERNAL
                + r")))|(?P<request>" + _REQUEST + r")|(?P<external>" + _EXTERNAL + "))")
_RECORD_STATE = (r"(?:(?P<found>(?:已经?)?(?:查到|找到|核实到)了?(?!的)|已有|存在|(?<![没否所只])有(?![没无])(?=[^，。；！？]{0,4}?"
                 r"(?:" + _EXTERNAL + r")))|(?P<absent>查不到|找不到|没有?(?:查到|找到|发现)|未(?:查到|找到|发现)|查无|不存在"
                 r"|(?<!有)没有(?!提供|获取|返回|给出|展示|查询|读取|访问|办法|看|提交|生成|创建|发起|办理)|尚无|暂无))")
_RECORD_SEEN = re.compile(_RECORD_STATE + r"[^，。；！？]{0,12}?" + _RECORD_KIND + r"(?!的?(?:权限|接口|能力|功能|入口|渠道))")
_RECORD_STATED = re.compile(_RECORD_KIND + r"[^，。；！？]{0,8}?" + _RECORD_STATE)
# What the user pasted called a result of the system (§5.4 G1); a negated copula (不是、并非) is not a claim.
_PASTE_AS_RESULT = re.compile(
    r"(?:这段|这条|这张|这些|它|截图|你(?:贴|发|提供|转)(?:过来|来)?的)[^，。；！？]{0,10}?(?<![不非没])(?:就是|正是|是|代表"
    r"|来自|属于)[^，。；！？]{0,6}?(?:系统|后台|我们|这边|平台)[^，。；！？]{0,4}?(?:结果|记录|回复|回执|操作)")
# Verbs the user can do themselves; a bare 你 before them is the user reporting their own step.
_USER_VERBS = frozenset({"提交", "递交", "交上", "交了", "交上去", "提交好", "提交上去", "交", "撤回", "撤销", "取消",
                         "联系"})
_SUBMIT_VERBS = frozenset({"提交", "递交", "交上", "交了", "交上去", "提交好", "提交上去", "交", "递交给", "替你交"})
_CREATE_VERBS = frozenset({"提交", "递交", "交上", "交了", "交上去", "提交好", "提交上去", "交", "建好", "建了", "开了",
                           "开好", "新开", "新建", "创建", "记成", "登记", "登记好", "记下", "记下来", "生成"})
_TRANSFER_VERBS = frozenset({"转给", "转到", "转交", "转人工", "转"})
_WITHDRAW_VERBS = frozenset({"撤回", "撤销", "撤掉"})
_TICKET_WORDS = ("工单", "人工", "专员", "同事", "二线", "主管")

# ---------------------------------------------------------------- G2: promises nobody can keep
_PROMISE = re.compile(
    r"(?:会|将|就)有(?:人|专员|同事|客服|工作人员|人工)"
    r"|(?:商家|平台|客服|专员|人工|后台|仓库|快递|售后|同事|主管|工作人员)[^，。；！？]{0,6}?(?:会|将)[^，。；！？]{0,12}?"
    r"(?:联系|跟进|处理|回电|回复|答复|通知|安排|解决)"
    r"|(?P<pace>稍后|尽快|很快|马上|待会儿?|回头|随后|第一时间|优先(?!级))[^，。；！？]{0,8}?"
    r"(?:联系|跟进|处理|回电|回复|答复|通知|告诉|安排|解决|有结果|有人|到账)"
    r"|有(?:进展|变化|结果|消息|更新)[^，。；！？]{0,3}?(?:通知|告诉|提醒|联系)你"
    r"|排队|队列|轮到你|前面还有")
# "稍后、尽快" that describe the user's own pace are not promises (RN5).
_USER_PACE = re.compile(r"[你您](?:可以|想|要|希望|方便|能|打算|也可以|自己)?$")

# ---------------------------------------------------------------- G3: consequences (kept detectors)
# The twelve consequence detectors of graph_reply_guards (B5d2 deletes that module); its thirteenth, the
# performed-action probe, belongs to G1 and checks records (RN6). No negation exemption (K18).
_CONSEQUENCES = (
    ("eligibility", re.compile(r"(?<!是否)(?<!能否)(?:不支持|支持|不予|不能|无法|可以|可)(?:直接|申请|办理|进行)?"
                               r"(?:退换货|退货|换货|退换|退款|维修|拒收|赔付|补偿)")),
    ("eligibility", re.compile(r"(?:不支持|不能|无法|只能|必须).{0,5}(?:直接)?"
                               r"(?:换成|换容量|换规格|换型号|换新|换(?!货)|退后重买|退货重买)")),
    ("negative_eligibility", re.compile(r"(?:不是|不算|不构成|不能作为).{0,10}(?:退货|换货|退款|拒收|售后).{0,6}"
                                        r"(?:理由|资格|依据)")),
    ("cost", re.compile(r"(?:运费|费用|货款|税款|赔偿|补偿).{0,15}(?:由(?:客户|消费者|京东|平台|商家|卖家|买家|您)"
                        r"|免费|免收|减免|返还|报销|扣除|扣款|全额)")),
    ("cost", re.compile(r"(?:客户|消费者|京东|平台|商家|卖家|买家|您).{0,8}(?:承担|支付|赔付|补偿).{0,8}"
                        r"(?:运费|费用|货款|税款)")),
    ("cost", re.compile(r"由(?:京东|平台|商家|卖家|买家|客户|消费者|用户|您|你).{0,3}(?:承担|支付|赔付|补偿)")),
    ("cost", re.compile(r"(?:免收|免除|免|免费|返还|报销|补偿).{0,5}(?:返回|退回|往返)?运费")),
    ("cost", re.compile(r"(?:免费|运费|取件|退换).{0,12}(?:权益次数|减免次数|免费次数).{0,12}(?:用完|耗尽|不可用|不再)")),
    ("cost", re.compile(r"(?:不支持|不能|无法|不会|不可|通常不|一般不).{0,12}(?:补用|补差|差价|价保|返券|退券)")),
    ("cost", re.compile(r"(?:赠品|发票).{0,16}(?:扣除|扣款|价值|税款|承担)")),
    ("deadline", re.compile(r"(?:\d+|[一二三四五六七八九十])(?:个)?(?:自然日|工作日|天|小时|个月|年)(?:（含）|\(含\))?"
                            r"(?:内|后|起|到账|退款|退货|换货)")),
    ("existing_aftersales", re.compile(r"(?:必须|需要|应当|只(?:能|好)|先).{0,10}(?:撤销|取消|关闭).{0,10}(?:原|售后|申请)"
                                       r"|(?:撤销|取消|关闭).{0,12}(?:再|重新|才(?:能)?).{0,12}(?:申请|提交|办理)")),
)

# ---------------------------------------------------------------- G7, G8
_CREDENTIAL_ASK = re.compile(
    r"(?:密码|验证码|校验码|动态码|银行卡号|卡号|身份证号|证件号|安全码)[^，。；！？]{0,6}?"
    r"(?:发给我|发我|告诉我|发过来|给我|报给我|提供给我|填给我)"
    r"|(?:告诉我|发给我|发我|报给我|(?<![你您])提供(?!的))[^，。；！？]{0,10}?(?:密码|验证码|校验码|动态码|卡号|身份证号|证件号|安全码)"
    r"|没有(?:你的)?(?:密码|验证码|身份证号|证件号)[^，。；！？]{0,4}?就?(?:没法|无法|不能)")
_REASON_ASK = re.compile(
    r"(?:说说|说一下|说下|讲讲|讲一下|聊聊|告诉我|写一下|补充一下|没说)[^，。；！？]{0,10}?(?:原因|理由|哪里不满意|为什么)"
    r"|为什么(?:想|要)?退|(?:原因|理由)(?:是什么|是啥)|是[^，。；！？]{0,10}?才(?:想)?退|(?:哪里|什么地方)不满意")
# On a card turn: telling the user to wait for review or that nothing is left to do means "submitted".
_IMPLIES_SUBMITTED = re.compile(r"等[^，。；！？]{0,4}?审核|[不无](?:需要|用|必)你?再[^，。；！？]{0,6}?操作|留意取件")


# ---------------------------------------------------------------- context
@dataclass(frozen=True)
class Context:
    """What code knows before the answer call; built with ``context(...)``."""
    own_order_ids: frozenset = frozenset()
    events: tuple = ()
    card_state: str = "none"
    sources: str = ""
    source_digits: tuple = ()
    law_selected: bool = False
    rule_names: frozenset = frozenset()
    capabilities: frozenset = field(default_factory=lambda: frozenset(CAPABILITIES))
    answer_model_called: bool = True
    source_publishers: frozenset = frozenset()


def rule_names(cards):
    """The rule-name lexicon: every title and alias of the turn's catalogue cards (never written by hand)."""
    names = set()
    for card in cards:
        for name in [card.get("title"), *(card.get("aliases") or [])]:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return frozenset(names)


def card_source_texts(card):
    """The card text a reply may take a number, a date or an address from (G4): what the card block shows."""
    texts = [card.get(key) for key in ("title", "scope", "text")]
    for key in ("conditions", "outcomes", "exceptions", "aliases"):
        texts += list(card.get(key) or [])
    for rows in card.get("outcome_premises") or []:
        texts += list(rows)
    return [text for text in texts if isinstance(text, str) and text]


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def context(*, own_order_ids=(), order_facts=None, events=(), card_state="none", selected_cards=(), catalogue_cards=(),
            paths=None, route_text="", capabilities=CAPABILITIES, answer_model_called=True, source_publishers=()):
    """Build the guard context. Sources for G4 are the selected cards, the path catalogue, the identity's order
    facts and the route result; what the user typed or pasted never is. B5e: the code's turn statements
    ``orders_shown``, ``order_not_in_account`` among the ``events``; B5f: the catalogue's publishers for G10."""
    if card_state not in ("none", "shown", "kept"):
        raise ValueError("GUARD_CARD_STATE_INVALID")
    texts = []
    for card in selected_cards:
        texts += card_source_texts(card)
    texts += list(_strings(paths or {})) + list(_strings(order_facts or {})) + ([route_text] if route_text else [])
    return Context(own_order_ids=frozenset(own_order_ids), events=tuple(dict(event) for event in events),
                   card_state=card_state,
                   sources="".join("\n".join(texts).split()),
                   source_digits=tuple(digits for digits in ("".join(ch for ch in text if ch.isdigit())
                                                              for text in texts) if digits),
                   law_selected=any(card.get("source_kind") == LAW_SOURCE_KIND for card in selected_cards),
                   rule_names=rule_names(catalogue_cards), capabilities=frozenset(capabilities),
                   answer_model_called=answer_model_called, source_publishers=frozenset(source_publishers))


# ---------------------------------------------------------------- helpers
def clauses(text):
    return [part for part in _CLAUSE.split(text) if part.strip()]


def _mask_owned_quotes(text):
    """Empty the quotations attributed to the user, a page or a card (RN4); others stay."""
    return _QUOTE.sub(lambda match: "“”" if _QUOTE_OWNER.search(text[:match.start()]) else match.group(), text)


def _marker(clause):
    match = _QUESTION.search(clause)
    return match.start() if match else None


def _asked(marker, position):
    return marker is not None and marker <= position


def _negated(clause, position, governed=False):
    """Negated right before ``position`` (RN7); ``governed`` (G1 claims, G2): or after a negated saying verb (RN17)."""
    return bool(_NEGATED.search(clause[:position])) or governed and any(
        not any(word in clause[match.end():position] for word in SCOPE_ENDS)
        for match in _NEG_GOVERNS.finditer(clause, 0, position))


def _relation_subject(clause, match):
    """A V了 phrase (no 已, no 我/你 before it) that a negated relation follows: 提交了申请不等于…… (B5e)."""
    after = _NEG_GOVERNS.search(clause, match.end())
    return (after is not None and after.group("relation") is not None
            and not any(word in clause[match.end():after.start()] for word in CONTRAST)
            and not any(char in clause[:match.start()] for char in "我你您"))


def _user_did_it(clause, position, verb):
    if verb not in _USER_VERBS:
        return False
    agents = list(_AGENT.finditer(clause[:position]))
    last = agents[-1] if agents else None
    return bool(last and last.group("you") and not last.group("prep") and not last.group("own"))


def _attributed(clause, position):
    """Words of the user, their paste or a page (not SYSTEM_SIDE's), unless the assistant acts after them (我 ...)."""
    return any("我" not in clause[match.end():position]
               and not any(word in clause[max(0, match.start() - 6):match.start()] for word in SYSTEM_SIDE)
               for match in _SAID_BY_USER.finditer(clause[:position]))


# The record an action of the capabilities leaves (an open 已为你…… is classified by the offer vocabulary).
_ACTION_RECORDS = {"show_return_card": ("card",), "void_return_card": ("card_voided",),
                   "open_ticket": ("ticket_created",), "append_ticket_event": ("ticket_event_appended",),
                   "withdraw_ticket": ("ticket_withdrawn",), "show_order_facts": ("orders_shown",)}


def _open_claim_types(clause, position):
    """已为你…… with a verb outside the list: only an action of the capabilities can have a record."""
    action = _ACTION.search(clause, position)
    return _ACTION_RECORDS.get(action.lastgroup, ()) if action else ()


def _claim_types(clause, verb):
    """Which recorded event would back a completed-action claim; () when nothing here can record it."""
    ticket = any(word in clause for word in _TICKET_WORDS) or _TICKET_ID.search(clause)
    if ticket and verb in _WITHDRAW_VERBS:
        return ("ticket_withdrawn",)
    if verb == "追加":
        return ("ticket_event_appended",)
    if ticket and verb in _TRANSFER_VERBS:
        return ("ticket_created", "ticket_event_appended")
    if ticket and verb in _CREATE_VERBS:
        return ("ticket_created",)
    if "卡" in clause and verb in ("生成", "出好"):
        return ("card",)
    if "卡" in clause and verb == "作废":
        return ("card_voided",)
    if (verb in _SUBMIT_VERBS or (verb == "生成" and "申请" in clause)) and "取消" not in clause:
        return ("return_request_submitted",)
    return ()


def _backed(clause, types, ctx):
    """RN6: a record of the same type, and of every order or ticket the clause names; 新开/这次 need this turn."""
    if not types:
        return False
    if types == ("card",):
        return ctx.card_state in ("shown", "kept")
    scopes = ("turn",) if _NOW.search(clause) else ("turn", "session")
    events = [event for event in ctx.events if event.get("type") in types and event.get("scope") in scopes]
    orders, tickets = set(_ORDER_ID.findall(clause)), set(_TICKET_ID.findall(clause))
    if orders or tickets:
        return (all(any(event.get("order_id") == order for event in events) for order in orders)
                and all(any(event.get("ticket") == ticket for event in events) for ticket in tickets))
    return bool(events)


def _ticket_open(clause, ctx):
    created = {event.get("ticket") for event in ctx.events if event.get("type") == "ticket_created"}
    withdrawn = {event.get("ticket") for event in ctx.events if event.get("type") == "ticket_withdrawn"}
    named = set(_TICKET_ID.findall(clause))
    return named <= (created - withdrawn) if named else bool(created - withdrawn)


def _record_backed(clause, match, ctx):
    """B5e: an order or a return request found or absent agrees with the facts and events; other records never."""
    if match.group("external"):
        return False
    named = set(_ORDER_ID.findall(clause))
    if match.group("order"):
        if match.group("found"):
            return bool(ctx.own_order_ids) and named <= ctx.own_order_ids
        stated = any(event.get("type") == "order_not_in_account" for event in ctx.events)
        return stated or bool(named - ctx.own_order_ids) or not ctx.own_order_ids
    requested = {event.get("order_id") for event in ctx.events if event.get("type") == "return_request_submitted"}
    if match.group("found"):
        return named <= requested if named else bool(requested)
    return not named & requested if named else not requested


def _hit(guard, code, clause):
    return {"guard": guard, "code": code, "excerpt": clause.strip()[:80]}


# ---------------------------------------------------------------- the guards, one clause at a time
def _g1(clause, marker, ctx):
    """Completed actions, states, contacts and records need a record; offers of what the system cannot do never
    pass; the user's paste is never a result."""
    found = []
    for pattern in (_DONE_YI, _DONE_LE):
        for match in pattern.finditer(clause):
            groups = match.groupdict()
            name = "verb" if groups.get("verb") else "verb2" if groups.get("verb2") else None
            verb, at, start = groups.get(name), match.start(name) if name else match.end(), match.start()
            if (_asked(marker, start) or _negated(clause, start, governed=True) or _attributed(clause, start)
                    or _user_did_it(clause, at, verb) or (pattern is _DONE_LE and _relation_subject(clause, match))):
                continue
            types = _claim_types(clause, verb) if verb else _open_claim_types(clause, at)
            if not _backed(clause, types, ctx):
                found.append(("G1_UNBACKED_COMPLETION", True))
    for match in [*_RECORD_SEEN.finditer(clause), *_RECORD_STATED.finditer(clause)]:
        start = match.start()
        if not (_asked(marker, start) or _negated(clause, start, governed=True) or _attributed(clause, start)
                or _record_backed(clause, match, ctx)):
            found.append(("G1_RECORD_WITHOUT_SOURCE", False))
    for match in _PASTE_AS_RESULT.finditer(clause):
        if not (_asked(marker, match.start()) or _negated(clause, match.start(), governed=True)):
            found.append(("G1_PASTE_AS_RESULT", False))
    for match in _STATE.finditer(clause):
        start = match.start()
        if _asked(marker, start) or _negated(clause, start, governed=True) or _attributed(clause, start):
            continue
        if match.group("ticket_status"):
            if (any(word in clause for word in _TICKET_WORDS) or _TICKET_ID.search(clause)) and not _ticket_open(clause, ctx):
                found.append(("G1_STATE_WITHOUT_RECORD", False))
        elif match.group("card_status"):
            if ctx.card_state == "none":
                found.append(("G1_STATE_WITHOUT_RECORD", False))
        elif match.group("contact"):
            if not _user_did_it(clause, start, "联系"):
                found.append(("G1_CONTACT_WITHOUT_RECORD", False))
        else:
            found.append(("G1_UNBACKED_COMPLETION", True))
    offer = _OFFER.search(clause)
    if offer:
        action = _ACTION.search(clause, offer.end())
        if action and not _negated(clause, action.start()):
            kind = action.lastgroup
            if kind == "external" or kind not in ctx.capabilities:
                found.append(("G1_UNSUPPORTED_PROXY", False))
    return found


def _g2(clause, marker):
    for match in _PROMISE.finditer(clause):
        start = match.start()
        if _asked(marker, start) or _negated(clause, start, governed=True):
            continue
        if match.group("pace") and _USER_PACE.search(clause[:start]):
            continue
        return True
    return False


@lru_cache(maxsize=64)
def _lexicon(names):
    """One pattern for a lexicon the catalogue gives (G3's rule names, G10's source publishers)."""
    ordered = sorted(names, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, ordered))) if ordered else None


def _g3(clause, marker, ctx):
    codes = []
    for kind, pattern in _CONSEQUENCES:
        if any(not _asked(marker, match.start()) for match in pattern.finditer(clause)):
            codes.append("G3_" + kind.upper())
    names = _lexicon(ctx.rule_names)
    if names is not None and GAP_PHRASE not in clause:
        if any(not _asked(marker, match.start()) for match in names.finditer(clause)):
            codes.append("G3_RULE_NAME")
    return codes


def _g4(clause, ctx):
    marker = _marker(clause)
    tokens = []
    for pattern in (_URL, _NUMBER):
        tokens += [(match.start(), match.group()) for match in pattern.finditer(clause)]
    for match in _DIGIT_RUN.finditer(clause):
        digits = "".join(ch for ch in match.group() if ch.isdigit())
        if len(digits) >= 5:
            tokens.append((match.start(), digits))
    for start, token in tokens:
        if _asked(marker, start):
            continue
        token = "".join(token.split())
        if token.isdigit():
            if not any(token in digits for digits in ctx.source_digits):
                return True
        elif token not in ctx.sources:
            return True
    return False


def check(text, ctx):
    """Every guard hit in ``text`` (one piece: a note, the next step or the follow-up), as [{guard, code, excerpt}]
    in guard order."""
    if not isinstance(text, str):
        raise ValueError("GUARD_TEXT_INVALID")
    if not ctx.answer_model_called or not text.strip():
        return []
    hits = []
    card_turn = ctx.card_state in ("shown", "kept")
    for clause in clauses(_mask_owned_quotes(text)):
        marker = _marker(clause)
        g1 = _g1(clause, marker, ctx)
        hits += [_hit("G1", code, clause) for code, _ in g1]
        if _g2(clause, marker):
            hits.append(_hit("G2", "G2_UNBACKED_PROMISE", clause))
        hits += [_hit("G3", code, clause) for code in _g3(clause, marker, ctx)]
        if not ctx.law_selected and any(not _asked(marker, match.start()) for match in _LAW.finditer(clause)):
            hits.append(_hit("G5", "G5_LAW_WORDING", clause))
        if _REASON_ASK.search(clause):
            hits.append(_hit("G8", "G8_REASON_ASKED", clause))
        if card_turn and (_IMPLIES_SUBMITTED.search(clause) or any(completion for _, completion in g1)):
            hits.append(_hit("G8", "G8_IMPLIES_SUBMITTED", clause))
    for clause in clauses(text):
        if _g4(clause, ctx):
            hits.append(_hit("G4", "G4_UNSOURCED", clause))
        if any(order not in ctx.own_order_ids for order in _ORDER_ID.findall(clause)):
            hits.append(_hit("G6", "G6_FOREIGN_ORDER", clause))
        if any(not _negated(clause, match.start()) for match in _CREDENTIAL_ASK.finditer(clause)):
            hits.append(_hit("G7", "G7_CREDENTIAL_ASKED", clause))
        if any(sum(ch.isdigit() for ch in match.group()) >= 12 for match in _DIGIT_RUN.finditer(clause)):
            hits.append(_hit("G7", "G7_LONG_NUMBER", clause))
        # G10 (D8): no source publisher is named, quoted or asked about (the text as written).
        publishers = _lexicon(ctx.source_publishers)
        if publishers is not None and publishers.search(clause):
            hits.append(_hit("G10", "G10_PUBLISHER_NAMED", clause))
    return sorted(hits, key=lambda hit: int(hit["guard"][1:]))   # G1 ... G10 (stable within a guard)


def guard_ids(hits):
    return sorted({hit["guard"] for hit in hits}, key=lambda guard: int(guard[1:]))


def a0_counts(text):
    """A0 (§5.4): occurrences of the demo-environment words; counted for T7, never blocked."""
    return {word: text.count(word) for word in A0_WORDS if word in text}


# ---------------------------------------------------------------- G9 (and G8's structural half)
ANSWER_KEYS = {"items", "show_order_ids", "next_step", "ask"}
ITEM_KEYS = {"index", "disposition", "claims", "path_id", "note"}


def _untidy(text, limit, ctx):
    if len(text) > limit:
        return "G9_TOO_LONG"
    if "[[" in text or "]]" in text or any(identifier in text for identifier in ctx.get("identifiers", ())):
        return "G9_ID_IN_TEXT"
    return None


def check_structure(answer, ctx):
    """G9 on a ``respond`` result (§5.2, B5f): (problems, tidied answer, dropped). A problem breaks the schema (shape,
    types, an id outside the turn's lists) and goes back to ``respond`` once (§5.5). What is only untidy is removed and
    listed in ``dropped`` ([{code, part}]): a repeated row or outcome, claims past the limits or of another fee service,
    a note where none belongs, a piece too long or with an id in it, an item left out; B5g: a gap or a refusal without
    a path gets its item's ``verify_path`` (listed with the ``path_id`` given). ``ctx``: {"items": [{"index",
    "fee_subject", "verify_path"}], "cards", "path_ids", "option_sets", "own_order_ids", "identifiers"}."""
    if not isinstance(answer, dict) or set(answer) != ANSWER_KEYS or not isinstance(answer["items"], list) or any(
            not isinstance(row, dict) or set(row) != ITEM_KEYS or not isinstance(row["note"], str)
            or not isinstance(row["claims"], list) for row in answer["items"]):
        return [_hit("G9", "G9_SHAPE", "")], answer, []
    answer, problems, dropped = deepcopy(answer), [], []
    ask, shown, sets = answer["ask"], answer["show_order_ids"], ctx.get("option_sets", {})
    if not isinstance(ask, dict) or set(ask) != {"text", "option_set_id"} or not isinstance(ask["text"], str):
        problems.append(_hit("G9", "G9_ASK_SHAPE", ""))
    elif ask["option_set_id"] != "none" and ask["option_set_id"] not in sets:
        problems.append(_hit("G9", "G9_OPTION_SET_UNKNOWN", str(ask["option_set_id"])))
    if not isinstance(answer["next_step"], str):
        problems.append(_hit("G9", "G9_NEXT_STEP_SHAPE", ""))
    if not isinstance(shown, list) or any(order not in ctx.get("own_order_ids", ()) for order in shown):
        problems.append(_hit("G9", "G9_ORDER_NOT_OWN", str(shown)))
    subjects = {item["index"]: item.get("fee_subject", "unknown") for item in ctx.get("items", [])}
    kept, total = {}, 0
    for row in answer["items"]:
        if type(row["index"]) is not int or row["index"] not in subjects:
            problems.append(_hit("G9", "G9_ITEM_UNKNOWN", str(row["index"])))
        if row["disposition"] not in DISPOSITIONS:
            problems.append(_hit("G9", "G9_DISPOSITION_UNKNOWN", str(row["disposition"])))
        if row["path_id"] != "none" and row["path_id"] not in ctx.get("path_ids", ()):
            problems.append(_hit("G9", "G9_PATH_UNKNOWN", str(row["path_id"])))
        if row["index"] in kept:
            dropped.append({"code": "G9_ITEM_REPEATED", "part": f"item:{row['index']}"})
            continue
        kept[row["index"]], claims = row, []
        for claim in row["claims"]:
            card = ctx.get("cards", {}).get(claim.get("card_id")) if isinstance(claim, dict) else None
            index = claim.get("outcome_index") if isinstance(claim, dict) else None
            if card is None or type(index) is not int or not 0 <= index < len(card.get("outcomes", [])):
                problems.append(_hit("G9", "G9_CLAIM_NOT_A_CANDIDATE", str(claim)))
                continue
            if not any(part.strip() for part in claim_premises(card, index) or []):
                problems.append(_hit("G9", "G9_CLAIM_WITHOUT_PREMISES", str(claim)))
                continue
            fee, subject = FEE_EFFECT_SUBJECT.get(card.get("effect_domain")), subjects.get(row["index"], "unknown")
            code = ("G9_CLAIM_REPEATED" if claim in claims   # B5d2: an outcome may answer two items, one once each
                    else "G9_FEE_SUBJECT_MISMATCH" if fee and subject not in FEE_SUBJECTS_UNKNOWN and fee != subject
                    else "G9_CLAIMS_PER_ITEM" if len(claims) >= CLAIMS_PER_ITEM
                    else "G9_CLAIMS_PER_ANSWER" if total >= CLAIMS_PER_ANSWER else None)
            if code:
                dropped.append({"code": code, "part": f"claim:{row['index']}"})
            else:
                claims.append(claim)
                total += 1
        row["claims"] = claims
        code = row["note"].strip() and ("G9_NOTE_NOT_ALLOWED" if row["disposition"] not in NOTE_DISPOSITIONS
                                        else _untidy(row["note"], NOTE_LIMIT, ctx))
        if code:
            dropped.append({"code": code, "part": f"note:{row['index']}"})
            row["note"] = ""
    if problems:
        return problems, answer, []
    answer["items"] = list(kept.values())
    dropped += [{"code": "G9_ITEM_MISSING", "part": f"item:{index}"} for index in subjects if index not in kept]
    verify = {item["index"]: item.get("verify_path") for item in ctx.get("items", [])}
    for row in answer["items"]:
        if row["disposition"] in PATH_DISPOSITIONS and row["path_id"] == "none" and verify.get(row["index"]):
            row["path_id"] = verify[row["index"]]
            dropped.append({"code": "G9_PATH_MISSING", "part": f"path:{row['index']}", "path_id": row["path_id"]})
    code = answer["next_step"].strip() and _untidy(answer["next_step"], NOTE_LIMIT, ctx)
    if code:
        dropped.append({"code": code, "part": "next_step"})
        answer["next_step"] = ""
    option = ask["option_set_id"]   # an ask without words is never shown, and a repeated order id shows once anyway
    code = ask["text"].strip() and ("G8_REASON_OPTION_SET" if option != "none"
                                    and sets[option].get("kind") == REASON_OPTION_KIND
                                    else _untidy(ask["text"], ASK_LIMIT, ctx))
    if code:
        dropped.append({"code": code, "part": "ask"})
        answer["ask"] = {"text": "", "option_set_id": "none"}
    return [], answer, dropped
