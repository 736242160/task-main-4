#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保理份额登记与还款分配工具（纯 Python 标准库，单文件）。

用法：
    python3 factoring_alloc.py 事件脚本.txt     # 处理脚本文件
    python3 factoring_alloc.py                  # 从标准输入读取
    python3 factoring_alloc.py --demo           # 运行内置示例

事件脚本格式（每行一条指令，# 之后为注释，状态跨指令持续累积）：
    supplier   <名称>                        定义供应商（可选，默认“供应商”）
    factor     <保理商>                      登记保理商
    receivable <应收笔号> <金额>             定义应收池中的一笔应收
    transfer   <保理商> <应收笔号> <金额>    转让流：保理商买入某笔应收的份额
    repay      <保理商> <金额>               还款流：还款经由某保理商通道到账
    recourse   <金额>                        追索后供应商补款，按份额回补
    report                                   输出份额分配表与错误清单

业务规则（自定部分已注明）：
    1. 同一笔应收可被多家保理商受让，按转让金额占应收总额的比例登记份额；
    2. 同一保理商对同一笔应收的重复转让 -> 拦截，记入错误清单；
    3. 累计转让超过应收余额 -> 整笔拦截，并报告超转额；
    4. 还款到账按转让登记顺序（全局登记序号）逐家逐份依次分配；
    5. 全部份额清偿完毕后的余款（自定规则）-> 记入“溢收挂账”，退回供应商；
    6. 追索后供应商补款按各份额的未回收余额比例回补（精确到分，
       四舍五入尾差归入未回收余额最大的份额）；补款超出未回收总额的部分
       同样计入溢收挂账；
    7. 引用不存在的保理商或应收笔 -> 记入错误清单，该事件不生效；
    8. 所有事件在同一份台账上顺序处理，状态跨转让流/还款流延续。
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def parse_money(text, what="金额"):
    """把字符串解析为精确到分的 Decimal，非法格式抛 ValueError。"""
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError(f"{what}格式非法: {text!r}")


def fmt(value):
    return f"{value:.2f}"


def dwidth(text):
    """显示宽度（全角字符计 2），用于表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - dwidth(text))


def render_table(headers, rows):
    widths = [dwidth(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], dwidth(cell))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


@dataclass
class Lot:
    """一份已登记的转让份额。"""

    seq: int            # 全局登记顺序号
    factor: str         # 保理商
    receivable: str     # 应收笔号
    amount: Decimal     # 受让金额
    repaid: Decimal = ZERO    # 还款流累计回收
    recouped: Decimal = ZERO  # 追索补款累计回补

    @property
    def outstanding(self):
        return self.amount - self.repaid - self.recouped


class Ledger:
    """保理份额台账：顺序处理事件流，状态全程延续。"""

    def __init__(self):
        self.supplier = "供应商"
        self.factors = []               # 保理商登记顺序
        self.receivables = {}           # 应收笔号 -> 应收总额
        self.lots = []                  # 份额登记顺序（全局 FIFO）
        self.transferred = {}           # 应收笔号 -> 累计已转让金额
        self.pairs = set()              # (保理商, 应收笔号) 防重复转让
        self.errors = []                # 错误清单
        self.surplus = ZERO             # 溢收挂账（退回供应商）
        self.flow_log = []              # 还款/回补分配流水

    # ---- 基础登记 -------------------------------------------------------

    def error(self, line_no, msg):
        self.errors.append(f"第{line_no}行: {msg}")

    def add_factor(self, line_no, name):
        if name in self.factors:
            self.error(line_no, f"保理商重复登记，已忽略: {name}")
            return
        self.factors.append(name)

    def add_receivable(self, line_no, recv_id, amount):
        if recv_id in self.receivables:
            self.error(line_no, f"应收笔重复定义，已忽略: {recv_id}")
            return
        if amount <= ZERO:
            self.error(line_no, f"应收金额必须为正: {recv_id} {fmt(amount)}")
            return
        self.receivables[recv_id] = amount
        self.transferred[recv_id] = ZERO

    # ---- 转让流 ---------------------------------------------------------

    def transfer(self, line_no, factor, recv_id, amount):
        if factor not in self.factors:
            self.error(line_no, f"转让引用不存在的保理商: {factor}")
            return
        if recv_id not in self.receivables:
            self.error(line_no, f"转让引用不存在的应收笔: {recv_id}")
            return
        if amount <= ZERO:
            self.error(line_no, f"转让金额必须为正: {factor} {recv_id} {fmt(amount)}")
            return
        if (factor, recv_id) in self.pairs:
            self.error(line_no, f"重复转让拦截: 保理商 {factor} 已受让应收笔 {recv_id}")
            return
        total = self.receivables[recv_id]
        done = self.transferred[recv_id]
        if done + amount > total:
            over = done + amount - total
            self.error(
                line_no,
                f"超转拦截: 应收笔 {recv_id} 余额 {fmt(total)}，已转让 {fmt(done)}，"
                f"本次拟转 {fmt(amount)}，超转额 {fmt(over)}（保理商 {factor}）",
            )
            return
        self.pairs.add((factor, recv_id))
        self.transferred[recv_id] = done + amount
        self.lots.append(Lot(len(self.lots) + 1, factor, recv_id, amount))

    # ---- 还款流 ---------------------------------------------------------

    def repay(self, line_no, factor, amount):
        if factor not in self.factors:
            self.error(line_no, f"还款引用不存在的保理商: {factor}")
            return
        if amount <= ZERO:
            self.error(line_no, f"还款金额必须为正: {factor} {fmt(amount)}")
            return
        remaining = amount
        allocs = []
        for lot in self.lots:  # 按登记顺序逐家逐份分配
            if remaining <= ZERO:
                break
            if lot.outstanding <= ZERO:
                continue
            pay = min(lot.outstanding, remaining)
            lot.repaid += pay
            remaining -= pay
            allocs.append(f"#{lot.seq} {lot.factor}/{lot.receivable} {fmt(pay)}")
        if remaining > ZERO:  # 自定规则：余款计入溢收挂账，退回供应商
            self.surplus += remaining
            allocs.append(f"余款 {fmt(remaining)} 计入溢收挂账（退回{self.supplier}）")
        self.flow_log.append(
            f"第{line_no}行 还款到账 {fmt(amount)}（通道 {factor}）-> " + "；".join(allocs)
        )

    # ---- 追索回补 -------------------------------------------------------

    def recourse(self, line_no, amount):
        if amount <= ZERO:
            self.error(line_no, f"补款金额必须为正: {fmt(amount)}")
            return
        open_lots = [lot for lot in self.lots if lot.outstanding > ZERO]
        total_out = sum((lot.outstanding for lot in open_lots), ZERO)
        if not open_lots:
            self.surplus += amount
            self.flow_log.append(
                f"第{line_no}行 追索补款 {fmt(amount)}：无未回收份额，"
                f"全额计入溢收挂账（退回{self.supplier}）"
            )
            return
        distributable = min(amount, total_out)
        if distributable == total_out:
            shares = [lot.outstanding for lot in open_lots]
        else:
            # 按未回收余额比例分摊，尾差归入未回收余额最大的份额
            shares = [
                (distributable * lot.outstanding / total_out).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                for lot in open_lots
            ]
            diff = distributable - sum(shares, ZERO)
            if diff != ZERO:
                idx = max(range(len(open_lots)), key=lambda i: open_lots[i].outstanding)
                shares[idx] += diff
        allocs = []
        for lot, share in zip(open_lots, shares):
            if share > ZERO:
                lot.recouped += share
                allocs.append(f"#{lot.seq} {lot.factor}/{lot.receivable} {fmt(share)}")
        leftover = amount - distributable
        if leftover > ZERO:
            self.surplus += leftover
            allocs.append(f"余款 {fmt(leftover)} 计入溢收挂账（退回{self.supplier}）")
        self.flow_log.append(
            f"第{line_no}行 追索补款 {fmt(amount)} 按份额回补 -> " + "；".join(allocs)
        )

    # ---- 报表 -----------------------------------------------------------

    def report(self):
        out = []
        out.append("=" * 60)
        out.append(f"份额分配表（供应商：{self.supplier}）")
        out.append("=" * 60)

        out.append("\n【份额明细】")
        if self.lots:
            rows = []
            for lot in self.lots:
                total = self.receivables[lot.receivable]
                pct = (lot.amount / total * 100).quantize(CENT, rounding=ROUND_HALF_UP)
                rows.append([
                    f"#{lot.seq}", lot.factor, lot.receivable,
                    fmt(lot.amount), f"{pct}%",
                    fmt(lot.repaid), fmt(lot.recouped), fmt(lot.outstanding),
                    "已清偿" if lot.outstanding == ZERO else "未清偿",
                ])
            out.append(render_table(
                ["顺序", "保理商", "应收笔", "受让金额", "份额占比",
                 "还款回收", "追索回补", "未回收余额", "状态"], rows))
        else:
            out.append("（无已登记份额）")

        out.append("\n【保理商汇总】")
        rows = []
        for name in self.factors:
            lots = [lot for lot in self.lots if lot.factor == name]
            rows.append([
                name, str(len(lots)),
                fmt(sum((l.amount for l in lots), ZERO)),
                fmt(sum((l.repaid for l in lots), ZERO)),
                fmt(sum((l.recouped for l in lots), ZERO)),
                fmt(sum((l.outstanding for l in lots), ZERO)),
            ])
        out.append(render_table(
            ["保理商", "受让份数", "受让总额", "还款回收", "追索回补", "未回收余额"], rows))

        out.append("\n【应收池】")
        rows = []
        for recv_id, total in self.receivables.items():
            done = self.transferred[recv_id]
            pct = (done / total * 100).quantize(CENT, rounding=ROUND_HALF_UP)
            rows.append([recv_id, fmt(total), fmt(done), f"{pct}%", fmt(total - done)])
        out.append(render_table(
            ["应收笔", "应收总额", "已转让", "转让比例", "未转让余额"], rows))

        out.append("\n【还款/补款分配流水】")
        out.extend(("  " + line) for line in self.flow_log) if self.flow_log else out.append("  （无）")
        out.append(f"\n溢收挂账（退回{self.supplier}）: {fmt(self.surplus)}")

        out.append("\n【错误清单】")
        if self.errors:
            out.append(f"共 {len(self.errors)} 笔：")
            out.extend(("  " + e) for e in self.errors)
        else:
            out.append("  无")
        return "\n".join(out)

    # ---- 事件流驱动 -----------------------------------------------------

    def run(self, text, out=sys.stdout):
        reported = False
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            try:
                if cmd == "supplier" and len(args) == 1:
                    self.supplier = args[0]
                elif cmd == "factor" and len(args) == 1:
                    self.add_factor(line_no, args[0])
                elif cmd == "receivable" and len(args) == 2:
                    self.add_receivable(line_no, args[0], parse_money(args[1], "应收金额"))
                elif cmd == "transfer" and len(args) == 3:
                    self.transfer(line_no, args[0], args[1], parse_money(args[2], "转让金额"))
                elif cmd == "repay" and len(args) == 2:
                    self.repay(line_no, args[0], parse_money(args[1], "还款金额"))
                elif cmd == "recourse" and len(args) == 1:
                    self.recourse(line_no, parse_money(args[0], "补款金额"))
                elif cmd == "report" and not args:
                    print(self.report(), file=out)
                    reported = True
                else:
                    self.error(line_no, f"未知指令或参数个数错误: {raw.strip()}")
            except ValueError as exc:
                self.error(line_no, str(exc))
        if not reported:
            print(self.report(), file=out)


DEMO_SCRIPT = """\
# ===== 供应商与应收池 =====
supplier 甲供应商
receivable R1 10000
receivable R2 6000
receivable R3 4000

# ===== 保理商登记 =====
factor 保理A
factor 保理B
factor 保理C
factor 保理D

# ===== 转让流 =====
transfer 保理A R1 5000      # 份额 #1
transfer 保理B R1 3000      # 份额 #2（同一笔应收多家受让）
transfer 保理C R1 1000      # 份额 #3
transfer 保理A R2 6000      # 份额 #4
transfer 保理B R3 3000      # 份额 #5
transfer 保理A R1 500       # 重复转让 -> 拦截
transfer 保理D R1 2500      # 超转：R1 已转 9000，超转额 1500 -> 拦截
transfer 保理C R2 1000      # 超转：R2 已足额转让，超转额 1000 -> 拦截
transfer 保理X R1 1000      # 保理商不存在 -> 报告
transfer 保理B R9 1000      # 应收笔不存在 -> 报告

# ===== 还款流（按登记顺序逐家分配）=====
repay 保理A 8000            # 清偿 #1(5000)、#2(3000)
repay 保理B 5000            # 清偿 #3(1000)，#4 部分受偿 4000

# ===== 追索后供应商补款，按份额回补 =====
recourse 1000               # 未回收 #4=2000、#5=3000 -> 回补 400 / 600

# ===== 大额还款，份额清偿后余款挂账退回供应商 =====
repay 保理C 99999           # 清偿 #4(1600)、#5(2400)，余款 95999 挂账

report
"""


def main(argv):
    if "--demo" in argv:
        text = DEMO_SCRIPT
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    Ledger().run(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
