#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""酒店房态配额管理工具（纯 Python 标准库，单文件）。

功能：
  - 每日配额 = 房型房间数；预订按 [入住日, 离店日) 逐日占用配额
  - 超配额预订拦截，并定位日期与房型
  - 退订释放配额；重复退订拦截
  - 改期联动重算新旧期间配额；新期间超配额则拦截且保留原订单
  - 清洁/维修房（maintenance 区间，自定规则）不可预订
  - 引用不存在的房型或订单会报告错误
  - 预订流 -> 变更流 状态延续，最终输出每日配额与错误清单

输入（JSON 文件）：
{
  "room_types": [
    {"name": "标准间", "rooms": 2,
     "maintenance": [["2026-10-05", "2026-10-07"]]}   # 可选，半开区间，清洁/维修不可订
  ],
  "bookings": [
    {"order": "B1", "room_type": "标准间", "checkin": "2026-10-01", "checkout": "2026-10-03"}
  ],
  "changes": [
    {"action": "reschedule", "order": "B1", "checkin": "2026-10-02", "checkout": "2026-10-04"},
    {"action": "cancel", "order": "B1"}
  ]
}

用法：
  python3 hotel_inventory.py input.json
"""

import json
import sys
from collections import defaultdict
from datetime import date, timedelta


def parse_date(text, ctx, errors):
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        errors.append({"op": ctx, "reason": "非法日期格式: %r（应为 YYYY-MM-DD）" % (text,)})
        return None


def date_range(start, end):
    """半开区间 [start, end) 的逐日迭代。"""
    cur = start
    while cur < end:
        yield cur
        cur += timedelta(days=1)


class Inventory:
    def __init__(self):
        self.capacity = {}                 # 房型 -> 房间数
        self.maintenance = defaultdict(set)  # 房型 -> 清洁/维修日期集合
        self.orders = {}                   # 订单号 -> {"room_type", "checkin", "checkout"}
        self.cancelled = set()             # 已退订订单号（用于拦截重复退订）
        self.usage = defaultdict(lambda: defaultdict(int))  # 房型 -> 日期 -> 已占配额
        self.errors = []

    # ---------- 内部工具 ----------

    def _err(self, op, reason, order=None, room_type=None, day=None):
        entry = {"op": op, "reason": reason}
        if order is not None:
            entry["order"] = order
        if room_type is not None:
            entry["room_type"] = room_type
        if day is not None:
            entry["date"] = day.isoformat()
        self.errors.append(entry)

    def _occupy(self, room_type, checkin, checkout, delta):
        for day in date_range(checkin, checkout):
            self.usage[room_type][day] += delta
            if self.usage[room_type][day] == 0:
                del self.usage[room_type][day]

    def _check_period(self, op, order, room_type, checkin, checkout):
        """校验某期间是否可订：维修日拦截 + 超配额拦截。返回是否通过。"""
        ok = True
        for day in date_range(checkin, checkout):
            if day in self.maintenance[room_type]:
                self._err(op, "该日期为清洁/维修房，不可预订",
                          order=order, room_type=room_type, day=day)
                ok = False
            elif self.usage[room_type].get(day, 0) + 1 > self.capacity[room_type]:
                self._err(op, "超配额：当日已订 %d 间，配额 %d 间"
                          % (self.usage[room_type].get(day, 0), self.capacity[room_type]),
                          order=order, room_type=room_type, day=day)
                ok = False
        return ok

    # ---------- 装载房型定义 ----------

    def load_room_types(self, defs):
        for item in defs:
            name = item.get("name")
            rooms = item.get("rooms")
            if not name or not isinstance(rooms, int) or rooms <= 0:
                self._err("room_type", "房型定义非法（需要 name 与正整数 rooms）: %r" % (item,))
                continue
            self.capacity[name] = rooms
            for rng in item.get("maintenance", []):
                start = parse_date(rng[0], "room_type:%s" % name, self.errors)
                end = parse_date(rng[1], "room_type:%s" % name, self.errors)
                if start and end and start < end:
                    for day in date_range(start, end):
                        self.maintenance[name].add(day)

    # ---------- 预订流 ----------

    def book(self, order, room_type, checkin_s, checkout_s):
        op = "book"
        if room_type not in self.capacity:
            self._err(op, "房型不存在: %s" % room_type, order=order, room_type=room_type)
            return
        if order in self.orders:
            self._err(op, "订单已存在，重复预订", order=order, room_type=room_type)
            return
        checkin = parse_date(checkin_s, op, self.errors)
        checkout = parse_date(checkout_s, op, self.errors)
        if checkin is None or checkout is None:
            return
        if checkin >= checkout:
            self._err(op, "入住日必须早于离店日", order=order, room_type=room_type)
            return
        if not self._check_period(op, order, room_type, checkin, checkout):
            return
        self.orders[order] = {"room_type": room_type, "checkin": checkin, "checkout": checkout}
        self._occupy(room_type, checkin, checkout, +1)

    # ---------- 变更流 ----------

    def cancel(self, order):
        op = "cancel"
        if order in self.cancelled:
            self._err(op, "订单已退订，重复退订", order=order)
            return
        rec = self.orders.get(order)
        if rec is None:
            self._err(op, "订单不存在: %s" % order, order=order)
            return
        self._occupy(rec["room_type"], rec["checkin"], rec["checkout"], -1)
        del self.orders[order]
        self.cancelled.add(order)

    def reschedule(self, order, checkin_s, checkout_s):
        op = "reschedule"
        rec = self.orders.get(order)
        if rec is None:
            reason = "订单已退订，不能改期" if order in self.cancelled else "订单不存在: %s" % order
            self._err(op, reason, order=order)
            return
        checkin = parse_date(checkin_s, op, self.errors)
        checkout = parse_date(checkout_s, op, self.errors)
        if checkin is None or checkout is None:
            return
        if checkin >= checkout:
            self._err(op, "改期后入住日必须早于离店日", order=order,
                      room_type=rec["room_type"])
            return
        room_type = rec["room_type"]
        # 先释放旧期间，再校验新期间（新旧期间联动重算，重叠日期不算超卖）
        self._occupy(room_type, rec["checkin"], rec["checkout"], -1)
        if not self._check_period(op, order, room_type, checkin, checkout):
            # 新期间不可订：回滚，保留原订单
            self._occupy(room_type, rec["checkin"], rec["checkout"], +1)
            return
        self.orders[order] = {"room_type": room_type, "checkin": checkin, "checkout": checkout}
        self._occupy(room_type, checkin, checkout, +1)

    # ---------- 输出 ----------

    def report(self):
        lines = []
        lines.append("=" * 56)
        lines.append("每日房态配额")
        lines.append("=" * 56)
        for room_type in sorted(self.capacity):
            cap = self.capacity[room_type]
            lines.append("")
            lines.append("【%s】房间数 %d" % (room_type, cap))
            days = set(self.usage[room_type]) | self.maintenance[room_type]
            if not days:
                lines.append("  （无占用、无维修日）")
                continue
            lines.append("  %-12s %6s %6s %6s  %s" % ("日期", "配额", "已占", "剩余", "备注"))
            for day in sorted(days):
                used = self.usage[room_type].get(day, 0)
                note = "清洁/维修" if day in self.maintenance[room_type] else ""
                lines.append("  %-12s %6d %6d %6d  %s"
                             % (day.isoformat(), cap, used, cap - used, note))
        lines.append("")
        lines.append("=" * 56)
        lines.append("错误清单（共 %d 条）" % len(self.errors))
        lines.append("=" * 56)
        if not self.errors:
            lines.append("  无错误")
        for i, err in enumerate(self.errors, 1):
            parts = ["%d. [%s]" % (i, err["op"])]
            if "order" in err:
                parts.append("订单=%s" % err["order"])
            if "room_type" in err:
                parts.append("房型=%s" % err["room_type"])
            if "date" in err:
                parts.append("日期=%s" % err["date"])
            parts.append(err["reason"])
            lines.append("  " + " ".join(parts))
        return "\n".join(lines)


def run(data):
    inv = Inventory()
    inv.load_room_types(data.get("room_types", []))
    for booking in data.get("bookings", []):
        inv.book(booking.get("order"), booking.get("room_type"),
                 booking.get("checkin"), booking.get("checkout"))
    for change in data.get("changes", []):
        action = change.get("action")
        if action == "cancel":
            inv.cancel(change.get("order"))
        elif action == "reschedule":
            inv.reschedule(change.get("order"), change.get("checkin"), change.get("checkout"))
        else:
            inv.errors.append({"op": "change", "reason": "未知变更动作: %r" % (action,)})
    return inv


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    with open(argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    print(run(data).report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
