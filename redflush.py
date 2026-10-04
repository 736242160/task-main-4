#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
redflush.py — 红冲 / 作废级联记账工具（纯 Python 标准库，单文件）

用法:
    python3 redflush.py 输入.json      # 从文件读
    cat 输入.json | python3 redflush.py  # 从 stdin 读
    python3 redflush.py --demo         # 运行内置红冲链 + 净额样例

输入 JSON:
{
  "invoices": [
    {"id": "A001", "amount": "1000.00", "tax": "130.00", "rate": "0.13"}
  ],
  "flows": [
    {"type": "red_flush", "orig": "A001", "new": "R001"},
    {"type": "void", "id": "A009"}
  ]
}
也兼容简写 "red_flushes": [{"orig","new"}] 与 "voids": ["id"]（先红冲后作废；
需要交错顺序请用 flows）。

自定记账规则:
 1. 红冲流 orig -> new：new 必须是已定义的红字票（金额为负），冲减额 = |new.amount|。
 2. 冲减额超过 orig 剩余可冲额 -> 拦截，报告差额；orig 全额被冲为「已红冲」，
    部分被冲为「部分红冲」；new 状态为「已开具」。
 3. 红字票可再被红冲，由此形成红冲链；净额按全链路重算。
 4. 红冲链成环（a 冲 b、b 冲 a）-> 环上所有红冲流拦截，报告环上票号。
 5. 作废仅允许「已开具」（无红冲记录）的票；已红冲/部分红冲/已作废均拦截；
    作废票再被红冲 -> 拦截。
 6. new.rate != orig.rate 视为跨期税率变更：不拦截，按新票实际税额入账，
    记警示并报告「税率差调整额」。
 7. 净额 = 所有未作废票税额（带符号，红字为负）的代数和；作废票贡献为 0。
 8. 引用不存在的票号 -> 报告错误，该条流不生效。
"""

import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")

STAT_ISSUED = "已开具"
STAT_PARTIAL = "部分红冲"
STAT_FLUSHED = "已红冲"
STAT_VOIDED = "已作废"


def dec(value):
    """宽松地把 JSON 数值转成 Decimal，失败返回 None。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def money(d):
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


class Invoice:
    __slots__ = ("id", "amount", "tax", "rate", "status", "flushed")

    def __init__(self, inv_id, amount, tax, rate):
        self.id = inv_id
        self.amount = amount      # 带符号，红字票为负
        self.tax = tax            # 带符号税额
        self.rate = rate
        self.status = STAT_ISSUED
        self.flushed = ZERO       # 已被红冲的累计金额（绝对值口径）

    @property
    def remaining(self):
        """剩余可冲额。"""
        return abs(self.amount) - self.flushed

    @property
    def is_red(self):
        return self.amount < ZERO


def find_cycles(edges):
    """Tarjan SCC（迭代版），返回成环节点列表；自环也算环。"""
    graph = {}
    for a, b in edges:
        graph.setdefault(a, []).append(b)
        graph.setdefault(b, [])
    index, low, on_stack, stack, sccs = {}, {}, set(), [], []
    counter = 0
    for root in graph:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(graph[root]))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(graph[nxt])))
                    descended = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)
    cycles = []
    for comp in sccs:
        if len(comp) > 1 or comp[0] in graph[comp[0]]:
            cycles.append(sorted(comp))
    return cycles


class Ledger:
    def __init__(self):
        self.invoices = {}
        self.errors = []
        self.warnings = []
        self.used_as_new = set()   # 一张红字票只能用于一条红冲流
        self.blocked_edges = set() # 成环被拦截的 (orig, new)

    def err(self, where, message, **extra):
        e = {"where": where, "message": message}
        e.update(extra)
        self.errors.append(e)

    def warn(self, where, message, **extra):
        w = {"where": where, "message": message}
        w.update(extra)
        self.warnings.append(w)

    # ---------- 发票定义 ----------
    def load_invoices(self, defs):
        for i, d in enumerate(defs):
            where = "invoices[%d]" % i
            inv_id = d.get("id")
            amount = dec(d.get("amount"))
            tax = dec(d.get("tax"))
            rate = dec(d.get("rate"))
            if not inv_id:
                self.err(where, "缺少票号")
                continue
            inv_id = str(inv_id)
            if inv_id in self.invoices:
                self.err(where, "票号重复定义: %s（保留首条）" % inv_id)
                continue
            if amount is None or tax is None or rate is None:
                self.err(where, "票 %s 金额/税额/税率无法解析" % inv_id)
                continue
            inv = Invoice(inv_id, amount, tax, rate)
            expected = money(amount * rate)
            if abs(expected - tax) > CENT:
                self.warn(where, "票 %s 税额 %s 与 金额×税率=%s 不符，以申报税额入账"
                          % (inv_id, tax, expected))
            self.invoices[inv_id] = inv

    # ---------- 红冲 ----------
    def red_flush(self, orig_id, new_id, where):
        inv_o = self.invoices.get(orig_id)
        inv_n = self.invoices.get(new_id)
        if inv_o is None:
            self.err(where, "红冲引用的原票号不存在: %s" % orig_id)
            return
        if inv_n is None:
            self.err(where, "红冲引用的新票号不存在: %s" % new_id)
            return
        if (orig_id, new_id) in self.blocked_edges:
            return  # 成环已统一报错，此处静默跳过
        if inv_o.status == STAT_VOIDED:
            self.err(where, "拦截：作废票不可红冲: %s" % orig_id)
            return
        if inv_o.status == STAT_FLUSHED:
            self.err(where, "拦截：原票 %s 已全额红冲，剩余可冲额为 0" % orig_id)
            return
        if inv_n.status == STAT_VOIDED:
            self.err(where, "拦截：新票 %s 已作废，不可用于红冲" % new_id)
            return
        if new_id in self.used_as_new:
            self.err(where, "拦截：红字票 %s 已用于其他红冲流" % new_id)
            return
        if not inv_n.is_red:
            self.err(where, "拦截：新票 %s 金额为正（%s），不是红字票"
                     % (new_id, inv_n.amount))
            return
        flush_amount = -inv_n.amount
        if flush_amount > inv_o.remaining:
            diff = flush_amount - inv_o.remaining
            self.err(where,
                     "拦截：红冲金额 %s 超过原票 %s 剩余可冲额 %s，差额 %s"
                     % (flush_amount, orig_id, inv_o.remaining, diff),
                     overflow=str(diff))
            return
        if inv_n.rate != inv_o.rate:
            expected_tax = money(flush_amount * inv_o.rate)
            actual_tax = abs(inv_n.tax)
            adjust = actual_tax - expected_tax
            self.warn(where,
                      "跨期税率变更：原票 %s 税率 %s，红字票 %s 税率 %s；"
                      "按原税率应冲税额 %s，实际冲减 %s，税率差调整额 %s"
                      % (orig_id, inv_o.rate, new_id, inv_n.rate,
                         expected_tax, actual_tax, adjust),
                      tax_rate_adjustment=str(adjust))
        inv_o.flushed += flush_amount
        inv_o.status = STAT_FLUSHED if inv_o.remaining == ZERO else STAT_PARTIAL
        self.used_as_new.add(new_id)

    # ---------- 作废 ----------
    def void(self, inv_id, where):
        inv = self.invoices.get(inv_id)
        if inv is None:
            self.err(where, "作废引用的票号不存在: %s" % inv_id)
            return
        if inv.status == STAT_VOIDED:
            self.err(where, "拦截：票 %s 重复作废" % inv_id)
            return
        if inv.status != STAT_ISSUED:
            self.err(where, "拦截：仅「已开具」可作废，票 %s 当前状态「%s」"
                     % (inv_id, inv.status))
            return
        inv.status = STAT_VOIDED

    # ---------- 汇总 ----------
    def report(self):
        rows = []
        net_amount, net_tax = ZERO, ZERO
        for inv_id in sorted(self.invoices):
            inv = self.invoices[inv_id]
            counted = inv.status != STAT_VOIDED
            c_amount = inv.amount if counted else ZERO
            c_tax = inv.tax if counted else ZERO
            net_amount += c_amount
            net_tax += c_tax
            rows.append({
                "id": inv.id,
                "kind": "红字" if inv.is_red else "蓝字",
                "amount": inv.amount,
                "tax": inv.tax,
                "rate": inv.rate,
                "status": inv.status,
                "flushed_amount": inv.flushed,
                "remaining_flushable": inv.remaining,
                "counted_in_net": counted,
                "net_tax_contribution": c_tax,
            })
        return {
            "invoices": rows,
            "net": {"amount": net_amount, "tax": net_tax},
            "errors": self.errors,
            "warnings": self.warnings,
        }


def normalize_flows(data, pre_errors):
    """统一成有序流列表：(kind, id1, id2, where)。跨流状态按序延续。"""
    flows = []
    for i, f in enumerate(data.get("flows", [])):
        where = "flows[%d]" % i
        t = f.get("type")
        if t == "red_flush" and f.get("orig") and f.get("new"):
            flows.append(("red_flush", str(f["orig"]), str(f["new"]), where))
        elif t == "void" and f.get("id"):
            flows.append(("void", str(f["id"]), None, where))
        else:
            pre_errors.append({"where": where, "message": "流定义非法: %r" % (f,)})
    for i, f in enumerate(data.get("red_flushes", [])):
        where = "red_flushes[%d]" % i
        if f.get("orig") and f.get("new"):
            flows.append(("red_flush", str(f["orig"]), str(f["new"]), where))
        else:
            pre_errors.append({"where": where, "message": "红冲流定义非法: %r" % (f,)})
    for i, inv_id in enumerate(data.get("voids", [])):
        flows.append(("void", str(inv_id), None, "voids[%d]" % i))
    return flows


def run(data):
    ledger = Ledger()
    pre_errors = []
    ledger.load_invoices(data.get("invoices", []))
    flows = normalize_flows(data, pre_errors)

    # 1) 先查环：环上的红冲流整体拦截
    edges = [(a, b) for kind, a, b, _ in flows
             if kind == "red_flush" and a in ledger.invoices and b in ledger.invoices]
    for cyc in find_cycles(edges):
        ledger.err("red_flush",
                   "红冲链成环，环上红冲流全部拦截: %s"
                   % " -> ".join(cyc + [cyc[0]]),
                   cycle=cyc)
        cset = set(cyc)
        for a, b in edges:
            if a in cset and b in cset:
                ledger.blocked_edges.add((a, b))

    # 2) 按序执行各条流，状态跨流延续
    for kind, a, b, where in flows:
        if kind == "red_flush":
            ledger.red_flush(a, b, where)
        else:
            ledger.void(a, where)

    ledger.errors = pre_errors + ledger.errors
    return ledger.report()


# ---------------- 内置样例：红冲链 + 净额 ----------------
DEMO = {
    "invoices": [
        {"id": "A001", "amount": "1000.00", "tax": "130.00", "rate": "0.13"},
        {"id": "R001", "amount": "-400.00", "tax": "-52.00", "rate": "0.13"},
        {"id": "R002", "amount": "-600.00", "tax": "-78.00", "rate": "0.13"},
        {"id": "A002", "amount": "2000.00", "tax": "260.00", "rate": "0.13"},
        {"id": "R101", "amount": "-2000.00", "tax": "-260.00", "rate": "0.13"},
        {"id": "R102", "amount": "-2000.00", "tax": "-260.00", "rate": "0.13"},
        {"id": "B001", "amount": "-100.00", "tax": "-13.00", "rate": "0.13"},
        {"id": "B002", "amount": "-100.00", "tax": "-13.00", "rate": "0.13"},
        {"id": "A004", "amount": "500.00", "tax": "65.00", "rate": "0.13"},
        {"id": "R004", "amount": "-600.00", "tax": "-78.00", "rate": "0.13"},
        {"id": "A005", "amount": "1000.00", "tax": "130.00", "rate": "0.13"},
        {"id": "R005", "amount": "-1000.00", "tax": "-90.00", "rate": "0.09"},
        {"id": "A009", "amount": "100.00", "tax": "13.00", "rate": "0.13"},
        {"id": "R009", "amount": "-100.00", "tax": "-13.00", "rate": "0.13"}
    ],
    "flows": [
        {"type": "red_flush", "orig": "A001", "new": "R001"},
        {"type": "red_flush", "orig": "A001", "new": "R002"},
        {"type": "red_flush", "orig": "A002", "new": "R101"},
        {"type": "red_flush", "orig": "R101", "new": "R102"},
        {"type": "red_flush", "orig": "B001", "new": "B002"},
        {"type": "red_flush", "orig": "B002", "new": "B001"},
        {"type": "void", "id": "A009"},
        {"type": "red_flush", "orig": "A009", "new": "R009"},
        {"type": "red_flush", "orig": "A004", "new": "R004"},
        {"type": "red_flush", "orig": "A005", "new": "R005"},
        {"type": "red_flush", "orig": "NOPE", "new": "R001"},
        {"type": "void", "id": "GHOST"}
    ]
}


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        data = DEMO
    elif len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as fp:
            data = json.load(fp)
    else:
        data = json.load(sys.stdin)
    report = run(data)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
