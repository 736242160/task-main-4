#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coupon_engine.py — 券叠加 / 核销单文件工具（纯 Python 标准库）

用法:
    python3 coupon_engine.py input.json     # 从 JSON 文件读入并输出核销结果
    python3 coupon_engine.py                # 从 stdin 读入
    python3 coupon_engine.py --demo         # 运行内置组合判定示例（可直接验证）

输入 JSON 格式:
{
  "coupons": [                       # 券定义
    {"id": "C1", "type": "threshold", "threshold": 100, "amount": 30,
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": "G1"},
    {"id": "C2", "type": "discount", "rate": 0.8,
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "mutex_group": null}
  ],
  "orders": [                        # 订单流
    {"id": "O1", "amount": 200.00, "date": "2026-06-01",
     "usable_coupons": ["C1", "C2"]}
  ],
  "redemptions": [                   # 核销流（按顺序处理，状态跨流延续）
    {"order_id": "O1", "coupon_ids": ["C1", "C2"], "date": "2026-06-02"}
  ]
}

自定规则（显式约定）:
  1. 金额内部以“分”(整数)计算，折扣四舍五入(ROUND_HALF_UP)到分。
  2. 叠加顺序固定为：先满减、后折扣。若换成“先折扣、后满减”结果不同，
     核销明细中报告 order_sensitive 及两种结果差额。
  3. 互斥规则：同一互斥组内至多一张券；无互斥组的券互不冲突。
  4. 最优组合：在该订单可用券中（未过期、未核销、互斥合法、减免后不为负），
     使最终金额最小；并列时取券数最少者。核销组合未达最优时报告可再省金额。
  5. 满减按减免过程中的当前金额判断是否满足门槛。
  6. 核销失败不产生任何状态变更；成功的核销会把券标记为已核销，
     后续核销流中再次使用同一券会被拦截（跨流状态延续）。
"""

import itertools
import json
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal


# ---------- 金额工具 ----------

def to_cents(yuan) -> int:
    return int((Decimal(str(yuan)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_yuan(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


# ---------- 数据模型 ----------

@dataclass
class Coupon:
    id: str
    type: str                      # "threshold" 满减 | "discount" 折扣
    threshold: int = 0             # 满减门槛（分）
    amount: int = 0                # 满减减免（分）
    rate: Decimal = None           # 折扣率，如 0.8
    valid_from: str = ""
    valid_to: str = ""
    mutex_group: str = None

    def valid_on(self, date: str) -> bool:
        return self.valid_from <= date <= self.valid_to


@dataclass
class Order:
    id: str
    amount: int                    # 分
    date: str
    usable_coupons: list = field(default_factory=list)


# ---------- 核心计算 ----------

def apply_combo(amount_cents: int, coupons, rule: str = "threshold_first") -> int:
    """按固定顺序应用券组合，返回最终金额（分）。rule: threshold_first / discount_first"""
    thresholds = [c for c in coupons if c.type == "threshold"]
    discounts = [c for c in coupons if c.type == "discount"]
    seq = thresholds + discounts if rule == "threshold_first" else discounts + thresholds
    amt = amount_cents
    for c in seq:
        if c.type == "threshold":
            if amt >= c.threshold:
                amt -= c.amount
        else:
            amt = int((Decimal(amt) * c.rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return amt


def mutex_ok(coupons) -> bool:
    """同一互斥组内至多一张"""
    seen = set()
    for c in coupons:
        if c.mutex_group is None:
            continue
        if c.mutex_group in seen:
            return False
        seen.add(c.mutex_group)
    return True


def best_combo(amount_cents: int, coupons):
    """枚举互斥合法组合，返回 (最优最终金额, 最优组合tuple, 全部合法组合数)"""
    groups = {}
    for c in coupons:
        groups.setdefault(c.mutex_group or ("__solo__" + c.id), []).append(c)
    if len(groups) > 24:
        raise ValueError("互斥组过多，组合空间超限")
    best_final, best_pick, legal = amount_cents + 1, (), 0
    for choice in itertools.product(*[[None] + gs for gs in groups.values()]):
        combo = tuple(c for c in choice if c)
        final = apply_combo(amount_cents, combo)
        if final < 0:
            continue
        legal += 1
        if final < best_final or (final == best_final and len(combo) < len(best_pick)):
            best_final, best_pick = final, combo
    return best_final, best_pick, legal


# ---------- 核销引擎 ----------

class Engine:
    def __init__(self, data: dict):
        self.coupons = {}
        for c in data.get("coupons", []):
            self.coupons[c["id"]] = Coupon(
                id=c["id"], type=c["type"],
                threshold=to_cents(c.get("threshold", 0)),
                amount=to_cents(c.get("amount", 0)),
                rate=Decimal(str(c["rate"])) if "rate" in c else None,
                valid_from=c.get("valid_from", "0000-01-01"),
                valid_to=c.get("valid_to", "9999-12-31"),
                mutex_group=c.get("mutex_group"),
            )
        self.orders = {}
        for o in data.get("orders", []):
            self.orders[o["id"]] = Order(
                id=o["id"], amount=to_cents(o["amount"]), date=o.get("date", ""),
                usable_coupons=list(o.get("usable_coupons", [])),
            )
        self.redeemed = {}           # coupon_id -> order_id（跨流状态）
        self.details = []            # 核销明细
        self.errors = []             # 错误清单

    def fail(self, idx, req, code, msg):
        self.errors.append({"seq": idx, "order_id": req.get("order_id"),
                            "coupon_ids": req.get("coupon_ids", []),
                            "error": code, "message": msg})

    def run(self, redemptions):
        for idx, req in enumerate(redemptions):
            self.redeem(idx, req)
        return {"redemptions": self.details, "errors": self.errors}

    def redeem(self, idx, req):
        oid = req.get("order_id")
        cids = req.get("coupon_ids", [])
        date = req.get("date", "")

        # 1. 订单存在性
        order = self.orders.get(oid)
        if order is None:
            return self.fail(idx, req, "ORDER_NOT_FOUND", f"订单不存在: {oid}")

        # 2. 券存在性
        missing = [cid for cid in cids if cid not in self.coupons]
        if missing:
            return self.fail(idx, req, "COUPON_NOT_FOUND", f"券不存在: {missing}")

        # 3. 组合内重复券
        if len(set(cids)) != len(cids):
            return self.fail(idx, req, "DUPLICATE_IN_COMBO", "组合内包含重复券")

        # 4. 券须在订单可用列表中
        not_usable = [cid for cid in cids if cid not in order.usable_coupons]
        if not_usable:
            return self.fail(idx, req, "NOT_USABLE", f"券不在订单可用列表: {not_usable}")

        # 5. 重复核销拦截（跨流状态）
        used = [(cid, self.redeemed[cid]) for cid in cids if cid in self.redeemed]
        if used:
            return self.fail(idx, req, "ALREADY_REDEEMED",
                             f"券已核销: {[c for c, _ in used]} (首次核销订单: {[o for _, o in used]})")

        combo = [self.coupons[cid] for cid in cids]

        # 6. 过期拦截
        expired = [c.id for c in combo if not c.valid_on(date)]
        if expired:
            return self.fail(idx, req, "COUPON_EXPIRED", f"券已过期或未生效 @ {date}: {expired}")

        # 7. 互斥组校验
        if not mutex_ok(combo):
            return self.fail(idx, req, "MUTEX_CONFLICT", "组合违反互斥组规则（同组至多一张）")

        # 8. 重算减免（固定顺序：先满减后折扣）
        final = apply_combo(order.amount, combo, "threshold_first")

        # 9. 负金额拦截
        if final < 0:
            return self.fail(idx, req, "NEGATIVE_AMOUNT",
                             f"减免后金额为负: {to_yuan(final)}")

        # 10. 顺序敏感性：换序结果不同则报告
        alt = apply_combo(order.amount, combo, "discount_first")
        order_sensitive = alt != final

        # 11. 最优性：与可再省金额
        pool = [self.coupons[cid] for cid in order.usable_coupons
                if cid not in self.redeemed and self.coupons[cid].valid_on(date)]
        best_final, best_pick, _ = best_combo(order.amount, pool)
        extra_savable = max(0, final - best_final)

        # 成功：登记状态（跨流延续）
        for cid in cids:
            self.redeemed[cid] = oid

        detail = {
            "seq": idx, "order_id": oid, "coupon_ids": cids, "date": date,
            "original_amount": to_yuan(order.amount),
            "saved": to_yuan(order.amount - final),
            "final_amount": to_yuan(final),
            "status": "OK",
        }
        if order_sensitive:
            detail["order_sensitive"] = {
                "message": "折扣与满减叠加顺序不同结果不同（本系统固定先满减后折扣）",
                "threshold_first": to_yuan(final),
                "discount_first": to_yuan(alt),
                "diff": to_yuan(abs(alt - final)),
            }
        if extra_savable > 0:
            detail["suboptimal"] = {
                "message": "未用满最省组合",
                "extra_savable": to_yuan(extra_savable),
                "best_combo": [c.id for c in best_pick],
                "best_final_amount": to_yuan(best_final),
            }
        self.details.append(detail)


# ---------- 内置组合判定示例 ----------

def demo_input():
    """每个场景使用独立订单与独立券，避免全局单次核销状态互相干扰。"""
    coupons = []
    orders = []
    R = []

    def th(cid, threshold, amount, group=None, vf="2026-01-01", vt="2026-12-31"):
        coupons.append({"id": cid, "type": "threshold", "threshold": threshold,
                        "amount": amount, "valid_from": vf, "valid_to": vt,
                        "mutex_group": group})

    def dc(cid, rate, group=None, vf="2026-01-01", vt="2026-12-31"):
        coupons.append({"id": cid, "type": "discount", "rate": rate,
                        "valid_from": vf, "valid_to": vt, "mutex_group": group})

    def order(oid, amount, cids):
        orders.append({"id": oid, "amount": amount, "date": "2026-06-01",
                       "usable_coupons": cids})

    # 场景1: 最优组合 + 顺序敏感。O1=200, 满100减30 + 8折
    #   先满减后折扣: (200-30)*0.8 = 136.00 ; 先折扣后满减: 200*0.8-30 = 130.00
    th("A_T30", 100, 30, group="G1"); dc("A_D80", 0.8)
    order("O1", 200.00, ["A_T30", "A_D80"])
    R.append({"order_id": "O1", "coupon_ids": ["A_T30", "A_D80"], "date": "2026-06-02"})

    # 场景2: 未用满最省组合。O2=120, 只用8折=96.00；最优 满100减30+8折=72.00，可再省24.00
    th("B_T30", 100, 30, group="G1"); dc("B_D80", 0.8)
    order("O2", 120.00, ["B_T30", "B_D80"])
    R.append({"order_id": "O2", "coupon_ids": ["B_D80"], "date": "2026-06-02"})

    # 场景3: 互斥冲突。两张满减同组 G1 -> 拦截
    th("C_T30", 100, 30, group="G1"); th("C_T10", 50, 10, group="G1")
    order("O3", 100.00, ["C_T30", "C_T10"])
    R.append({"order_id": "O3", "coupon_ids": ["C_T30", "C_T10"], "date": "2026-06-02"})

    # 场景4: 重复核销。D_T30 先在 O4 核销成功，再在 O5 使用 -> 拦截（跨流状态延续）
    th("D_T30", 100, 30)
    order("O4", 150.00, ["D_T30"]); order("O5", 200.00, ["D_T30"])
    R.append({"order_id": "O4", "coupon_ids": ["D_T30"], "date": "2026-06-02"})
    R.append({"order_id": "O5", "coupon_ids": ["D_T30"], "date": "2026-06-03"})

    # 场景5: 过期券 -> 拦截
    th("E_OLD", 10, 5, vf="2025-01-01", vt="2025-12-31")
    order("O6", 40.00, ["E_OLD"])
    R.append({"order_id": "O6", "coupon_ids": ["E_OLD"], "date": "2026-06-02"})

    # 场景6: 减免后为负。40 - 500 < 0 -> 拦截
    th("F_BIG", 0, 500)
    order("O7", 40.00, ["F_BIG"])
    R.append({"order_id": "O7", "coupon_ids": ["F_BIG"], "date": "2026-06-02"})

    # 场景7: 引用不存在的券 -> 报告
    order("O8", 100.00, [])
    R.append({"order_id": "O8", "coupon_ids": ["NOPE"], "date": "2026-06-02"})

    # 场景8: 引用不存在的订单 -> 报告
    R.append({"order_id": "O9", "coupon_ids": ["A_D80"], "date": "2026-06-02"})

    # 场景9: 券不在订单可用列表 -> 拦截
    th("I_T30", 100, 30)
    order("O10", 150.00, [])
    R.append({"order_id": "O10", "coupon_ids": ["I_T30"], "date": "2026-06-02"})

    return {"coupons": coupons, "orders": orders, "redemptions": R}


def main():
    if "--demo" in sys.argv[1:]:
        data = demo_input()
        print("=== 内置组合判定示例 ===")
    elif len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    engine = Engine(data)
    result = engine.run(data.get("redemptions", []))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
