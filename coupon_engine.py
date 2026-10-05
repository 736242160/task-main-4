#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coupon_engine.py — 优惠券叠加 / 核销单文件工具（仅 Python 标准库）

叠加规则（自定，全局固定）：
  1. 互斥组内至多一张券；不同互斥组、无组券可自由叠加。
  2. 叠加顺序：先满减（门槛按订单原始金额判定，减免额累加），后折扣（逐张连乘）。
  3. 若按相反顺序（先折扣后满减，门槛按折后金额判定）结果不同，则报告"顺序敏感"。
  4. 减免后金额为负 -> 拦截。
  5. 最优组合 = 在（未过期、未核销、满足互斥、金额非负）约束下减免最多的组合，
     并列时取券数最少者；给定组合未达最优时报告"可再省金额"。

用法：
  python3 coupon_engine.py --demo        # 运行内置组合判定示例（可直接验证）
  python3 coupon_engine.py input.json    # 处理 JSON 事件流文件
  python3 coupon_engine.py               # 从 stdin 读取 JSON 事件流

输入 JSON：
{
  "events": [
    {"type": "coupon", "id": "C1", "kind": "threshold",
     "threshold": "100.00", "off": "20.00",
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G1"},
    {"type": "coupon", "id": "C3", "kind": "discount", "rate": "0.90",
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G2"},
    {"type": "order",  "id": "O1", "amount": "200.00",
     "coupons": ["C1", "C3"], "date": "2026-10-01"},
    {"type": "redeem", "order": "O1", "coupons": ["C1", "C3"], "date": "2026-10-02"}
  ]
}
事件按顺序处理，状态（券定义 / 订单 / 已核销券）跨事件流延续。
"""

import json
import sys
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations

RULE_ORDER = "threshold_first"   # 自定叠加规则：先满减后折扣
RULE_ORDER_ALT = "discount_first"


def to_cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(cents):
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


class Coupon:
    def __init__(self, ev):
        self.id = ev["id"]
        self.kind = ev["kind"]                      # threshold | discount
        self.threshold = to_cents(ev.get("threshold", 0))
        self.off = to_cents(ev.get("off", 0))
        self.rate = Decimal(str(ev.get("rate", 1)))
        self.valid_from = parse_date(ev.get("valid_from"))
        self.valid_to = parse_date(ev.get("valid_to"))
        self.mutex_group = ev.get("mutex_group")

    def valid_on(self, d):
        if self.valid_from and d < self.valid_from:
            return False
        if self.valid_to and d > self.valid_to:
            return False
        return True


class Order:
    def __init__(self, ev):
        self.id = ev["id"]
        self.amount = to_cents(ev["amount"])
        self.coupon_ids = list(ev.get("coupons", []))
        self.date = parse_date(ev.get("date"))
        self.redeemed = False


def apply_combo(amount, combo, rule):
    """按给定顺序规则计算组合减免。返回 (最终金额, 明细列表)。"""
    thresholds = [c for c in combo if c.kind == "threshold"]
    discounts = [c for c in combo if c.kind == "discount"]
    detail = []
    if rule == "threshold_first":
        off_total = 0
        for c in thresholds:
            if amount >= c.threshold:
                off_total += c.off
                detail.append({"coupon": c.id, "effect": fmt(c.off), "note": "满减生效"})
            else:
                detail.append({"coupon": c.id, "effect": "0.00", "note": "未达门槛"})
        amt = amount - off_total
        for c in discounts:
            before = amt
            amt = int((Decimal(amt) * c.rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            detail.append({"coupon": c.id, "effect": fmt(before - amt), "note": "折扣 %.2f 折" % (c.rate * 10)})
    else:  # discount_first：门槛按折后金额判定
        amt = amount
        for c in discounts:
            before = amt
            amt = int((Decimal(amt) * c.rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            detail.append({"coupon": c.id, "effect": fmt(before - amt), "note": "折扣 %.2f 折" % (c.rate * 10)})
        off_total = 0
        for c in thresholds:
            if amt >= c.threshold:
                off_total += c.off
                detail.append({"coupon": c.id, "effect": fmt(c.off), "note": "满减生效"})
            else:
                detail.append({"coupon": c.id, "effect": "0.00", "note": "未达门槛"})
        amt -= off_total
    return amt, detail


def mutex_ok(combo):
    groups = [c.mutex_group for c in combo if c.mutex_group]
    return len(groups) == len(set(groups))


def best_combination(order, pool):
    """在候选券中搜索最优组合。返回 (节省金额, 组合id列表, 最优最终金额)。"""
    best = None  # (key, savings, ids, final)
    for r in range(len(pool) + 1):
        for combo in combinations(pool, r):
            if not mutex_ok(combo):
                continue
            final, _ = apply_combo(order.amount, list(combo), RULE_ORDER)
            if final < 0:
                continue
            savings = order.amount - final
            key = (savings, -len(combo))
            if best is None or key > best[0]:
                best = (key, savings, [c.id for c in combo], final)
    return best[1], best[2], best[3]


class Engine:
    def __init__(self):
        self.coupons = {}
        self.orders = {}
        self.used = set()          # 已核销券 id（跨流延续）
        self.results = []
        self.errors = []

    def run(self, events):
        for seq, ev in enumerate(events, 1):
            t = ev.get("type")
            if t == "coupon":
                self.coupons[ev["id"]] = Coupon(ev)
            elif t == "order":
                self.orders[ev["id"]] = Order(ev)
            elif t == "redeem":
                self._redeem(seq, ev)
            else:
                self._error(seq, None, "未知事件类型: %r" % t)
        return {"results": self.results, "errors": self.errors}

    def _error(self, seq, order_id, msg):
        self.errors.append({"seq": seq, "order": order_id, "message": msg})

    def _redeem(self, seq, ev):
        order_id = ev.get("order")
        rec = {"seq": seq, "order": order_id, "coupons": list(ev.get("coupons", [])),
               "status": None, "errors": [], "warnings": []}

        order = self.orders.get(order_id)
        if order is None:
            rec["errors"].append("订单不存在: %s" % order_id)
            rec["status"] = "blocked"
            self._finish(rec)
            return
        if order.redeemed:
            rec["errors"].append("订单已核销，不可重复核销: %s" % order_id)
            rec["status"] = "blocked"
            self._finish(rec)
            return

        on_date = parse_date(ev.get("date")) or order.date or date.today()

        combo, seen = [], set()
        for cid in rec["coupons"]:
            c = self.coupons.get(cid)
            if c is None:
                rec["errors"].append("券不存在: %s" % cid)
                continue
            if cid in seen:
                rec["errors"].append("组合内券重复: %s" % cid)
                continue
            seen.add(cid)
            if cid not in order.coupon_ids:
                rec["errors"].append("券 %s 不在订单可用券列表中" % cid)
            if cid in self.used:
                rec["errors"].append("券 %s 已核销，不可重复使用" % cid)
            if not c.valid_on(on_date):
                rec["errors"].append("券 %s 已过期（有效期 %s ~ %s，核销日 %s）" % (
                    cid, c.valid_from, c.valid_to, on_date))
            combo.append(c)

        if not mutex_ok(combo):
            rec["errors"].append("互斥组冲突：同一互斥组内使用了多张券")

        if rec["errors"]:
            rec["status"] = "blocked"
            self._finish(rec)
            return

        final, detail = apply_combo(order.amount, combo, RULE_ORDER)
        if final < 0:
            rec["errors"].append("减免后订单金额为负（%s），核销被拦截" % fmt(final))
            rec["status"] = "blocked"
            self._finish(rec)
            return

        final_alt, _ = apply_combo(order.amount, combo, RULE_ORDER_ALT)
        if final_alt != final:
            rec["warnings"].append(
                "叠加顺序敏感：先满减后折扣=%s，先折扣后满减=%s；按规则采用先满减后折扣"
                % (fmt(final), fmt(final_alt)))

        savings = order.amount - final
        pool = [self.coupons[cid] for cid in order.coupon_ids
                if cid in self.coupons and cid not in self.used
                and self.coupons[cid].valid_on(on_date)]
        best_savings, best_ids, _ = best_combination(order, pool)
        if best_savings > savings:
            rec["warnings"].append(
                "未用满最省组合：可再省 %s（最优组合 %s，可省 %s；当前组合省 %s）"
                % (fmt(best_savings - savings), best_ids, fmt(best_savings), fmt(savings)))

        for c in combo:
            self.used.add(c.id)
        order.redeemed = True
        rec.update({"status": "success", "original_amount": fmt(order.amount),
                    "discount_amount": fmt(savings), "final_amount": fmt(final),
                    "breakdown": detail})
        self._finish(rec)

    def _finish(self, rec):
        for msg in rec["errors"]:
            self._error(rec["seq"], rec["order"], msg)
        self.results.append(rec)


def demo_events():
    return [
        # ---- 券定义 ----
        {"type": "coupon", "id": "C1", "kind": "threshold", "threshold": "100.00", "off": "20.00",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G1"},
        {"type": "coupon", "id": "C2", "kind": "threshold", "threshold": "100.00", "off": "30.00",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G1"},
        {"type": "coupon", "id": "C3", "kind": "discount", "rate": "0.90",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G2"},
        {"type": "coupon", "id": "C4", "kind": "discount", "rate": "0.80",
         "valid_from": "2026-01-01", "valid_to": "2026-06-30", "mutex_group": "G2"},
        {"type": "coupon", "id": "C5", "kind": "threshold", "threshold": "50.00", "off": "90.00",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G3"},
        {"type": "coupon", "id": "C6", "kind": "threshold", "threshold": "100.00", "off": "10.00",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31"},
        {"type": "coupon", "id": "C8", "kind": "threshold", "threshold": "100.00", "off": "15.00",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G1"},
        {"type": "coupon", "id": "C7", "kind": "discount", "rate": "0.85",
         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G4"},
        # ---- 订单流 ----
        {"type": "order", "id": "O1", "amount": "200.00", "coupons": ["C1", "C2", "C3"], "date": "2026-10-01"},
        {"type": "order", "id": "O2", "amount": "80.00",  "coupons": ["C5"], "date": "2026-10-01"},
        {"type": "order", "id": "O3", "amount": "150.00", "coupons": ["C4", "C7"], "date": "2026-10-01"},
        {"type": "order", "id": "O4", "amount": "120.00", "coupons": ["C6"], "date": "2026-10-01"},
        {"type": "order", "id": "O5", "amount": "150.00", "coupons": ["C1", "C6", "C8"], "date": "2026-10-01"},
        {"type": "order", "id": "O6", "amount": "130.00", "coupons": ["C1"], "date": "2026-10-01"},
        # ---- 核销流 ----
        # 1) 正常叠加：满减30 + 9折；两种顺序结果不同 -> 顺序敏感报告
        {"type": "redeem", "order": "O1", "coupons": ["C2", "C3"], "date": "2026-10-02"},
        # 2) 互斥组冲突：C1 与 C8 同属 G1 -> 拦截
        {"type": "redeem", "order": "O5", "coupons": ["C1", "C8"], "date": "2026-10-02"},
        # 3) 只用 C1，最优是 C1+C6 -> 报告可再省 10.00
        {"type": "redeem", "order": "O5", "coupons": ["C1"], "date": "2026-10-02"},
        # 4) C1 已在 O5 核销 -> 重复核销拦截
        {"type": "redeem", "order": "O6", "coupons": ["C1"], "date": "2026-10-03"},
        # 5) 80 - 90 = -10 -> 金额为负拦截
        {"type": "redeem", "order": "O2", "coupons": ["C5"], "date": "2026-10-02"},
        # 6) C4 已于 2026-06-30 过期 -> 拦截
        {"type": "redeem", "order": "O3", "coupons": ["C4", "C7"], "date": "2026-10-02"},
        # 7) 引用不存在的券 -> 拦截
        {"type": "redeem", "order": "O4", "coupons": ["C999"], "date": "2026-10-02"},
        # 8) 引用不存在的订单 -> 拦截
        {"type": "redeem", "order": "O999", "coupons": ["C6"], "date": "2026-10-02"},
        # 9) 正常核销 O4（C6 未被使用）-> 成功
        {"type": "redeem", "order": "O4", "coupons": ["C6"], "date": "2026-10-02"},
    ]


def main(argv):
    if "--demo" in argv:
        events = demo_events()
        print("=== 输入事件流 ===")
        print(json.dumps({"events": events}, ensure_ascii=False, indent=2))
        report = Engine().run(events)
        print("=== 核销结果 ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    src = open(argv[0], encoding="utf-8").read() if argv else sys.stdin.read()
    events = json.loads(src)["events"]
    print(json.dumps(Engine().run(events), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
