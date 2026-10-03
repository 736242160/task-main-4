#!/usr/bin/env python3
"""claims_tool.py — 理赔事故归并、定损分摊与赔付拦截工具（纯 Python 标准库，单文件）

用法:
    python3 claims_tool.py input.json     # 处理外部输入文件
    python3 claims_tool.py --demo         # 运行内置分摊计算示例

输入 JSON 格式（四个流，按 报案 -> 定损 -> 赔付 顺序处理，状态跨流延续）:
{
  "policies":    [{"policy_id": "P1", "limit": 100000, "type": "车险"}, ...],
  "reports":     [{"accident_id": "A1", "policy_id": "P1", "loss": 30000,
                   "ratio": 0.75}, ...],          # ratio 可选；同一事故要么全给要么全不给
  "assessments": [{"accident_id": "A1", "amount": 36000}, ...],
  "payments":    [{"accident_id": "A1", "policy_id": "P1", "amount": 27000}, ...]
}

分摊规则（默认）: 某保单责任比例 = 该保单报案损失 / 事故总报案损失；
若报案流给出了自定义 ratio，则校验其之和必须等于 1，否则拦截该次定损并报告。
所有金额用 fractions.Fraction 精确计算，无浮点误差。
"""

import json
import sys
from fractions import Fraction


def money(value):
    return Fraction(str(value))


def fmt(value):
    return f"{float(value):,.2f}"


def pct(value):
    return f"{float(value * 100):.4f}%"


class Engine:
    def __init__(self, policies):
        self.policies = {}
        for p in policies:
            pid = p["policy_id"]
            self.policies[pid] = {"limit": money(p["limit"]), "type": p.get("type", "")}
        self.accidents = {}
        self.paid_total = {pid: Fraction(0) for pid in self.policies}
        self.errors = []
        self.notices = []

    def err(self, code, msg):
        self.errors.append((code, msg))

    def note(self, code, msg):
        self.notices.append((code, msg))

    # ---------- 报案流 ----------
    def process_report(self, r):
        aid, pid = r["accident_id"], r["policy_id"]
        loss = money(r["loss"])
        if pid not in self.policies:
            self.err("UNKNOWN_POLICY", f"报案引用未知保单 {pid}（事故 {aid}），已拦截")
            return
        acc = self.accidents.get(aid)
        if acc is None:
            acc = {"reports": {}, "custom_ratios": {}, "assessed": None,
                   "ratios": {}, "shares": {}, "paid": {}, "status": "open"}
            self.accidents[aid] = acc
        else:
            if acc["status"] == "closed":
                self.err("REPORT_AFTER_CLOSE",
                         f"事故 {aid} 已结案，保单 {pid} 的新增报案已拦截")
                return
            if acc["assessed"] is not None:
                self.err("REPORT_AFTER_ASSESSMENT",
                         f"事故 {aid} 已定损，保单 {pid} 的新增报案已拦截")
                return
            if pid in acc["reports"]:
                self.err("DUPLICATE_REPORT",
                         f"保单 {pid} 对事故 {aid} 重复报案，已拦截")
                return
            self.note("ACCIDENT_MERGED",
                      f"事故 {aid} 被保单 {pid} 再次报案，已归并为同一事故"
                      f"（现有保单: {sorted(acc['reports'])}）")
        acc["reports"][pid] = loss
        if "ratio" in r:
            acc["custom_ratios"][pid] = money(r["ratio"])

    # ---------- 定损流 ----------
    def process_assessment(self, a):
        aid = a["accident_id"]
        amount = money(a["amount"])
        acc = self.accidents.get(aid)
        if acc is None:
            self.err("ASSESSMENT_WITHOUT_REPORT",
                     f"事故 {aid} 无任何报案，定损已拦截")
            return
        if acc["assessed"] is not None:
            self.err("DUPLICATE_ASSESSMENT", f"事故 {aid} 重复定损，已拦截")
            return
        total_loss = sum(acc["reports"].values(), Fraction(0))
        if amount > total_loss:
            self.err("ASSESSMENT_EXCEEDS_LOSS",
                     f"事故 {aid} 定损额 {fmt(amount)} 超过事故总损失 {fmt(total_loss)}")

        custom = acc["custom_ratios"]
        if custom:
            if set(custom) != set(acc["reports"]):
                self.err("RATIO_INCOMPLETE",
                         f"事故 {aid} 自定义责任比例未覆盖全部报案保单，定损已拦截")
                return
            ratio_sum = sum(custom.values(), Fraction(0))
            if ratio_sum != 1:
                self.err("RATIO_SUM_NOT_ONE",
                         f"事故 {aid} 责任分摊比例之和为 {float(ratio_sum):.6f}（≠1），定损已拦截")
                return
            ratios = dict(custom)
        else:
            if total_loss == 0:
                self.err("ZERO_TOTAL_LOSS", f"事故 {aid} 总损失为 0，无法分摊，定损已拦截")
                return
            ratios = {pid: loss / total_loss for pid, loss in acc["reports"].items()}

        check = sum(ratios.values(), Fraction(0))
        if check != 1:  # 防御性校验，正常不会触发
            self.err("RATIO_SUM_NOT_ONE",
                     f"事故 {aid} 计算所得比例之和为 {float(check):.6f}（≠1），定损已拦截")
            return

        acc["assessed"] = amount
        acc["ratios"] = ratios
        acc["shares"] = {pid: amount * ratio for pid, ratio in ratios.items()}
        acc["paid"] = {pid: Fraction(0) for pid in ratios}
        acc["status"] = "assessed"

    # ---------- 赔付流 ----------
    def process_payment(self, p):
        aid, pid = p["accident_id"], p["policy_id"]
        amount = money(p["amount"])
        acc = self.accidents.get(aid)
        if acc is None or pid not in acc["reports"]:
            self.err("PAYMENT_WITHOUT_REPORT",
                     f"赔付引用未归并的报案（事故 {aid}，保单 {pid}），已拦截")
            return
        if acc["assessed"] is None:
            self.err("PAYMENT_BEFORE_ASSESSMENT",
                     f"事故 {aid} 尚未定损，保单 {pid} 的赔付已拦截")
            return
        limit = self.policies[pid]["limit"]
        if self.paid_total[pid] + amount > limit:
            self.err("PAYMENT_EXCEEDS_LIMIT",
                     f"保单 {pid} 累计赔付 {fmt(self.paid_total[pid] + amount)} "
                     f"超保额 {fmt(limit)}，已拦截（问题保单号: {pid}）")
            return
        if acc["paid"][pid] + amount > acc["shares"][pid]:
            self.err("PAYMENT_EXCEEDS_SHARE",
                     f"保单 {pid} 在事故 {aid} 累计赔付将超其分摊额 "
                     f"{fmt(acc['shares'][pid])}，已拦截")
            return
        acc["paid"][pid] += amount
        self.paid_total[pid] += amount
        if sum(acc["paid"].values(), Fraction(0)) == acc["assessed"]:
            acc["status"] = "closed"
            self.note("ACCIDENT_CLOSED", f"事故 {aid} 赔付完毕，已结案")

    # ---------- 输出 ----------
    def render(self):
        out = []
        out.append("=" * 78)
        out.append("事故分摊表")
        out.append("=" * 78)
        for aid in sorted(self.accidents):
            acc = self.accidents[aid]
            total_loss = sum(acc["reports"].values(), Fraction(0))
            assessed = acc["assessed"]
            paid_sum = sum(acc["paid"].values(), Fraction(0))
            out.append(f"\n事故 {aid}  状态: {acc['status']}  总损失: {fmt(total_loss)}  "
                       f"定损: {fmt(assessed) if assessed is not None else '-'}  "
                       f"已赔: {fmt(paid_sum)}")
            out.append(f"  {'保单号':<8}{'报案损失':>14}{'责任比例':>12}"
                       f"{'分摊定损':>14}{'已赔付':>14}{'剩余分摊':>14}")
            for pid in sorted(acc["reports"]):
                loss = acc["reports"][pid]
                ratio = acc["ratios"].get(pid)
                share = acc["shares"].get(pid)
                paid = acc["paid"].get(pid, Fraction(0))
                remain = share - paid if share is not None else None
                out.append(f"  {pid:<8}{fmt(loss):>14}"
                           f"{pct(ratio) if ratio is not None else '-':>12}"
                           f"{fmt(share) if share is not None else '-':>14}"
                           f"{fmt(paid):>14}"
                           f"{fmt(remain) if remain is not None else '-':>14}")
        out.append("")
        out.append("=" * 78)
        out.append("保单状态")
        out.append("=" * 78)
        out.append(f"  {'保单号':<8}{'险种':<10}{'保额':>14}{'累计赔付':>14}{'剩余额度':>14}")
        for pid in sorted(self.policies):
            pol = self.policies[pid]
            paid = self.paid_total[pid]
            out.append(f"  {pid:<8}{pol['type']:<10}{fmt(pol['limit']):>14}"
                       f"{fmt(paid):>14}{fmt(pol['limit'] - paid):>14}")
        out.append("")
        out.append("=" * 78)
        out.append(f"归并/结案记录（{len(self.notices)} 条）")
        out.append("=" * 78)
        for code, msg in self.notices:
            out.append(f"  [{code}] {msg}")
        out.append("")
        out.append("=" * 78)
        out.append(f"错误清单（{len(self.errors)} 条）")
        out.append("=" * 78)
        for code, msg in self.errors:
            out.append(f"  [{code}] {msg}")
        return "\n".join(out)


def run(data):
    eng = Engine(data.get("policies", []))
    for r in data.get("reports", []):
        eng.process_report(r)
    for a in data.get("assessments", []):
        eng.process_assessment(a)
    for p in data.get("payments", []):
        eng.process_payment(p)
    return eng.render()


DEMO = {
    "policies": [
        {"policy_id": "P1", "limit": 100000, "type": "车险"},
        {"policy_id": "P2", "limit": 50000, "type": "财产险"},
        {"policy_id": "P3", "limit": 80000, "type": "责任险"},
    ],
    "reports": [
        {"accident_id": "A1", "policy_id": "P1", "loss": 30000},
        {"accident_id": "A1", "policy_id": "P2", "loss": 10000},   # 同一事故多保单 -> 归并
        {"accident_id": "A2", "policy_id": "P3", "loss": 20000},
        {"accident_id": "A3", "policy_id": "P1", "loss": 5000, "ratio": 0.5},
        {"accident_id": "A3", "policy_id": "P2", "loss": 5000, "ratio": 0.6},  # 比例和≠1
    ],
    "assessments": [
        {"accident_id": "A1", "amount": 36000},   # 正常: P1 3/4=27000, P2 1/4=9000
        {"accident_id": "A2", "amount": 25000},   # 定损超总损失 20000 -> 报告
        {"accident_id": "A3", "amount": 9000},    # 比例和 1.1 -> 拦截
    ],
    "payments": [
        {"accident_id": "A1", "policy_id": "P1", "amount": 27000},
        {"accident_id": "A1", "policy_id": "P2", "amount": 9000},   # A1 赔满 -> 结案
        {"accident_id": "A1", "policy_id": "P3", "amount": 1000},   # 未归并报案 -> 拦截
        {"accident_id": "A1", "policy_id": "P1", "amount": 80000},  # P1 累计超保额 -> 拦截
        {"accident_id": "A2", "policy_id": "P3", "amount": 26000},  # 超分摊额 -> 拦截
        {"accident_id": "A2", "policy_id": "P3", "amount": 25000},
    ],
}

DEMO_LATE_REPORT = {"accident_id": "A1", "policy_id": "P3", "loss": 5000}


def main(argv):
    if len(argv) >= 2 and argv[1] == "--demo":
        print("内置示例输入：")
        print(json.dumps(DEMO, ensure_ascii=False, indent=2))
        print()
        print(run(DEMO))
        print()
        print("=" * 78)
        print("跨流状态延续演示：A1 已结案，此时追加一条新报案")
        print("=" * 78)
        eng = Engine(DEMO["policies"])
        for r in DEMO["reports"]:
            eng.process_report(r)
        for a in DEMO["assessments"]:
            eng.process_assessment(a)
        for p in DEMO["payments"]:
            eng.process_payment(p)
        eng.process_report(DEMO_LATE_REPORT)
        for code, msg in eng.errors[-1:]:
            print(f"  [{code}] {msg}")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 1
    with open(argv[1], encoding="utf-8") as f:
        data = json.load(f)
    print(run(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
