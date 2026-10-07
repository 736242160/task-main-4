#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信用证改单/支取审核工具（纯 Python 标准库，单文件）

用法:
    python3 lc_audit.py            # 运行内置样例（版本链 + 支取流），可直接验证
    python3 lc_audit.py case.json  # 审核自定义 JSON 输入

JSON 输入格式:
{
  "lcs":        [ {"lc_no","amount","issue_date","beneficiary","applicant","required_docs"} ],
  "amendments": [ {"lc_no","version","changes":{"amount"?,"beneficiary"?,"required_docs"?}} ],
  "drawings":   [ {"lc_no","amount","date","docs":[{"type","issuer"?,"date"?,"amount"?,"quantity"?,"unit_price"?}]} ]
}

审核规则:
  1. 单据按最新版本的要求审核
  2. 更正单版本号必须递增 1，跳号/回退要报告（回退拒绝生效，跳号报告后生效）
  3. 多次支取累计扣减余额，余额不足拒付并报告超出额
  4. 发票金额 == 数量 x 单价，不一致定位到具体单据
  5. 日期链: 开证日 <= 装运日(B/L) <= 支取日
  6. 版本变更后，已支取金额按新版本额度重算余额
  7. 发票抬头(issuer) 与受益人不符要报告
  8. 引用不存在的证号要报告
  9. 状态跨流延续（同一引擎顺序处理）
"""
import json
import sys
from datetime import date


def parse_date(s, ctx, errors):
    try:
        return date.fromisoformat(s)
    except Exception:
        errors.append("[日期格式错误] %s: %r 不是合法 ISO 日期" % (ctx, s))
        return None


class LCState:
    """单张信用证的跨流延续状态"""
    def __init__(self, defn):
        self.lc_no = defn["lc_no"]
        self.amount = float(defn["amount"])
        self.issue_date = defn["issue_date"]
        self.beneficiary = defn["beneficiary"]
        self.applicant = defn.get("applicant", "")
        self.required_docs = list(defn.get("required_docs", []))
        self.version = 1          # 开证即版本 1
        self.drawn_total = 0.0    # 累计已支取

    @property
    def balance(self):
        # 版本变更后按最新额度重算余额
        return self.amount - self.drawn_total


class Engine:
    def __init__(self):
        self.lcs = {}
        self.errors = []     # 全局错误清单
        self.results = []    # 每笔支取结论

    # ---------- 开证 ----------
    def define_lc(self, defn):
        lc_no = defn.get("lc_no")
        if not lc_no:
            self.errors.append("[开证错误] 缺少证号: %r" % (defn,))
            return
        if lc_no in self.lcs:
            self.errors.append("[开证错误] 证号 %s 重复开证" % lc_no)
            return
        errs = []
        if parse_date(defn.get("issue_date", ""), "开证 %s issue_date" % lc_no, errs) is None:
            self.errors.extend(errs)
            return
        self.lcs[lc_no] = LCState(defn)

    # ---------- 更正单 ----------
    def apply_amendment(self, amd):
        lc_no = amd.get("lc_no")
        ver = amd.get("version")
        lc = self.lcs.get(lc_no)
        if lc is None:
            self.errors.append("[更正单错误] 更正单引用不存在的证号 %s (版本 %s)" % (lc_no, ver))
            return
        if not isinstance(ver, int):
            self.errors.append("[更正单错误] 证号 %s 版本号非法: %r" % (lc_no, ver))
            return
        expected = lc.version + 1
        if ver <= lc.version:
            self.errors.append(
                "[版本回退] 证号 %s 更正单版本 %d 不大于当前版本 %d，该更正单被拒绝"
                % (lc_no, ver, lc.version))
            return
        if ver > expected:
            self.errors.append(
                "[版本跳号] 证号 %s 期望版本 %d，收到版本 %d（跳过 %d 个版本），仍按收到内容生效"
                % (lc_no, expected, ver, ver - expected))
        changes = amd.get("changes", {})
        old_amount = lc.amount
        if "amount" in changes:
            lc.amount = float(changes["amount"])
        if "beneficiary" in changes:
            lc.beneficiary = changes["beneficiary"]
        if "required_docs" in changes:
            lc.required_docs = list(changes["required_docs"])
        lc.version = ver
        if "amount" in changes:
            # 版本变更后按新版本重算余额（已支取金额不变，额度变化）
            self.results.append(
                "更正生效: 证号 %s 版本 %d，金额 %.2f -> %.2f，已支取 %.2f，重算余额 %.2f"
                % (lc_no, ver, old_amount, lc.amount, lc.drawn_total, lc.balance))
            if lc.balance < 0:
                self.errors.append(
                    "[余额透支] 证号 %s 版本 %d 将额度下调至 %.2f，低于已支取 %.2f，透支 %.2f"
                    % (lc_no, ver, lc.amount, lc.drawn_total, -lc.balance))

    # ---------- 支取 ----------
    def process_drawing(self, idx, dr):
        lc_no = dr.get("lc_no")
        tag = "支取#%d(证号 %s)" % (idx, lc_no)
        lc = self.lcs.get(lc_no)
        if lc is None:
            self.errors.append("[支取错误] %s 引用不存在的证号，拒付" % tag)
            self.results.append("%s: 拒付 — 证号不存在" % tag)
            return

        local = []  # 本笔支取的错误
        docs = dr.get("docs", [])
        amount = float(dr.get("amount", 0))
        draw_date = parse_date(dr.get("date", ""), tag + " date", local)

        # 1) 单据按最新版本审核
        presented = {d.get("type") for d in docs}
        for req in lc.required_docs:
            if req not in presented:
                local.append("[单据缺失] %s 缺少必交单据 %s（按版本 %d 审核）"
                             % (tag, req, lc.version))

        # 2) 发票勾稽 + 抬头
        for i, d in enumerate(docs):
            if d.get("type") != "INVOICE":
                continue
            issuer = d.get("issuer")
            if issuer is not None and issuer != lc.beneficiary:
                local.append("[抬头不符] %s 发票#%d 抬头 %r 与受益人 %r 不符"
                             % (tag, i, issuer, lc.beneficiary))
            q, p, a = d.get("quantity"), d.get("unit_price"), d.get("amount")
            if q is not None and p is not None and a is not None:
                expect = float(q) * float(p)
                if abs(expect - float(a)) > 0.005:
                    local.append(
                        "[勾稽不符] %s 发票#%d 金额 %.2f != 数量 %s x 单价 %s = %.2f（差异 %.2f）"
                        % (tag, i, float(a), q, p, expect, float(a) - expect))

        # 3) 日期链: 开证日 <= 装运日 <= 支取日
        issue = date.fromisoformat(lc.issue_date)
        for i, d in enumerate(docs):
            if d.get("type") != "B/L":
                continue
            ship = parse_date(d.get("date", ""), "%s B/L#%d date" % (tag, i), local)
            if ship is None:
                continue
            if ship < issue:
                local.append("[日期链] %s B/L#%d 装运日 %s 早于开证日 %s"
                             % (tag, i, ship, lc.issue_date))
            if draw_date is not None and ship > draw_date:
                local.append("[日期链] %s B/L#%d 装运日 %s 晚于支取日 %s"
                             % (tag, i, ship, dr.get("date")))

        # 4) 余额校验（累计扣减，按最新版本额度）
        if amount > lc.balance + 1e-9:
            over = amount - lc.balance
            local.append("[余额不足] %s 支取 %.2f 超过当前余额 %.2f，超出 %.2f，拒付"
                         % (tag, amount, lc.balance, over))

        self.errors.extend(local)
        if local:
            self.results.append("%s: 拒付 — %d 项不符（余额 %.2f 不变）"
                                % (tag, len(local), lc.balance))
        else:
            lc.drawn_total += amount
            self.results.append("%s: 承付 %.2f — 累计支取 %.2f，剩余额度 %.2f（版本 %d）"
                                % (tag, amount, lc.drawn_total, lc.balance, lc.version))

    # ---------- 汇总 ----------
    def report(self):
        lines = []
        lines.append("=" * 60)
        lines.append("支取/更正结论")
        lines.append("=" * 60)
        lines.extend(self.results or ["（无）"])
        lines.append("")
        lines.append("=" * 60)
        lines.append("错误清单（共 %d 条）" % len(self.errors))
        lines.append("=" * 60)
        lines.extend(self.errors or ["（无错误）"])
        lines.append("")
        lines.append("最终状态")
        for lc in self.lcs.values():
            lines.append("  证号 %s 版本 %d 额度 %.2f 已支取 %.2f 余额 %.2f"
                         % (lc.lc_no, lc.version, lc.amount, lc.drawn_total, lc.balance))
        return "\n".join(lines)


def run(case):
    eng = Engine()
    for defn in case.get("lcs", []):
        eng.define_lc(defn)
    # 更正单流与支取流按各自顺序处理；若提供 events 则按事件序（跨流状态延续）
    events = case.get("events")
    if events:
        di = 0
        for ev in events:
            if ev["kind"] == "amendment":
                eng.apply_amendment(ev["data"])
            else:
                di += 1
                eng.process_drawing(di, ev["data"])
    else:
        for amd in case.get("amendments", []):
            eng.apply_amendment(amd)
        for i, dr in enumerate(case.get("drawings", []), 1):
            eng.process_drawing(i, dr)
    return eng


def sample_case():
    """内置样例：版本链（正常递增/跳号/回退/不存在证号）+ 支取流（各类错误）"""
    return {
        "lcs": [
            {"lc_no": "LC001", "amount": 100000, "issue_date": "2026-01-10",
             "beneficiary": "上海玩具厂", "applicant": "纽约贸易公司",
             "required_docs": ["INVOICE", "B/L", "PACKING_LIST"]},
            {"lc_no": "LC002", "amount": 50000, "issue_date": "2026-02-01",
             "beneficiary": "深圳电子厂", "applicant": "伦敦商贸",
             "required_docs": ["INVOICE", "B/L"]},
        ],
        "events": [
            # ---- 更正单流：版本链 ----
            {"kind": "amendment", "data": {"lc_no": "LC001", "version": 2,
                "changes": {"amount": 120000}}},                       # 正常递增
            {"kind": "amendment", "data": {"lc_no": "LC001", "version": 4,
                "changes": {"required_docs": ["INVOICE", "B/L", "PACKING_LIST", "INSURANCE"]}}},  # 跳号 3
            {"kind": "amendment", "data": {"lc_no": "LC001", "version": 3,
                "changes": {"amount": 90000}}},                        # 回退，拒绝
            {"kind": "amendment", "data": {"lc_no": "LC999", "version": 2,
                "changes": {"amount": 80000}}},                        # 证号不存在
            # ---- 支取流 ----
            {"kind": "drawing", "data": {"lc_no": "LC001", "amount": 60000, "date": "2026-03-01",
                "docs": [
                    {"type": "INVOICE", "issuer": "上海玩具厂", "amount": 60000,
                     "quantity": 200, "unit_price": 300},
                    {"type": "B/L", "date": "2026-02-20"},
                    {"type": "PACKING_LIST"},
                    {"type": "INSURANCE"},
                ]}},                                                   # 承付
            {"kind": "drawing", "data": {"lc_no": "LC001", "amount": 70000, "date": "2026-03-05",
                "docs": [
                    {"type": "INVOICE", "issuer": "上海玩具厂", "amount": 70000,
                     "quantity": 100, "unit_price": 700},
                    {"type": "B/L", "date": "2026-03-01"},
                    {"type": "PACKING_LIST"},
                    {"type": "INSURANCE"},
                ]}},                                                   # 余额不足：余额60000，超出10000
            {"kind": "drawing", "data": {"lc_no": "LC001", "amount": 30000, "date": "2026-03-10",
                "docs": [
                    {"type": "INVOICE", "issuer": "广州冒名厂", "amount": 30000,
                     "quantity": 100, "unit_price": 280},
                    {"type": "B/L", "date": "2026-01-05"},
                    {"type": "PACKING_LIST"},
                    {"type": "INSURANCE"},
                ]}},                                                   # 抬头不符 + 勾稽不符 + 装运早于开证
            {"kind": "drawing", "data": {"lc_no": "LC002", "amount": 20000, "date": "2026-03-02",
                "docs": [
                    {"type": "INVOICE", "issuer": "深圳电子厂", "amount": 20000,
                     "quantity": 40, "unit_price": 500},
                ]}},                                                   # 缺 B/L
            {"kind": "drawing", "data": {"lc_no": "LC999", "amount": 1000, "date": "2026-03-03",
                "docs": []}},                                          # 证号不存在
            {"kind": "drawing", "data": {"lc_no": "LC001", "amount": 50000, "date": "2026-03-20",
                "docs": [
                    {"type": "INVOICE", "issuer": "上海玩具厂", "amount": 50000,
                     "quantity": 250, "unit_price": 200},
                    {"type": "B/L", "date": "2026-03-15"},
                    {"type": "PACKING_LIST"},
                    {"type": "INSURANCE"},
                ]}},                                                   # 承付：余额 120000-60000-50000=10000
        ],
    }


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            case = json.load(f)
    else:
        case = sample_case()
        print("（未提供输入文件，运行内置样例；用法: python3 lc_audit.py case.json）\n")
    eng = run(case)
    print(eng.report())
    sys.exit(1 if eng.errors else 0)


if __name__ == "__main__":
    main()
