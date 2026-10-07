#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三方支付对账工具（纯标准库，单文件）。

输入：支付单流、渠道回单流、商户入账流三个文件，
每行一条记录：交易号,金额,状态   （状态 ∈ {成功, 失败, 退款}，支持英文别名）
支持空行与 # 注释。金额按分精确处理（Decimal）。

用法：
    python3 recon.py 支付单.csv 渠道回单.csv 商户入账.csv
    python3 recon.py --demo          # 内置平账样例 + 错误样例，可直接验证
"""

import csv
import io
import sys
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

# 状态机：初始 -> 成功 -> (退款)* -> 终态；初始 -> 失败 -> 终态
# 退款只允许挂在成功之后；失败为终态，之后不允许任何记录。
SUCCESS, FAILURE, REFUND = "成功", "失败", "退款"
STATUS_ALIAS = {
    "成功": SUCCESS, "success": SUCCESS, "s": SUCCESS,
    "失败": FAILURE, "failure": FAILURE, "fail": FAILURE, "f": FAILURE,
    "退款": REFUND, "refund": REFUND, "r": REFUND,
}
PARTIES = ["支付单", "渠道回单", "商户入账"]


class Rec:
    __slots__ = ("party", "txn", "amount", "status", "line")

    def __init__(self, party, txn, amount, status, line):
        self.party, self.txn, self.amount, self.status, self.line = (
            party, txn, amount, status, line)


def parse_stream(party, text):
    """解析单条流，返回 (记录列表, 解析错误列表)。"""
    recs, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        row = next(csv.reader(io.StringIO(line)))
        if len(row) != 3:
            errors.append(f"[解析错误] {party} 第{lineno}行：字段数不为3 -> {line}")
            continue
        txn, amt_s, st_s = (c.strip() for c in row)
        status = STATUS_ALIAS.get(st_s.lower(), STATUS_ALIAS.get(st_s))
        if status is None:
            errors.append(f"[解析错误] {party} 第{lineno}行：非法状态 '{st_s}'")
            continue
        try:
            amount = Decimal(amt_s)
        except InvalidOperation:
            errors.append(f"[解析错误] {party} 第{lineno}行：非法金额 '{amt_s}'")
            continue
        if amount < 0:
            errors.append(f"[解析错误] {party} 第{lineno}行：金额为负 '{amt_s}'")
            continue
        if not txn:
            errors.append(f"[解析错误] {party} 第{lineno}行：交易号为空")
            continue
        recs.append(Rec(party, txn, amount, status, lineno))
    return recs, errors


class Txn:
    """同一交易号在三方的归并视图（状态机跨流延续）。"""

    def __init__(self, txn):
        self.txn = txn
        # party -> {"成功": [金额...], "失败": [...], "退款": [...]}（已去重）
        self.legs = {p: {SUCCESS: [], FAILURE: [], REFUND: []} for p in PARTIES}
        self.errors = []  # (错误码, 描述)

    def err(self, code, msg):
        self.errors.append((code, msg))

    def add(self, rec):
        leg = self.legs[rec.party][rec.status]
        # 同一方重复记录拦截：同状态同金额 -> 重复；同状态不同金额 -> 冲突
        if rec.amount in leg:
            self.err("重复记录",
                     f"{rec.party} 第{rec.line}行：{rec.status} {rec.amount} 与已有记录重复，已拦截")
            return
        if leg and rec.status in (SUCCESS, FAILURE):
            self.err("重复记录",
                     f"{rec.party} 第{rec.line}行：{rec.status} 金额 {rec.amount} "
                     f"与已有 {leg[0]} 冲突，已拦截")
            return
        leg.append(rec.amount)

    # ---- 状态机与金额校验（跨流延续：任一主流的成功即可支撑退款）----
    def validate(self):
        succ = [a for p in PARTIES for a in self.legs[p][SUCCESS]]
        fail = [a for p in PARTIES for a in self.legs[p][FAILURE]]
        refunds = {p: sum(self.legs[p][REFUND]) for p in PARTIES}
        total_refund = sum(refunds.values())

        if succ and fail:
            self.err("状态冲突", "成功与失败记录同时存在（终态后又有记录）")
        if total_refund and not succ:
            self.err("退款无成功", "存在退款记录但三方均无成功记录，退款只允许在成功状态下出现")
        if fail and total_refund:
            self.err("失败后退还款", "失败为终态，之后不允许出现退款")
        if succ:
            base = max(succ)
            for p in PARTIES:
                if refunds[p] > base:
                    self.err("退款超额",
                             f"{p} 累计退款 {refunds[p]} 超过原交易金额 {base}")
        # 三方成功金额一致性
        succ_by_party = {p: self.legs[p][SUCCESS][0] for p in PARTIES
                         if self.legs[p][SUCCESS]}
        if len(set(succ_by_party.values())) > 1:
            detail = "，".join(f"{p}={a}" for p, a in succ_by_party.items())
            missing = [p for p in PARTIES if not self.legs[p][SUCCESS]
                       and not self.legs[p][FAILURE]]
            hint = f"；缺记录方：{'、'.join(missing)}" if missing else ""
            self.err("金额不一致", f"三方成功金额不一致（{detail}）{hint}")
        # 三方退款一致性
        ref_present = {p: r for p, r in refunds.items() if r}
        if len(set(ref_present.values())) > 1:
            detail = "，".join(f"{p}={a}" for p, a in ref_present.items())
            self.err("退款不一致", f"三方退款金额不一致（{detail}）")

    def missing_parties(self):
        return [p for p in PARTIES
                if not any(self.legs[p][s] for s in (SUCCESS, FAILURE, REFUND))]

    def balanced(self):
        if self.errors or self.missing_parties():
            return False
        # 三方齐全且无错误：成功金额三方一致（或三方皆失败），退款三方一致
        succ = {p: self.legs[p][SUCCESS] for p in PARTIES}
        fail = {p: self.legs[p][FAILURE] for p in PARTIES}
        refunds = {p: sum(self.legs[p][REFUND]) for p in PARTIES}
        all_succ = all(succ[p] for p in PARTIES)
        all_fail = all(fail[p] for p in PARTIES)
        if not (all_succ or all_fail):
            return False
        if all_succ and len({succ[p][0] for p in PARTIES}) != 1:
            return False
        return len(set(refunds.values())) <= 1

    def leg_str(self, party):
        parts = []
        for s in (SUCCESS, FAILURE, REFUND):
            for a in self.legs[party][s]:
                parts.append(f"{s}{a}")
        return "+".join(parts) if parts else "—"


def reconcile(streams):
    """streams: {party: text} -> (txns 有序字典, 全局错误列表)"""
    txns, errors = OrderedDict(), []
    for party in PARTIES:
        recs, perrs = parse_stream(party, streams.get(party, ""))
        errors.extend(perrs)
        for rec in recs:  # 按流内到达顺序逐条进入状态机，乱序天然兼容
            txns.setdefault(rec.txn, Txn(rec.txn)).add(rec)
    for t in txns.values():
        t.validate()
        miss = t.missing_parties()
        if miss:
            t.err("缺方记录", f"三方不齐全，缺：{'、'.join(miss)}")
    return txns, errors


def report(txns, parse_errors):
    out = []
    out.append("=" * 72)
    out.append("交易清单")
    out.append("=" * 72)
    out.append(f"{'交易号':<14}{'支付单':<16}{'渠道回单':<16}{'商户入账':<16}结果")
    for t in txns.values():
        out.append(f"{t.txn:<14}{t.leg_str('支付单'):<16}"
                   f"{t.leg_str('渠道回单'):<16}{t.leg_str('商户入账'):<16}"
                   f"{'平' if t.balanced() else '差'}")
    out.append("")
    out.append("=" * 72)
    out.append("轧差错误清单")
    out.append("=" * 72)
    n = 0
    for e in parse_errors:
        n += 1
        out.append(f"{n}. {e}")
    for t in txns.values():
        for code, msg in t.errors:
            n += 1
            out.append(f"{n}. [{code}] 交易 {t.txn}：{msg}")
    if n == 0:
        out.append("（无错误，三方全部轧平）")
    total = len(txns)
    ok = sum(1 for t in txns.values() if t.balanced())
    out.append("")
    out.append(f"汇总：交易 {total} 笔，平账 {ok} 笔，差异 {total - ok} 笔，错误 {n} 条")
    return "\n".join(out)


# ---------------- 内置样例 ----------------
DEMO_OK = {
    "支付单": """# 支付单流（注意 T002 渠道先到、支付单后到，乱序）
T001,100.00,成功
T002,50.00,成功
T003,20.00,失败
T004,80.00,成功
T004,30.00,退款
""",
    "渠道回单": """# 渠道回单流：T002 先于支付单到达（乱序）；T004 部分退款
T002,50.00,成功
T001,100.00,成功
T003,20.00,失败
T004,80.00,成功
T004,30.00,退款
""",
    "商户入账": """T001,100.00,成功
T002,50.00,成功
T003,20.00,失败
T004,80.00,成功
T004,30.00,退款
""",
}

DEMO_BAD = {
    "支付单": """B001,100.00,成功
B001,100.00,成功
B002,60.00,成功
B003,40.00,退款
B004,70.00,成功
B004,80.00,退款
B005,10.00,成功
B006,90.00,失败
B006,5.00,退款
""",
    "渠道回单": """B001,100.00,成功
B002,66.00,成功
B004,70.00,成功
B004,80.00,退款
B005,10.00,成功
B006,90.00,失败
B006,5.00,退款
""",
    "商户入账": """B001,100.00,成功
B003,40.00,退款
B004,70.00,成功
B004,80.00,退款
B006,90.00,失败
B006,5.00,退款
""",
}


def main(argv):
    if len(argv) == 2 and argv[1] == "--demo":
        print("【样例一】正常对账（含乱序、部分退款）\n")
        txns, perrs = reconcile(DEMO_OK)
        print(report(txns, perrs))
        print("\n")
        print("【样例二】错误对账（重复/缺方/金额不一致/非法退款/退款超额）\n")
        txns, perrs = reconcile(DEMO_BAD)
        print(report(txns, perrs))
        return 0
    if len(argv) != 4:
        print(__doc__)
        return 2
    streams = {}
    for party, path in zip(PARTIES, argv[1:4]):
        with open(path, encoding="utf-8") as f:
            streams[party] = f.read()
    txns, perrs = reconcile(streams)
    print(report(txns, perrs))
    return 1 if perrs or any(not t.balanced() for t in txns.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
