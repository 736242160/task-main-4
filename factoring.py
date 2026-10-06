#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factoring.py — 应收账款保理份额登记与回款分配工具（纯 Python 标准库，单文件）

业务规则（自定，全程一致）：
1. 同一笔应收可转让给多家保理商，按登记先后建立份额台账；
   份额 = 该保理商受让金额 / 该笔应收面额。
2. 同一保理商对同一笔应收只允许转让一次，重复转让整笔拦截。
3. 一笔应收的累计转让额不得超过面额；超出时整笔拦截并报告超转额
   （不接受部分转让，保证台账干净）。
4. 还款（PAY）视为债务人到账：按登记顺序逐家瀑布分配（先登记先清偿）；
   全部份额清偿完毕后的余款归供应商（未转让权益 / 超额到账），
   记入该笔应收的“供应商余款”。
5. 追索后供应商补款（RECOURSE）按登记份额比例回补尚未足额回收的保理商；
   已足额回收的家跳过，余量在其余家之间继续按比例分配（多轮直至分完）；
   全部回补完毕仍有剩余的，归入供应商余款。
6. 引用不存在的保理商 / 应收笔 / 供应商，记录错误并跳过该事件。
7. 所有事件按输入顺序逐条处理，状态跨转让流 / 还款流延续。

输入格式（UTF-8 文本，# 起为注释，空白分隔）：
    FACTOR   <保理商ID> <名称>
    SUPPLIER <供应商ID> <名称>
    AR       <供应商ID> <应收笔ID> <面额>
    TRANSFER <保理商ID> <应收笔ID> <金额>
    PAY      <应收笔ID> <金额>
    RECOURSE <应收笔ID> <金额>

用法：
    python3 factoring.py 输入文件
    python3 factoring.py            # 运行内置示例
"""
from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """解析金额并统一保留两位小数。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Allocation:
    """一笔份额登记：某保理商受让某笔应收的一部分。"""
    seq: int            # 全局登记序号（决定回款瀑布顺序）
    factor_id: str
    ar_id: str
    amount: Decimal     # 受让金额
    repaid: Decimal = ZERO  # 已回收金额

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.repaid


@dataclass
class Receivable:
    """应收池中的一笔应收。"""
    ar_id: str
    supplier_id: str
    face: Decimal                       # 面额
    allocations: list = field(default_factory=list)  # 按登记顺序排列
    supplier_remainder: Decimal = ZERO  # 份额清偿完毕后的余款（归供应商）

    @property
    def transferred(self) -> Decimal:
        return sum((a.amount for a in self.allocations), ZERO)

    @property
    def repaid(self) -> Decimal:
        return sum((a.repaid for a in self.allocations), ZERO)


class Ledger:
    """份额台账：处理定义、转让流、还款流，状态跨流延续。"""

    def __init__(self):
        self.factors = {}       # 保理商ID -> 名称
        self.suppliers = {}     # 供应商ID -> 名称
        self.receivables = {}   # 应收笔ID -> Receivable
        self.allocations = []   # 全部份额登记（全局登记顺序）
        self.errors = []        # (行号, 原始行, 错误信息)
        self._pairs = set()     # (保理商ID, 应收笔ID)，用于重复转让拦截
        self._seq = 0

    # ---- 定义段 -------------------------------------------------------

    def add_factor(self, fid, name):
        self.factors[fid] = name

    def add_supplier(self, sid, name):
        self.suppliers[sid] = name

    def add_receivable(self, lineno, raw, sid, ar_id, face):
        if sid not in self.suppliers:
            self._error(lineno, raw, f"未知供应商 {sid}")
            return
        if ar_id in self.receivables:
            self._error(lineno, raw, f"应收笔 {ar_id} 重复定义")
            return
        if face <= 0:
            self._error(lineno, raw, "应收面额必须为正")
            return
        self.receivables[ar_id] = Receivable(ar_id, sid, face)

    # ---- 转让流 -------------------------------------------------------

    def transfer(self, lineno, raw, fid, ar_id, amount):
        if fid not in self.factors:
            self._error(lineno, raw, f"未知保理商 {fid}")
            return
        ar = self.receivables.get(ar_id)
        if ar is None:
            self._error(lineno, raw, f"未知应收笔 {ar_id}")
            return
        if amount <= 0:
            self._error(lineno, raw, "转让金额必须为正")
            return
        if (fid, ar_id) in self._pairs:
            self._error(lineno, raw, f"重复转让拦截: 保理商 {fid} 已受让应收 {ar_id}")
            return
        excess = ar.transferred + amount - ar.face
        if excess > 0:
            self._error(lineno, raw,
                        f"超转拦截: 应收 {ar_id} 面额 {ar.face:,.2f}, "
                        f"已转让 {ar.transferred:,.2f}, 本次 {amount:,.2f}, "
                        f"超转额 {excess:,.2f}")
            return
        self._seq += 1
        alloc = Allocation(self._seq, fid, ar_id, amount)
        ar.allocations.append(alloc)
        self.allocations.append(alloc)
        self._pairs.add((fid, ar_id))

    # ---- 还款流 -------------------------------------------------------

    def repay(self, lineno, raw, ar_id, amount):
        """债务人到账：按登记顺序逐家瀑布分配，余款归供应商。"""
        ar = self._check_ar(lineno, raw, ar_id, amount)
        if ar is None:
            return
        remaining = amount
        for alloc in ar.allocations:            # 列表即登记顺序
            if remaining <= 0:
                break
            pay = min(remaining, alloc.outstanding)
            alloc.repaid += pay
            remaining -= pay
        if remaining > 0:                       # 份额清偿完毕后的余款
            ar.supplier_remainder += remaining

    def recourse(self, lineno, raw, ar_id, amount):
        """追索后供应商补款：按登记份额比例回补未足额回收的保理商。"""
        ar = self._check_ar(lineno, raw, ar_id, amount)
        if ar is None:
            return
        remaining = amount
        while remaining > 0:
            eligible = [a for a in ar.allocations if a.outstanding > 0]
            if not eligible:
                break
            total_share = sum((a.amount for a in eligible), ZERO)
            distributed = ZERO
            for i, alloc in enumerate(eligible):
                if i == len(eligible) - 1:
                    pay = remaining - distributed   # 末位兜底，吸收舍入尾差
                else:
                    pay = (remaining * alloc.amount / total_share).quantize(
                        CENT, rounding=ROUND_HALF_UP)
                pay = min(pay, alloc.outstanding)   # 回补不超过未收余额
                alloc.repaid += pay
                distributed += pay
            if distributed <= 0:
                break
            remaining -= distributed
        if remaining > 0:                           # 全部回补完毕仍有剩余
            ar.supplier_remainder += remaining

    # ---- 内部 ---------------------------------------------------------

    def _check_ar(self, lineno, raw, ar_id, amount):
        ar = self.receivables.get(ar_id)
        if ar is None:
            self._error(lineno, raw, f"未知应收笔 {ar_id}")
            return None
        if amount <= 0:
            self._error(lineno, raw, "金额必须为正")
            return None
        return ar

    def _error(self, lineno, raw, message):
        self.errors.append((lineno, raw.strip(), message))


# ---- 输入解析 -----------------------------------------------------------

def run_lines(ledger, lines):
    """按行处理事件流，状态在 Ledger 中跨流延续。"""
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        try:
            if cmd == "FACTOR" and len(parts) >= 3:
                ledger.add_factor(parts[1], " ".join(parts[2:]))
            elif cmd == "SUPPLIER" and len(parts) >= 3:
                ledger.add_supplier(parts[1], " ".join(parts[2:]))
            elif cmd == "AR" and len(parts) == 4:
                ledger.add_receivable(lineno, raw, parts[1], parts[2], money(parts[3]))
            elif cmd == "TRANSFER" and len(parts) == 4:
                ledger.transfer(lineno, raw, parts[1], parts[2], money(parts[3]))
            elif cmd == "PAY" and len(parts) == 3:
                ledger.repay(lineno, raw, parts[1], money(parts[2]))
            elif cmd == "RECOURSE" and len(parts) == 3:
                ledger.recourse(lineno, raw, parts[1], money(parts[2]))
            else:
                ledger._error(lineno, raw, f"无法识别的指令或参数个数错误: {cmd}")
        except InvalidOperation:
            ledger._error(lineno, raw, "金额格式非法")


# ---- 输出报表 -----------------------------------------------------------

def _width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, n):
    return s + " " * max(0, n - _width(s))


def render_table(headers, rows):
    all_rows = [headers] + rows
    widths = [max(_width(str(r[i])) for r in all_rows) for i in range(len(headers))]
    lines = []
    for idx, r in enumerate(all_rows):
        lines.append("  " + "  ".join(_pad(str(c), widths[i]) for i, c in enumerate(r)))
        if idx == 0:
            lines.append("  " + "  ".join("-" * w for w in widths))
    return lines


def report(ledger):
    out = ["=" * 76, "份额分配表", "=" * 76]
    headers = ["应收笔", "供应商", "保理商", "受让金额", "份额%(占面额)", "已回收", "未回收"]
    rows = []
    for ar in ledger.receivables.values():
        if not ar.allocations:
            rows.append([ar.ar_id, ar.supplier_id, "(未转让)", "-", "-", "-", "-"])
        for a in ar.allocations:
            share = (a.amount / ar.face * 100).quantize(CENT, rounding=ROUND_HALF_UP)
            rows.append([ar.ar_id, ar.supplier_id,
                         f"{a.factor_id} {ledger.factors.get(a.factor_id, '')}",
                         f"{a.amount:,.2f}", f"{share}%",
                         f"{a.repaid:,.2f}", f"{a.outstanding:,.2f}"])
        if ar.supplier_remainder > 0:
            rows.append([ar.ar_id, ar.supplier_id, "→ 供应商余款", "-", "-",
                         f"{ar.supplier_remainder:,.2f}", "-"])
    out.extend(render_table(headers, rows))

    out += ["", "=" * 76, "保理商汇总", "=" * 76]
    rows = []
    for fid, name in ledger.factors.items():
        allocs = [a for a in ledger.allocations if a.factor_id == fid]
        tot = sum((a.amount for a in allocs), ZERO)
        rep = sum((a.repaid for a in allocs), ZERO)
        rows.append([f"{fid} {name}", f"{tot:,.2f}", f"{rep:,.2f}", f"{tot - rep:,.2f}"])
    out.extend(render_table(["保理商", "累计受让", "累计回收", "未回收"], rows))

    out += ["", "=" * 76, "错误清单", "=" * 76]
    if not ledger.errors:
        out.append("  (无)")
    for lineno, raw, msg in ledger.errors:
        out.append(f"  第{lineno}行 [{raw}]")
        out.append(f"    -> {msg}")
    return "\n".join(out)


# ---- 内置示例 -----------------------------------------------------------

DEMO = """\
# ---- 定义段：保理商 / 供应商 / 应收池 ----
FACTOR   F1 华信保理
FACTOR   F2 中商保理
FACTOR   F3 联合保理
SUPPLIER S1 宏达电子
AR       S1 R001 100000
AR       S1 R002 60000

# ---- 转让流 ----
TRANSFER F1 R001 50000      # 登记成功（序号1）
TRANSFER F2 R001 30000      # 同一应收第二家受让（序号2）
TRANSFER F2 R002 60000      # 全额受让（序号3）
TRANSFER F3 R001 30000      # 超转：已转80000+30000>100000，超转10000 → 拦截
TRANSFER F1 R001 10000      # 重复转让 → 拦截
TRANSFER F9 R001 1000       # 未知保理商 → 报告
TRANSFER F1 R999 1000       # 未知应收笔 → 报告

# ---- 还款流（状态跨流延续） ----
PAY      R001 70000         # 瀑布分配：F1 +50000(清偿)，F2 +20000(尚欠10000)
RECOURSE R001 5000          # 追索补款：F1已足额跳过，全部回补 F2
PAY      R002 65000         # F2 +60000(清偿)，余款 5000 归供应商
PAY      R999 1000          # 未知应收笔 → 报告
"""


def main(argv):
    ledger = Ledger()
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            run_lines(ledger, fh)
    else:
        print("(未指定输入文件，运行内置示例)\n")
        run_lines(ledger, DEMO.splitlines())
    print(report(ledger))


if __name__ == "__main__":
    main(sys.argv)
