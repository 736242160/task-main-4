#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lc_check.py — 信用证更正单/支取校验工具（纯 Python 标准库，单文件）

用法:
    python3 lc_check.py             # 运行内置样例（版本链 + 支取流，可直接验证）
    python3 lc_check.py input.json  # 校验自定义输入

输入 JSON 结构:
{
  "lcs": [
    {"lc_no": "LC001", "amount": 100000, "issue_date": "2026-01-10",
     "beneficiary": "ABC Corp", "required_docs": ["invoice", "bill_of_lading"]}
  ],
  "amendments": [
    {"lc_no": "LC001", "version": 2, "seq": 2,
     "changes": {"amount": 50000, "beneficiary": "...", "required_docs": [...]}}
  ],
  "drawings": [
    {"lc_no": "LC001", "amount": 40000, "date": "2026-03-01", "seq": 1,
     "documents": [
       {"type": "invoice", "title": "ABC Corp",
        "quantity": 200, "unit_price": 200, "amount": 40000},
       {"type": "bill_of_lading", "title": "ABC Corp", "shipment_date": "2026-02-20"}
     ]}
  ]
}

说明:
- seq 用于把更正单流与支取流合并成一条时间线（跨流状态延续）；缺省时先更正后支取。
- 金额用 Decimal 精确计算；日期为 ISO 格式 YYYY-MM-DD。
"""
import json
import sys
from datetime import date
from decimal import Decimal

TOL = Decimal("0.005")  # 金额勾稽容差（半分）


def dec(x):
    return Decimal(str(x))


def money(x):
    return f"{x:.2f}"


class LCState:
    """单张信用证的当前状态（随更正单演进）。"""

    def __init__(self, d):
        self.lc_no = d["lc_no"]
        self.amount = dec(d["amount"])
        self.issue_date = date.fromisoformat(d["issue_date"])
        self.beneficiary = d["beneficiary"]
        self.required_docs = list(d.get("required_docs", []))
        self.version = 1
        self.drawn_total = Decimal("0")  # 累计已支取

    @property
    def balance(self):
        return self.amount - self.drawn_total


class Engine:
    def __init__(self):
        self.lcs = {}
        self.errors = []    # 全局错误清单 [(位置, 信息)]
        self.results = []   # 每笔支取结论

    # ---------- 载入 ----------
    def load_lcs(self, defs):
        for d in defs:
            if d["lc_no"] in self.lcs:
                self.errors.append((f"开证 {d['lc_no']}", "证号重复定义"))
                continue
            self.lcs[d["lc_no"]] = LCState(d)

    # ---------- 更正单 ----------
    def apply_amendment(self, a):
        lc_no, v = a["lc_no"], a["version"]
        where = f"更正单 {lc_no} v{v}"
        lc = self.lcs.get(lc_no)
        if lc is None:
            self.errors.append((where, f"引用不存在的证号 {lc_no}"))
            return
        expected = lc.version + 1
        if v <= lc.version:
            self.errors.append((where, f"版本回退：当前 v{lc.version}，收到 v{v}，该更正单被拒绝"))
            return
        if v != expected:
            self.errors.append((where, f"版本跳号：应为 v{expected}，收到 v{v}（仍按收到的内容生效）"))
        ch = a.get("changes", {})
        if "amount" in ch:
            lc.amount = dec(ch["amount"])
        if "beneficiary" in ch:
            lc.beneficiary = ch["beneficiary"]
        if "required_docs" in ch:
            lc.required_docs = list(ch["required_docs"])
        if "issue_date" in ch:
            lc.issue_date = date.fromisoformat(ch["issue_date"])
        lc.version = v
        # 版本变更后按新版本重算已支取
        if lc.drawn_total > lc.amount + TOL:
            over = lc.drawn_total - lc.amount
            self.errors.append((where,
                f"版本变更后重算：已支取累计 {money(lc.drawn_total)} 超出新额度 "
                f"{money(lc.amount)}，超出 {money(over)}"))

    # ---------- 支取 ----------
    def process_drawing(self, d):
        lc_no = d["lc_no"]
        amount = dec(d["amount"])
        draw_date = date.fromisoformat(d["date"])
        docs = d.get("documents", [])
        where = f"支取 {lc_no} {d['date']} 金额 {money(amount)}"
        errs = []

        lc = self.lcs.get(lc_no)
        if lc is None:
            errs.append(f"引用不存在的证号 {lc_no}")
            self._record(where, "拒付", errs)
            return

        # 1) 按最新版本审核单据种类
        present = {doc.get("type") for doc in docs}
        for req in lc.required_docs:
            if req not in present:
                errs.append(f"缺少最新版本(v{lc.version})要求的单据: {req}")

        # 2) 发票金额与数量单价勾稽（定位单据对）
        invoices = [doc for doc in docs if doc.get("type") == "invoice"]
        inv_total = Decimal("0")
        for i, inv in enumerate(invoices, 1):
            q, p, amt = inv.get("quantity"), inv.get("unit_price"), inv.get("amount")
            if q is not None and p is not None and amt is not None:
                expect = dec(q) * dec(p)
                inv_total += dec(amt)
                if abs(expect - dec(amt)) > TOL:
                    errs.append(
                        f"单据对勾稽不符: 发票#{i} 金额 {money(dec(amt))} != "
                        f"数量 {q} × 单价 {money(dec(p))} = {money(expect)}")
        if invoices and abs(inv_total - amount) > TOL:
            errs.append(
                f"单据对勾稽不符: 支取金额 {money(amount)} != 发票合计 {money(inv_total)}")

        # 3) 抬头与受益人核对
        for i, doc in enumerate(docs, 1):
            title = doc.get("title")
            if title is not None and title != lc.beneficiary:
                errs.append(
                    f"单据#{i}({doc.get('type')}) 抬头 '{title}' 与受益人 "
                    f"'{lc.beneficiary}' 不符")

        # 4) 日期链：开证日 <= 装运日 <= 支取日
        for i, doc in enumerate(docs, 1):
            sd = doc.get("shipment_date")
            if sd is None:
                continue
            sd = date.fromisoformat(sd)
            if sd < lc.issue_date:
                errs.append(
                    f"单据#{i}({doc.get('type')}) 装运日 {sd} 早于开证日 {lc.issue_date}")
            if sd > draw_date:
                errs.append(
                    f"单据#{i}({doc.get('type')}) 装运日 {sd} 晚于支取日 {draw_date}")

        # 5) 余额校验（累计扣减）
        if amount > lc.balance + TOL:
            errs.append(
                f"余额不足：申请 {money(amount)}，当前余额 {money(lc.balance)}，"
                f"超出 {money(amount - lc.balance)}")

        if errs:
            self._record(where, "拒付", errs, lc)
        else:
            lc.drawn_total += amount
            self._record(where, "承付", [], lc)

    def _record(self, where, status, errs, lc=None):
        for e in errs:
            self.errors.append((where, e))
        self.results.append({
            "where": where, "status": status, "errors": errs,
            "balance": None if lc is None else lc.balance,
            "version": None if lc is None else lc.version,
        })

    # ---------- 主流程：跨流状态延续 ----------
    def run(self, payload):
        self.load_lcs(payload.get("lcs", []))
        events = []
        for a in payload.get("amendments", []):
            events.append((a.get("seq"), "amendment", a))
        for d in payload.get("drawings", []):
            events.append((d.get("seq"), "drawing", d))
        # 有 seq 按 seq 归并排序；无 seq 保持先更正后支取
        events.sort(key=lambda e: (e[0] is None, e[0] if e[0] is not None else 0))
        for _, kind, item in events:
            if kind == "amendment":
                self.apply_amendment(item)
            else:
                self.process_drawing(item)
        return self

    def report(self, out=sys.stdout):
        p = lambda s="": print(s, file=out)
        p("=" * 60)
        p("支取结论")
        p("=" * 60)
        for r in self.results:
            bal = "N/A" if r["balance"] is None else money(r["balance"])
            ver = "N/A" if r["version"] is None else f"v{r['version']}"
            p(f"[{r['status']}] {r['where']}  (版本 {ver}, 支取后余额 {bal})")
            for e in r["errors"]:
                p(f"    - {e}")
        p()
        p("=" * 60)
        p(f"错误清单（共 {len(self.errors)} 条）")
        p("=" * 60)
        for i, (where, e) in enumerate(self.errors, 1):
            p(f"{i:2d}. [{where}] {e}")
        p()
        p("=" * 60)
        p("期末各证状态")
        p("=" * 60)
        for lc in self.lcs.values():
            p(f"{lc.lc_no}: 版本 v{lc.version}, 额度 {money(lc.amount)}, "
              f"已支取 {money(lc.drawn_total)}, 余额 {money(lc.balance)}")


# ---------- 内置样例：版本链 + 支取流 ----------
SAMPLE = {
    "lcs": [
        {"lc_no": "LC001", "amount": 100000, "issue_date": "2026-01-10",
         "beneficiary": "Shanghai Textile Co.",
         "required_docs": ["invoice", "bill_of_lading", "packing_list"]},
        {"lc_no": "LC002", "amount": 5000, "issue_date": "2026-02-01",
         "beneficiary": "Delta Foods",
         "required_docs": ["invoice", "bill_of_lading"]},
    ],
    "amendments": [
        # v2：LC001 额度 100000 -> 50000（此时已支取 40000，重算后余额 10000）
        {"lc_no": "LC001", "version": 2, "seq": 2, "changes": {"amount": 50000}},
        # v4：跳号（应为 v3），额度 50000 -> 30000，已支取 40000 超出新额度
        {"lc_no": "LC001", "version": 4, "seq": 5, "changes": {"amount": 30000}},
        # v2：版本回退，应被拒绝
        {"lc_no": "LC001", "version": 2, "seq": 6, "changes": {"amount": 999999}},
        # 引用不存在的证号
        {"lc_no": "LC999", "version": 2, "seq": 7, "changes": {"amount": 1}},
    ],
    "drawings": [
        # D1：单据齐全、勾稽一致、日期链合法、抬头正确 -> 承付 40000
        {"lc_no": "LC001", "amount": 40000, "date": "2026-03-01", "seq": 1,
         "documents": [
             {"type": "invoice", "title": "Shanghai Textile Co.",
              "quantity": 200, "unit_price": 200, "amount": 40000},
             {"type": "bill_of_lading", "title": "Shanghai Textile Co.",
              "shipment_date": "2026-02-20"},
             {"type": "packing_list", "title": "Shanghai Textile Co."}]},
        # D2：v2 后余额仅 10000，申请 15000 -> 余额不足，超出 5000
        {"lc_no": "LC001", "amount": 15000, "date": "2026-03-05", "seq": 3,
         "documents": [
             {"type": "invoice", "title": "Shanghai Textile Co.",
              "quantity": 75, "unit_price": 200, "amount": 15000},
             {"type": "bill_of_lading", "title": "Shanghai Textile Co.",
              "shipment_date": "2026-03-01"},
             {"type": "packing_list", "title": "Shanghai Textile Co."}]},
        # D3：发票勾稽不符 + 缺装箱单 + 装运早于开证 + 抬头不符 -> 拒付
        {"lc_no": "LC001", "amount": 8000, "date": "2026-03-08", "seq": 4,
         "documents": [
             {"type": "invoice", "title": "Wrong Title Co.",
              "quantity": 100, "unit_price": 80, "amount": 8100},
             {"type": "bill_of_lading", "title": "Shanghai Textile Co.",
              "shipment_date": "2026-01-05"}]},
        # D4：v4 后额度 30000 < 已支取 40000，余额为负 -> 拒付
        {"lc_no": "LC001", "amount": 5000, "date": "2026-03-12", "seq": 8,
         "documents": [
             {"type": "invoice", "title": "Shanghai Textile Co.",
              "quantity": 25, "unit_price": 200, "amount": 5000},
             {"type": "bill_of_lading", "title": "Shanghai Textile Co.",
              "shipment_date": "2026-03-10"},
             {"type": "packing_list", "title": "Shanghai Textile Co."}]},
        # D5：引用不存在的证号
        {"lc_no": "LC999", "amount": 100, "date": "2026-03-12", "seq": 9,
         "documents": []},
        # D6：装运日晚于支取日 -> 拒付
        {"lc_no": "LC002", "amount": 5000, "date": "2026-03-05", "seq": 10,
         "documents": [
             {"type": "invoice", "title": "Delta Foods",
              "quantity": 50, "unit_price": 100, "amount": 5000},
             {"type": "bill_of_lading", "title": "Delta Foods",
              "shipment_date": "2026-03-10"}]},
        # D7：LC002 正常支取 -> 承付
        {"lc_no": "LC002", "amount": 5000, "date": "2026-03-15", "seq": 11,
         "documents": [
             {"type": "invoice", "title": "Delta Foods",
              "quantity": 50, "unit_price": 100, "amount": 5000},
             {"type": "bill_of_lading", "title": "Delta Foods",
              "shipment_date": "2026-03-10"}]},
    ],
}


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            payload = json.load(f)
    else:
        print("（未提供输入文件，运行内置样例；用法: python3 lc_check.py input.json）\n")
        payload = SAMPLE
    Engine().run(payload).report()


if __name__ == "__main__":
    main()
