#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三方支付对账工具（纯 Python 标准库，单文件）

输入：支付单流、渠道回单流、商户入账流（CSV，每条含 交易号,金额,状态）
输出：交易清单 + 轧差错误清单

对账规则：
  1. 同一交易号的三方记录按状态机归并，与到达顺序无关（渠道先到、支付单后到也正确）。
  2. 状态机：失败 为终态；成功 之后允许 退款；退款 不允许凭空出现。
  3. 同一方同一交易号同一状态的重复记录拦截并报告。
  4. 退款金额超过原交易成功金额要拦截。
  5. 三方齐全且净额（成功-退款）一致才算平账；缺任何一方都报告缺哪方。
  6. 三方金额不一致时报告，并指出哪一方缺记录 / 哪一方金额异常。

用法：
  python3 recon.py 支付单.csv 渠道回单.csv 商户入账.csv
  python3 recon.py --demo        # 运行内置对账示例与错误样例
"""

import argparse
import csv
import io
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation

PARTIES = ("支付单", "渠道回单", "商户入账")

STATUS_SUCCESS = "成功"
STATUS_FAIL = "失败"
STATUS_REFUND = "退款"
VALID_STATUS = {STATUS_SUCCESS, STATUS_FAIL, STATUS_REFUND}

HEADER_ALIASES = {
    "交易号": "txn_id", "txn_id": "txn_id", "txnid": "txn_id", "订单号": "txn_id",
    "金额": "amount", "amount": "amount",
    "状态": "status", "status": "status",
}


class Record:
    __slots__ = ("txn_id", "amount", "status", "party", "line")

    def __init__(self, txn_id, amount, status, party, line):
        self.txn_id = txn_id
        self.amount = amount
        self.status = status
        self.party = party
        self.line = line

    def __repr__(self):
        return "<%s %s %s %s>" % (self.party, self.txn_id, self.status, self.amount)


class TxnAgg:
    """同一交易号在三方的归并结果。"""

    def __init__(self, txn_id):
        self.txn_id = txn_id
        # party -> {status: Record}
        self.records = {p: {} for p in PARTIES}
        self.errors = []  # [(类别, 描述)]

    def error(self, category, detail):
        self.errors.append((category, detail))

    def net(self, party):
        """某方净额：成功 - 退款；失败记 0；无记录返回 None。"""
        recs = self.records[party]
        if not recs:
            return None
        if STATUS_SUCCESS in recs:
            amount = recs[STATUS_SUCCESS].amount
            if STATUS_REFUND in recs:
                amount -= recs[STATUS_REFUND].amount
            return amount
        return Decimal("0")


def parse_stream(text, party, errors):
    """解析一路 CSV 流，返回 Record 列表。解析级错误记入 errors。"""
    records = []
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        return records

    header = [c.strip().lower() for c in rows[0]]
    if any(h in HEADER_ALIASES for h in header):
        idx = {}
        for i, h in enumerate(header):
            key = HEADER_ALIASES.get(h)
            if key and key not in idx:
                idx[key] = i
        data_rows = rows[1:]
    else:
        idx = {"txn_id": 0, "amount": 1, "status": 2}
        data_rows = rows

    for lineno, row in enumerate(data_rows, start=2 if data_rows is not rows else 1):
        try:
            txn_id = row[idx["txn_id"]].strip()
            amount = Decimal(row[idx["amount"]].strip())
            status = row[idx["status"]].strip()
        except (IndexError, InvalidOperation) as exc:
            errors.append(("解析错误", "%s 第%d行无法解析: %s (%s)" % (party, lineno, row, exc)))
            continue
        if not txn_id:
            errors.append(("解析错误", "%s 第%d行交易号为空" % (party, lineno)))
            continue
        if status not in VALID_STATUS:
            errors.append(("状态非法", "%s 第%d行交易 %s 状态 %r 非法" % (party, lineno, txn_id, status)))
            continue
        records.append(Record(txn_id, amount, status, party, lineno))
    return records


def merge_record(agg, rec):
    """把一条记录归并进交易聚合体；同一方重复记录在此拦截。

    状态机生命周期校验不在这里做：三路流允许乱序到达（如渠道回单的退款
    先于支付单的成功到达），只有全部归并完成后才能按状态集合正确判定，
    见 validate_txn。
    """
    recs = agg.records[rec.party]
    if rec.status in recs:
        first = recs[rec.status]
        agg.error("重复记录", "%s 交易 %s 状态[%s]重复：第%d行与第%d行（保留先到的，金额 %s / %s）"
                  % (rec.party, rec.txn_id, rec.status, first.line, rec.line,
                     first.amount, rec.amount))
        return
    recs[rec.status] = rec


# 同一方内合法的状态组合（状态机归并结果，与到达顺序无关）：
#   {成功} / {失败} / {成功, 退款}
VALID_COMBOS = (
    frozenset({STATUS_SUCCESS}),
    frozenset({STATUS_FAIL}),
    frozenset({STATUS_SUCCESS, STATUS_REFUND}),
)


def validate_txn(agg):
    """归并完成后对单笔交易做整体校验（与到达顺序无关）。"""
    for party in PARTIES:
        recs = agg.records[party]
        combo = frozenset(recs)
        if not combo:
            continue
        if combo not in VALID_COMBOS:
            if combo == frozenset({STATUS_REFUND}):
                agg.error("退款无成功", "%s 交易 %s 存在退款但无成功记录（第%d行）"
                          % (party, agg.txn_id, recs[STATUS_REFUND].line))
            else:
                agg.error("状态流转非法", "%s 交易 %s 状态组合[%s]不合法（状态机：成功后可退款，失败为终态）"
                          % (party, agg.txn_id, "/".join(sorted(combo))))
        # 退款金额不得超过原交易成功金额
        if STATUS_REFUND in recs and STATUS_SUCCESS in recs:
            if recs[STATUS_REFUND].amount > recs[STATUS_SUCCESS].amount:
                agg.error("退款超限", "%s 交易 %s 退款金额 %s 超过成功金额 %s"
                          % (party, agg.txn_id, recs[STATUS_REFUND].amount,
                             recs[STATUS_SUCCESS].amount))

    # 三方齐全性：缺哪方报哪方
    missing = [p for p in PARTIES if not agg.records[p]]
    for p in missing:
        agg.error("缺少记录", "交易 %s 缺少[%s]记录" % (agg.txn_id, p))

    # 状态冲突：一方成功、另一方失败
    has_success = any(STATUS_SUCCESS in agg.records[p] for p in PARTIES)
    has_fail = any(STATUS_FAIL in agg.records[p] for p in PARTIES)
    if has_success and has_fail:
        where = {p: sorted(agg.records[p]) for p in PARTIES if agg.records[p]}
        agg.error("状态冲突", "交易 %s 三方状态冲突：%s"
                  % (agg.txn_id, "; ".join("%s=%s" % (p, "/".join(s)) for p, s in where.items())))

    # 金额一致性：比较三方净额，指出不一致/缺失方
    nets = {p: agg.net(p) for p in PARTIES if agg.records[p]}
    if len(nets) >= 2 and len(set(nets.values())) > 1:
        detail = "; ".join("%s净额=%s" % (p, n) for p, n in nets.items())
        if missing:
            detail += "；缺[%s]记录" % "/".join(missing)
        agg.error("金额不一致", "交易 %s 三方净额不一致：%s" % (agg.txn_id, detail))


def reconcile(pay_text, channel_text, merchant_text):
    """主流程：解析 -> 归并 -> 校验 -> 输出 (交易清单, 错误清单)。"""
    parse_errors = []
    streams = [
        parse_stream(pay_text, "支付单", parse_errors),
        parse_stream(channel_text, "渠道回单", parse_errors),
        parse_stream(merchant_text, "商户入账", parse_errors),
    ]

    aggs = {}
    # 乱序到达无关：先全部归并，再统一校验
    for records in streams:
        for rec in records:
            agg = aggs.setdefault(rec.txn_id, TxnAgg(rec.txn_id))
            merge_record(agg, rec)

    for agg in aggs.values():
        validate_txn(agg)

    txn_list = []
    error_list = list(parse_errors)
    for txn_id in sorted(aggs):
        agg = aggs[txn_id]
        nets = {p: agg.net(p) for p in PARTIES}
        balanced = (not agg.errors
                    and all(nets[p] is not None for p in PARTIES)
                    and len(set(nets.values())) == 1)
        txn_list.append({
            "txn_id": txn_id,
            "nets": nets,
            "result": "平" if balanced else "差",
        })
        error_list.extend(agg.errors)
    return txn_list, error_list


def fmt_net(value):
    return "-" if value is None else str(value)


def render(txn_list, error_list, out):
    w = out.write
    w("=" * 72 + "\n")
    w("交易清单\n")
    w("=" * 72 + "\n")
    w("%-16s %-12s %-12s %-12s %s\n" % ("交易号", "支付单净额", "渠道回单净额", "商户入账净额", "结果"))
    w("-" * 72 + "\n")
    for t in txn_list:
        w("%-16s %-12s %-12s %-12s %s\n" % (
            t["txn_id"], fmt_net(t["nets"]["支付单"]),
            fmt_net(t["nets"]["渠道回单"]), fmt_net(t["nets"]["商户入账"]), t["result"]))
    ok = sum(1 for t in txn_list if t["result"] == "平")
    w("-" * 72 + "\n")
    w("共 %d 笔交易：平账 %d 笔，差异 %d 笔\n\n" % (len(txn_list), ok, len(txn_list) - ok))

    w("=" * 72 + "\n")
    w("轧差错误清单\n")
    w("=" * 72 + "\n")
    if not error_list:
        w("（无错误，三方全部轧平）\n")
    else:
        by_cat = defaultdict(list)
        for cat, detail in error_list:
            by_cat[cat].append(detail)
        for cat in sorted(by_cat):
            w("[%s] %d 条\n" % (cat, len(by_cat[cat])))
            for d in by_cat[cat]:
                w("  - %s\n" % d)
    w("\n")


# ---------------------------------------------------------------- 内置示例

DEMO_OK = {
    "支付单": """交易号,金额,状态
T1001,100.00,成功
T1002,200.00,成功
T1002,50.00,退款
T1003,300.00,失败
""",
    # 故意乱序：渠道回单里 T1002 的退款排在成功之前，T1001 整体在最后
    "渠道回单": """交易号,金额,状态
T1002,50.00,退款
T1002,200.00,成功
T1003,300.00,失败
T1001,100.00,成功
""",
    "商户入账": """交易号,金额,状态
T1003,300.00,失败
T1001,100.00,成功
T1002,200.00,成功
T1002,50.00,退款
""",
}

DEMO_BAD = {
    "支付单": """交易号,金额,状态
T2001,100.00,成功
T2001,100.00,成功
T2002,200.00,成功
T2003,80.00,成功
T2003,120.00,退款
T2004,50.00,退款
T2005,60.00,成功
T2006,70.00,成功
T2007,90.00,失败
""",
    "渠道回单": """交易号,金额,状态
T2001,100.00,成功
T2002,200.00,成功
T2003,80.00,成功
T2003,120.00,退款
T2004,50.00,退款
T2005,66.00,成功
T2007,90.00,成功
""",
    "商户入账": """交易号,金额,状态
T2001,100.00,成功
T2002,200.00,成功
T2003,80.00,成功
T2003,120.00,退款
T2004,50.00,退款
T2005,60.00,成功
T2007,90.00,失败
""",
}


def run_demo(out):
    w = out.write
    w("########## 示例一：正常对账（含乱序到达、部分退款、失败单） ##########\n\n")
    txn_list, error_list = reconcile(DEMO_OK["支付单"], DEMO_OK["渠道回单"], DEMO_OK["商户入账"])
    render(txn_list, error_list, out)
    w("\n########## 示例二：错误样例（重复/缺方/金额不符/退款异常/状态冲突） ##########\n\n")
    txn_list, error_list = reconcile(DEMO_BAD["支付单"], DEMO_BAD["渠道回单"], DEMO_BAD["商户入账"])
    render(txn_list, error_list, out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="三方支付对账工具（支付单/渠道回单/商户入账）")
    parser.add_argument("files", nargs="*", help="依次为：支付单.csv 渠道回单.csv 商户入账.csv")
    parser.add_argument("--demo", action="store_true", help="运行内置对账示例与错误样例")
    args = parser.parse_args(argv)

    if args.demo or not args.files:
        run_demo(sys.stdout)
        return 0
    if len(args.files) != 3:
        parser.error("需要三个 CSV 文件：支付单 渠道回单 商户入账（或用 --demo）")

    texts = []
    for path in args.files:
        with open(path, "r", encoding="utf-8-sig") as f:
            texts.append(f.read())
    txn_list, error_list = reconcile(*texts)
    render(txn_list, error_list, sys.stdout)
    return 1 if error_list else 0


if __name__ == "__main__":
    sys.exit(main())
