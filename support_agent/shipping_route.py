"""Conservative, source-independent return-route facts from user turns.

Only explicit province-level directions and named endpoint corrections are
accepted. These are user assertions, never verified order or policy facts.
"""
from __future__ import annotations

import re


# Province-level region names for explicit route endpoints.
NODES = ('黑龙江','内蒙古','新疆','西藏','宁夏','广西','北京','天津','上海','重庆',
         '河北','河南','云南','辽宁','湖南','安徽','山东','江苏','浙江','江西','湖北',
         '甘肃','山西','陕西','吉林','福建','贵州','广东','青海','四川','海南',
         '香港','澳门','台湾')


def normalize_node(value):
    for node in NODES:
        if value == node or value in {node + s for s in ('省', '市', '自治区', '壮族自治区', '回族自治区', '维吾尔自治区')}:
            return node
    return value


_NODE = "(?:" + "|".join(re.escape(node) for node in sorted(NODES, key=len, reverse=True)) + ")"
_PLACE = _NODE + r"(?:壮族自治区|回族自治区|维吾尔自治区|自治区|省|市)?"
_MAINLAND_NODE = "(?:" + "|".join(re.escape(node) for node in sorted(
    (node for node in NODES if node not in {"香港", "澳门", "台湾"}), key=len, reverse=True)) + ")"
_MAINLAND_PLACE = _MAINLAND_NODE + r"(?:壮族自治区|回族自治区|维吾尔自治区|自治区|省|市)?"
_PAIR = re.compile(
    rf"(?P<origin>{_PLACE})\s*(?:自行寄回|自己寄回|自寄|寄回)(?:到|至)?\s*(?P<destination>{_PLACE})|"
    rf"(?P<arrow_origin>{_PLACE})\s*(?:→|->)\s*(?P<arrow_destination>{_PLACE})")
_FORWARD_PAIR = re.compile(rf"(?:从|由)?{_PLACE}\s*(?:寄到|寄往|发往|发到)\s*{_PLACE}")
_ORIGIN = re.compile(
    rf"(?:寄出地|寄件地|发货地|寄出省份|起点)\s*(?:更正为|改为|改成|换成|是|为|在|[:：])\s*(?P<value>{_PLACE})|"
    rf"(?:仍|还是|继续)?从\s*(?P<from>{_PLACE})\s*(?:自行寄回|自己寄回|自寄|寄出|寄回|寄往|寄到|寄)")
_DESTINATION = re.compile(
    rf"(?:收件地|收件省份|退回地址|目的地|终点)\s*(?:更正为|改为|改成|换成|是|为|在|[:：])\s*(?P<value>{_PLACE})")
_ROUTE_TOPIC = re.compile(r"逆向运费|补偿表|赔付表|运费表|首重|(?:发货地|寄件地|寄出地).{0,20}(?:收件地|目的地)")
_ROUTE_QUESTION = re.compile(r"(?:金额|多少钱|参考标准|补偿标准|赔付标准|表列|核对|查表|查.{0,5}表|方向|路线|哪个发货地|哪个收件地)")
_AMOUNT_QUESTION = re.compile(r"金额|多少钱|参考标准|补偿标准|赔付标准|表列|查表")
_NO_AMOUNT = re.compile(r"(?:不要|不必|不用|无需|别|不想|暂不).{0,12}(?:金额|数额|报数|查表)|只问.{0,12}(?:条件|范围|方向)")
_EXAMPLE = re.compile(r"假如|假设|比如|例如|举例|原订单|原配送|原发货|听说|据说|客服说|页面写|规则写")
_INVALID = re.compile(r"不确定|不知道|还不知道|没确认|未确认|撤回|作废")
_DENIAL = re.compile(r"不是|并非|不从|不要按|别按|没说")
_CORRECTION = re.compile(r"更正|改成|改为|换成|说错|现在是")
_ORIGIN_ROLE = re.compile(r"寄件地|寄出地|发货地|寄出省份|起点")
_DESTINATION_ROLE = re.compile(r"收件地|收件省份|退回地址|目的地|终点")
_MERCHANT_CONFLICT = re.compile(r"第三方(?:店|商家|卖家)|非自营|不是京东自营|不属于京东自营|淘宝|拼多多")
_REGION_CONFLICT = re.compile(r"香港|澳门|台湾|港澳台|海外")
_POSITIVE_JD_SCOPE = re.compile(r"(?:这|这单|本单|这笔订单|当前订单|这件商品|现在)(?:是|属于)京东自营|"
                                r"(?:改为|更正为|而是)京东自营|(?:^|[，,；;。])是京东自营")
_POSITIVE_MAINLAND = re.compile(rf"(?:收货地址|收件地址|收货地|订单收货地)\s*"
                                rf"(?:更正为|改为|现在是|是|在)\s*(?:中国大陆|大陆|{_MAINLAND_PLACE})")
_BRANCH_CONFLICT = re.compile(r"(?:不是|并非|非|不属于)\s*京东原因|京东(?:不|无)责任")
_RESET = re.compile(r"新政策场景|另一个政策问题|另一个问题|换个问题|另一单|换一单|其他订单|新订单|新任务|"
                    r"不查(?:这个|这条)?(?:路线|运费表|补偿表)")
_SPLIT = re.compile(r"[，,。！？!?；;\n]+")


def route_question(message):
    """A bounded trigger; keyword overlap never licenses an amount by itself."""
    return bool(_ROUTE_TOPIC.search(message) and _ROUTE_QUESTION.search(message))


def _province(value):
    normalized = normalize_node(value)
    return normalized if normalized in NODES else None


def resolve_user_route(message, history):
    """Return current explicit direction or a missing/ambiguous status.

    Only authenticated conversation user turns participate. A correction replaces
    its named endpoint; conflicting or negated assertions cannot revive old data.
    """
    if not route_question(message):
        return {"status": "not_requested", "origin": None, "destination": None,
                "amount_requested": False, "missing": []}
    turns = [row["content"] for row in history if row.get("role") == "user"
             and row.get("source_kind") != "historical_external" and type(row.get("content")) is str]
    turns.append(message)
    facts = {"origin": None, "destination": None}
    ambiguous = False
    merchant_conflict = region_conflict = False
    branch_conflict = False
    for turn in turns:
        resets = list(_RESET.finditer(turn))
        if resets:
            facts = {"origin": None, "destination": None}
            merchant_conflict = region_conflict = branch_conflict = False
            # Earlier words in the same message belong to the superseded
            # order/topic; they cannot seed the new route.
            turn = turn[resets[-1].end():]
        scope_marks = list(_MERCHANT_CONFLICT.finditer(turn))
        positive_marks = list(_POSITIVE_JD_SCOPE.finditer(turn))
        if scope_marks or positive_marks:
            merchant_conflict = bool(scope_marks and
                                     (not positive_marks or scope_marks[-1].start() > positive_marks[-1].start()))
        region_marks = list(_REGION_CONFLICT.finditer(turn))
        mainland_marks = list(_POSITIVE_MAINLAND.finditer(turn))
        if region_marks or mainland_marks:
            region_conflict = bool(region_marks and
                                   (not mainland_marks or region_marks[-1].start() > mainland_marks[-1].start()))
        if _BRANCH_CONFLICT.search(turn):
            branch_conflict = True
        if re.search(r"(?:改为|更正为)京东原因", turn):
            branch_conflict = False
        turn_pair = None
        for clause in _SPLIT.split(turn):
            if not clause or _EXAMPLE.search(clause):
                continue
            if _FORWARD_PAIR.search(clause):
                facts = {"origin": None, "destination": None}
                ambiguous = True
                continue
            if re.search(r"不是这(?:次|条)|并非这(?:次|条)|不按这条|不是本次", clause):
                facts = {"origin": None, "destination": None}
                ambiguous = True
                continue
            if (_CORRECTION.search(clause) or _DENIAL.search(clause)):
                if _ORIGIN_ROLE.search(clause):
                    facts["origin"] = None
                if _DESTINATION_ROLE.search(clause):
                    facts["destination"] = None
                # Unparsed whole-direction corrections must not retain both
                # endpoints of the old route.
                if re.search(rf"{_PLACE}\s*(?:到|至)\s*{_PLACE}", clause) and not _PAIR.search(clause):
                    facts = {"origin": None, "destination": None}
            # Explicit uncertainty clears the named side, including an older
            # value. A sentence about both endpoints clears both.
            if _INVALID.search(clause):
                if re.search(r"寄件地|寄出地|发货地|寄出省份|起点|从.{0,12}寄", clause):
                    facts["origin"] = None
                if re.search(r"收件地|目的地|退回地址|终点|寄到|寄回到", clause):
                    facts["destination"] = None
                if re.search(r"路线|方向|两端", clause):
                    facts = {"origin": None, "destination": None}
                continue
            pairs = list(_PAIR.finditer(clause))
            corrections = bool(_CORRECTION.search(clause))
            pair_values = [(p["origin"] or p["arrow_origin"], p["destination"] or p["arrow_destination"])
                           for p in pairs]
            if len(set(pair_values)) > 1 and not corrections:
                facts = {"origin": None, "destination": None}
                ambiguous = True
                continue
            if pairs:
                pair = pairs[-1] if corrections else pairs[0]
                if pair["arrow_origin"] and not re.search(r"退回|退货|自寄|寄回|逆向", clause):
                    facts = {"origin": None, "destination": None}
                    ambiguous = True
                    continue
                if _DENIAL.search(clause[pair.end():]) and re.search(r"这次|本次|路线|方向|退货|退回", clause[pair.end():]):
                    facts = {"origin": None, "destination": None}
                    ambiguous = True
                    continue
                prefix = clause[:pair.start()]
                if corrections:
                    markers = list(_CORRECTION.finditer(prefix))
                    if markers:
                        prefix = prefix[markers[-1].end():]
                if _DENIAL.search(prefix) or ("商家" in clause or "卖家" in clause) and not re.search(r"我.{0,8}(?:自寄|寄回)", clause):
                    facts = {"origin": None, "destination": None}
                    ambiguous = True
                    continue
                candidate = (_province(pair["origin"] or pair["arrow_origin"]),
                             _province(pair["destination"] or pair["arrow_destination"]))
                if turn_pair is not None and candidate != turn_pair and not corrections:
                    facts = {"origin": None, "destination": None}
                    ambiguous = True
                    continue
                turn_pair = candidate
                facts.update(origin=candidate[0], destination=candidate[1])
                ambiguous = False
            for side, pattern in (("origin", _ORIGIN), ("destination", _DESTINATION)):
                matches = list(pattern.finditer(clause))
                if matches:
                    values = {_province(match.group("value") or match.groupdict().get("from")) for match in matches}
                    if len(values) > 1 and not corrections:
                        facts[side], ambiguous = None, True
                    else:
                        match = matches[-1] if corrections else matches[0]
                        prefix = clause[:match.start()]
                        if corrections:
                            markers = list(_CORRECTION.finditer(prefix))
                            if markers:
                                prefix = prefix[markers[-1].end():]
                        if _DENIAL.search(prefix):
                            facts[side], ambiguous = None, True
                        else:
                            facts[side] = _province(match.group("value") or match.groupdict().get("from"))
            # A new city-level or otherwise unsupported endpoint must not
            # silently keep an old province from the prior turn.
            if re.search(r"(?:寄件地|寄出地|发货地|从)\s*(?:更正为|改为|改成|是|为|[:：])?\s*[\u4e00-\u9fff]{2,8}市\s*(?:自寄|寄)", clause) and not _ORIGIN.search(clause):
                facts["origin"] = None
            if re.search(r"(?:收件地|目的地|寄到|寄回到)\s*(?:更正为|改为|改成|是|为|[:：])?\s*[\u4e00-\u9fff]{2,8}市", clause) and not _DESTINATION.search(clause) and not pairs:
                facts["destination"] = None
    missing = [side for side in ("origin", "destination") if facts[side] is None]
    status = ("out_of_scope" if merchant_conflict or region_conflict else "branch_conflict" if branch_conflict else
              "ambiguous" if ambiguous else "needs_clarification" if missing else "resolved")
    return {"status": status,
            **facts, "missing": missing,
            "amount_requested": bool(_AMOUNT_QUESTION.search(message)) and not bool(_NO_AMOUNT.search(message))}
