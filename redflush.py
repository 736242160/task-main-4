#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
redflush.py — 红冲与作废级联记账工具（纯 Python 标准库，单文件）

用法：
    python3 redflush.py 输入文件          # 从文件读取
    python3 redflush.py < 输入文件        # 从标准输入读取
    python3 redflush.py --demo            # 运行内置样例（红冲链 + 净额）

输入格式（行式 DSL，# 之后为注释，空行忽略；发票定义须先于引用它的流水）：
    invoice  编号 金额 税额 税率          # 税率可写 0.13 或 13%
    redflush 原票号 新票号                # 红冲流：新票号红冲原票号
    void     票号                         # 作废流

自定记账规则（重要）：
  1. 方向：蓝字票方向为 +1。红冲流 (原票 -> 新票) 成功后，新票方向 = -原票方向。
     因此红冲链上方向交替：蓝(+)->红(-)->蓝字回转(+)->红(-)……
  2. 净额：每张非作废票贡献 方向 × 税额；作废票贡献 0。
     全额红冲的一对票净额互相抵销为 0；部分红冲时蓝票保留全额、红票冲减部分，
     合计即剩余可冲额。净额在全部流水处理完后按最终状态统一重算。
  3. 红冲校验（按顺序流水处理，跨流状态延续）：
     - 原票/新票必须已定义，否则报“引用不存在票号”；
     - 作废票再被红冲：拦截；
     - 新票必须是“未使用”的蓝字票（未作废、未参与任何红冲流）；
     - 红冲链成环（含 a冲b、b冲a、自冲）：拦截并列出环上票号；
     - 本次税额/金额超过原票剩余可冲额：拦截并报告差额；
     - 新票税率与原票不一致视为“跨期税率变更”：放行但记警告，
       冲减按红字票实际税额入账。
  4. 作废：仅允许“未参与任何红冲链”的蓝字票作废；红字票/已红冲票只能走红冲链。
  5. 失败的流水不改变任何票面状态（后续流水基于此前成功流水的状态继续）。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

ST_ISSUED = "开具"
ST_PARTIAL = "部分红冲"
ST_FLUSHED = "已红冲"
ST_RED = "红字开具"
ST_RED_PARTIAL = "红字部分红冲"
ST_RED_FLUSHED = "红字已红冲"
ST_REISSUE = "蓝字回转开具"
ST_REISSUE_PARTIAL = "蓝字回转部分红冲"
ST_REISSUE_FLUSHED = "蓝字回转已红冲"
ST_VOID = "已作废"


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_rate(text: str) -> Decimal:
    text = text.strip()
    if text.endswith("%"):
        return Decimal(text[:-1]) / Decimal(100)
    return Decimal(text)


def disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


@dataclass
class Invoice:
    no: str
    amount: Decimal
    tax: Decimal
    rate: Decimal
    sign: int = 1                      # +1 蓝字 / -1 红字，由红冲流确定
    is_child: bool = False             # 是否由红冲流产生
    void: bool = False
    flushed_tax: Decimal = ZERO        # 已被红冲的税额（绝对值累计）
    flushed_amount: Decimal = ZERO     # 已被红冲的金额（绝对值累计）

    @property
    def category(self) -> str:
        if not self.is_child:
            return "蓝字"
        return "红字" if self.sign < 0 else "蓝字回转"

    @property
    def status(self) -> str:
        if self.void:
            return ST_VOID
        full = self.flushed_tax >= abs(self.tax) and abs(self.tax) > ZERO
        part = ZERO < self.flushed_tax < abs(self.tax)
        if self.is_child:
            if self.sign < 0:
                return ST_RED_FLUSHED if full else (ST_RED_PARTIAL if part else ST_RED)
            return ST_REISSUE_FLUSHED if full else (ST_REISSUE_PARTIAL if part else ST_REISSUE)
        return ST_FLUSHED if full else (ST_PARTIAL if part else ST_ISSUED)

    @property
    def net_tax(self) -> Decimal:
        return ZERO if self.void else self.sign * self.tax


class Ledger:
    def __init__(self) -> None:
        self.invoices: dict[str, Invoice] = {}
        self.edges: dict[str, list[str]] = {}   # 原票号 -> [红字票号...]
        self.errors: list[str] = []
        self.warnings: list[str] = []

    # ---------- 发票定义 ----------
    def define(self, no: str, amount: Decimal, tax: Decimal, rate: Decimal, where: str) -> None:
        if no in self.invoices:
            self.errors.append(f"{where}：发票 {no} 重复定义，保留首次定义")
            return
        if amount < ZERO or tax < ZERO:
            self.errors.append(f"{where}：发票 {no} 金额/税额不得为负（红字方向由红冲流自动确定）")
            return
        self.invoices[no] = Invoice(no=no, amount=amount, tax=tax, rate=rate)
        expect = money(amount * rate)
        if abs(tax - expect) > Decimal("0.05"):
            self.warnings.append(
                f"{where}：发票 {no} 税额 {tax} 与 金额×税率={expect} 不符，按申报税额入账")

    # ---------- 作废流 ----------
    def void(self, no: str, where: str) -> None:
        inv = self.invoices.get(no)
        if inv is None:
            self.errors.append(f"{where}：作废流引用不存在票号 {no}")
            return
        if inv.void:
            self.errors.append(f"{where}：发票 {no} 已作废，重复作废被拦截")
            return
        if inv.is_child or inv.flushed_tax > ZERO:
            self.errors.append(
                f"{where}：发票 {no} 已参与红冲链（状态：{inv.status}），不能作废，只能继续红冲")
            return
        inv.void = True

    # ---------- 红冲流 ----------
    def _find_path(self, start: str, target: str):
        """沿 原票->新票 边找 start 到 target 的路径，找不到返回 None。"""
        stack = [(start, [start])]
        visited = set()
        while stack:
            node, path = stack.pop()
            if node == target:
                return path
            if node in visited:
                continue
            visited.add(node)
            for nxt in self.edges.get(node, ()):
                stack.append((nxt, path + [nxt]))
        return None

    def redflush(self, orig: str, new: str, where: str) -> None:
        inv_o = self.invoices.get(orig)
        inv_n = self.invoices.get(new)
        if inv_o is None:
            self.errors.append(f"{where}：红冲流 {orig} -> {new} 引用不存在票号 {orig}（原票）")
            return
        if inv_n is None:
            self.errors.append(f"{where}：红冲流 {orig} -> {new} 引用不存在票号 {new}（新票）")
            return
        if orig == new:
            self.errors.append(f"{where}：红冲链成环：{orig} 自我红冲，环上票号 [{orig}]")
            return
        if inv_o.void:
            self.errors.append(f"{where}：作废票 {orig} 再被红冲，已拦截（红字票 {new} 不生效）")
            return
        cycle = self._find_path(new, orig)
        if cycle is not None:
            ring = cycle + [new]
            self.errors.append(
                f"{where}：红冲链成环，已拦截；环上票号 [{'、'.join(dict.fromkeys(ring))}]，"
                f"路径：{' -> '.join(ring)}")
            return
        if inv_n.void or inv_n.is_child or inv_n.no in self.edges or inv_n.flushed_tax > ZERO:
            self.errors.append(
                f"{where}：新票 {new} 状态不允许作为红字票（已作废/已参与红冲链）")
            return
        if inv_n.rate != inv_o.rate:
            self.warnings.append(
                f"{where}：跨期税率变更：红字票 {new} 税率 {inv_n.rate} 与原票 {orig} "
                f"税率 {inv_o.rate} 不一致，按红字票实际税额 {inv_n.tax} 冲减")
        remain_tax = abs(inv_o.tax) - inv_o.flushed_tax
        remain_amt = abs(inv_o.amount) - inv_o.flushed_amount
        over = []
        if inv_n.tax - remain_tax > ZERO:
            over.append(f"税额超出差额 {money(inv_n.tax - remain_tax)}"
                        f"（剩余可冲 {remain_tax}，本次 {inv_n.tax}）")
        if inv_n.amount - remain_amt > ZERO:
            over.append(f"金额超出差额 {money(inv_n.amount - remain_amt)}"
                        f"（剩余可冲 {remain_amt}，本次 {inv_n.amount}）")
        if over:
            self.errors.append(
                f"{where}：红冲 {orig} 超出剩余可冲额，已拦截；" + "；".join(over))
            return
        inv_n.is_child = True
        inv_n.sign = -inv_o.sign
        inv_o.flushed_tax += inv_n.tax
        inv_o.flushed_amount += inv_n.amount
        self.edges.setdefault(orig, []).append(new)

    # ---------- 输出 ----------
    def report(self, out=sys.stdout) -> None:
        print("=" * 64, file=out)
        print("发票状态清单", file=out)
        print("=" * 64, file=out)
        header = ("票号", "类别", "金额", "税额", "税率", "状态", "净额贡献")
        rows = []
        for inv in self.invoices.values():
            rows.append((inv.no, inv.category, f"{inv.amount:.2f}", f"{inv.tax:.2f}",
                         f"{inv.rate.normalize()}", inv.status, f"{inv.net_tax:+.2f}"))
        widths = [max(disp_width(x) for x in col) for col in zip(header, *rows)] if rows else \
                 [disp_width(h) for h in header]
        print("  ".join(pad(h, w) for h, w in zip(header, widths)), file=out)
        print("  ".join("-" * w for w in widths), file=out)
        for row in rows:
            print("  ".join(pad(c, w) for c, w in zip(row, widths)), file=out)

        print(file=out)
        print("=" * 64, file=out)
        print("净额清单（按税额累计，作废票计 0）", file=out)
        print("=" * 64, file=out)
        by_rate: dict[Decimal, list[Invoice]] = {}
        for inv in self.invoices.values():
            by_rate.setdefault(inv.rate, []).append(inv)
        total = ZERO
        for rate in sorted(by_rate):
            group = by_rate[rate]
            sub = sum((inv.net_tax for inv in group), ZERO)
            total += sub
            print(f"税率 {rate.normalize()}：{len(group)} 张，净税额 {sub:+.2f}", file=out)
        print("-" * 64, file=out)
        print(f"账面净税额合计：{total:+.2f}", file=out)

        print(file=out)
        print("=" * 64, file=out)
        print(f"警告清单（{len(self.warnings)} 条）", file=out)
        print("=" * 64, file=out)
        for msg in self.warnings or ["（无）"]:
            print(f"  [警告] {msg}", file=out)

        print(file=out)
        print("=" * 64, file=out)
        print(f"错误清单（{len(self.errors)} 条）", file=out)
        print("=" * 64, file=out)
        for msg in self.errors or ["（无）"]:
            print(f"  [错误] {msg}", file=out)


def run_text(text: str) -> Ledger:
    ledger = Ledger()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        where = f"第{lineno}行"
        try:
            if cmd in ("invoice", "发票"):
                if len(args) != 4:
                    raise ValueError("格式：invoice 编号 金额 税额 税率")
                ledger.define(args[0], money(args[1]), money(args[2]), parse_rate(args[3]), where)
            elif cmd in ("redflush", "红冲"):
                if len(args) != 2:
                    raise ValueError("格式：redflush 原票号 新票号")
                ledger.redflush(args[0], args[1], where)
            elif cmd in ("void", "作废"):
                if len(args) != 1:
                    raise ValueError("格式：void 票号")
                ledger.void(args[0], where)
            else:
                ledger.errors.append(f"{where}：未知指令 {parts[0]!r}")
        except (InvalidOperation, ValueError) as exc:
            ledger.errors.append(f"{where}：解析失败：{exc}")
    return ledger


DEMO = """\
# ---------- 发票定义：invoice 编号 金额 税额 税率 ----------
invoice A001 1000.00 130.00 0.13
invoice A002 2000.00 260.00 0.13
invoice A003  500.00  65.00 0.13
invoice A004  800.00  72.00 0.09
invoice R001 1000.00 130.00 0.13
invoice B001  500.00  65.00 0.13
invoice R002  500.00  65.00 0.13
invoice C002 1600.00 208.00 0.13
invoice R003  100.00  13.00 0.13
invoice V001  100.00  13.00 0.13
invoice R009  100.00  13.00 0.13
invoice X001  400.00  52.00 0.13
invoice Y001  400.00  52.00 0.13
invoice Z404   10.00   1.30 13%

# ---------- 红冲链：A001 -> R001 -> B001（红字再被冲，方向回转为蓝） ----------
redflush A001 R001
redflush R001 B001

# ---------- 部分红冲 + 超额拦截 ----------
redflush A002 R002
redflush A002 C002

# ---------- 跨期税率变更（0.09 -> 0.13），警告但放行 ----------
redflush A004 R003

# ---------- 作废 + 作废票再被红冲拦截 ----------
void V001
redflush V001 R009

# ---------- 红冲链成环：X001 -> Y001 -> X001 ----------
redflush X001 Y001
redflush Y001 X001

# ---------- 引用不存在票号 ----------
redflush GHOST Z404
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="红冲与作废级联记账工具（纯标准库）")
    parser.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置样例（红冲链 + 净额）")
    args = parser.parse_args(argv)

    if args.demo:
        print("【样例输入】")
        print(DEMO)
        ledger = run_text(DEMO)
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            ledger = run_text(fh.read())
    else:
        ledger = run_text(sys.stdin.read())

    ledger.report()
    return 1 if ledger.errors else 0


if __name__ == "__main__":
    sys.exit(main())
