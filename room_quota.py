#!/usr/bin/env python3
"""客房配额改期仲裁器（纯标准库，单文件）。

输入格式（行式文本，# 开头为注释，字段以空白分隔）：

    ROOM   <房型> <房间数>                      房型定义（可重复定义，后者覆盖）
    BOOK   <订单> <房型> <入住日> <离店日>       预订流
    CHANGE <订单> <新入住日> <新离店日>          变更流：改期
    CANCEL <订单>                              变更流：退订
    BLOCK  <房型> <起始日> <结束日> <原因>       清洁/维修封锁（该期间不可预订）

日期格式 YYYY-MM-DD。占用规则：占用 [入住日, 离店日) 的每一天，
离店日当天不占配额。每日配额 = 房间数，预订每夜占 1 配额，
清洁/维修封锁每夜占 1 配额。

用法：
    python3 room_quota.py [输入文件]     省略输入文件时运行内置演示样例
    python3 room_quota.py - < input.txt  从标准输入读取
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict


def parse_date(text, where, errors):
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        errors.append("%s: 非法日期 %r（应为 YYYY-MM-DD）" % (where, text))
        return None


class Order:
    __slots__ = ("order_id", "room_type", "checkin", "checkout", "active")

    def __init__(self, order_id, room_type, checkin, checkout):
        self.order_id = order_id
        self.room_type = room_type
        self.checkin = checkin
        self.checkout = checkout
        self.active = True


def nights(checkin, checkout):
    """生成 [checkin, checkout) 的每一天。"""
    day = checkin
    while day < checkout:
        yield day
        day += dt.timedelta(days=1)


class Hotel:
    def __init__(self):
        self.rooms = {}                       # 房型 -> 房间数
        self.orders = {}                      # 订单号 -> Order
        self.blocks = defaultdict(list)       # 房型 -> [(起始日, 结束日, 原因)]
        self.errors = []                      # 错误清单（字符串）

    # ---- 占用查询 -------------------------------------------------------

    def used_on(self, room_type, day, ignore_order=None):
        """某房型某日已被有效预订占用的配额（可排除某订单自身）。"""
        count = 0
        for order in self.orders.values():
            if not order.active or order.room_type != room_type:
                continue
            if ignore_order is not None and order.order_id == ignore_order:
                continue
            if order.checkin <= day < order.checkout:
                count += 1
        return count

    def blocked_on(self, room_type, day):
        """某房型某日因清洁/维修被封锁的配额及原因。"""
        reasons = []
        for start, end, reason in self.blocks.get(room_type, []):
            if start <= day < end:
                reasons.append(reason)
        return reasons

    def find_conflict(self, room_type, checkin, checkout, ignore_order=None):
        """检查 [checkin, checkout) 是否可订。

        返回 None 表示可订；否则返回 (日期, 说明) 列表，定位到具体日期。
        """
        if room_type not in self.rooms:
            return None  # 房型不存在由调用方单独报告
        quota = self.rooms[room_type]
        conflicts = []
        for day in nights(checkin, checkout):
            reasons = self.blocked_on(room_type, day)
            if reasons:
                conflicts.append((day, "清洁/维修封锁（%s）" % "、".join(reasons)))
                continue
            used = self.used_on(room_type, day, ignore_order=ignore_order)
            if used + 1 > quota:
                conflicts.append((day, "超配额：当日已占 %d/%d" % (used, quota)))
        return conflicts or None

    @staticmethod
    def _fmt_conflicts(room_type, conflicts):
        parts = ["%s(%s)" % (day.isoformat(), why) for day, why in conflicts]
        return "房型[%s] 冲突日期: %s" % (room_type, "；".join(parts))

    # ---- 指令处理 -------------------------------------------------------

    def cmd_room(self, where, name, count_text):
        try:
            count = int(count_text)
        except ValueError:
            self.errors.append("%s: 房间数 %r 不是整数" % (where, count_text))
            return
        if count <= 0:
            self.errors.append("%s: 房间数必须为正整数，得到 %d" % (where, count))
            return
        self.rooms[name] = count

    def cmd_book(self, where, order_id, room_type, ci_text, co_text):
        checkin = parse_date(ci_text, where, self.errors)
        checkout = parse_date(co_text, where, self.errors)
        if checkin is None or checkout is None:
            return
        if checkout <= checkin:
            self.errors.append("%s: 离店日 %s 必须晚于入住日 %s"
                               % (where, co_text, ci_text))
            return
        if order_id in self.orders and self.orders[order_id].active:
            self.errors.append("%s: 订单 %s 已存在且有效，重复预订被拦截"
                               % (where, order_id))
            return
        if room_type not in self.rooms:
            self.errors.append("%s: 订单 %s 引用了不存在的房型[%s]，预订被拦截"
                               % (where, order_id, room_type))
            return
        conflicts = self.find_conflict(room_type, checkin, checkout)
        if conflicts:
            self.errors.append("%s: 订单 %s 预订被拦截，%s"
                               % (where, order_id,
                                  self._fmt_conflicts(room_type, conflicts)))
            return
        self.orders[order_id] = Order(order_id, room_type, checkin, checkout)

    def cmd_change(self, where, order_id, ci_text, co_text):
        order = self.orders.get(order_id)
        if order is None:
            self.errors.append("%s: 改期引用了不存在的订单 %s，被拦截"
                               % (where, order_id))
            return
        if not order.active:
            self.errors.append("%s: 订单 %s 已退订，不能改期，被拦截"
                               % (where, order_id))
            return
        checkin = parse_date(ci_text, where, self.errors)
        checkout = parse_date(co_text, where, self.errors)
        if checkin is None or checkout is None:
            return
        if checkout <= checkin:
            self.errors.append("%s: 改期后离店日 %s 必须晚于入住日 %s"
                               % (where, co_text, ci_text))
            return
        # 排除本订单自身占用后校验新期间；失败则保留原预订不变。
        conflicts = self.find_conflict(order.room_type, checkin, checkout,
                                       ignore_order=order_id)
        if conflicts:
            self.errors.append(
                "%s: 订单 %s 改期被拦截（保留原期间 %s~%s），%s"
                % (where, order_id, order.checkin.isoformat(),
                   order.checkout.isoformat(),
                   self._fmt_conflicts(order.room_type, conflicts)))
            return
        order.checkin, order.checkout = checkin, checkout

    def cmd_cancel(self, where, order_id):
        order = self.orders.get(order_id)
        if order is None:
            self.errors.append("%s: 退订引用了不存在的订单 %s，被拦截"
                               % (where, order_id))
            return
        if not order.active:
            self.errors.append("%s: 订单 %s 重复退订，被拦截" % (where, order_id))
            return
        order.active = False  # 释放配额，后续重算自动生效

    def cmd_block(self, where, room_type, start_text, end_text, reason):
        start = parse_date(start_text, where, self.errors)
        end = parse_date(end_text, where, self.errors)
        if start is None or end is None:
            return
        if end <= start:
            self.errors.append("%s: 封锁结束日 %s 必须晚于起始日 %s"
                               % (where, end_text, start_text))
            return
        if room_type not in self.rooms:
            self.errors.append("%s: 封锁引用了不存在的房型[%s]" % (where, room_type))
            return
        self.blocks[room_type].append((start, end, reason or "未注明"))

    # ---- 驱动与报告 -----------------------------------------------------

    def run(self, lines):
        handlers = {
            "ROOM": (self.cmd_room, 3),
            "BOOK": (self.cmd_book, 5),
            "CHANGE": (self.cmd_change, 4),
            "CANCEL": (self.cmd_cancel, 2),
            "BLOCK": (self.cmd_block, 5),
        }
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            keyword = parts[0].upper()
            entry = handlers.get(keyword)
            where = "第%d行" % lineno
            if entry is None:
                self.errors.append("%s: 无法识别的指令 %r" % (where, parts[0]))
                continue
            handler, arity = entry
            args = parts[1:]
            # BLOCK 的原因可含空格，把多余字段并入最后一个参数。
            if keyword == "BLOCK" and len(args) > arity - 1:
                args = args[:arity - 2] + [" ".join(args[arity - 2:])]
            if len(args) != arity - 1:
                self.errors.append("%s: 指令 %s 需要 %d 个字段，得到 %d 个"
                                   % (where, keyword, arity - 1, len(args)))
                continue
            handler(where, *args)

    def report(self):
        out = []
        out.append("=" * 60)
        out.append("每日房态配额（占用/配额，仅列出有占用或封锁的日期）")
        out.append("=" * 60)
        for room_type in sorted(self.rooms):
            quota = self.rooms[room_type]
            days = set()
            for order in self.orders.values():
                if order.active and order.room_type == room_type:
                    days.update(nights(order.checkin, order.checkout))
            for start, end, _ in self.blocks.get(room_type, []):
                days.update(nights(start, end))
            out.append("房型[%s] 房间数=%d" % (room_type, quota))
            if not days:
                out.append("  （无占用记录）")
            for day in sorted(days):
                used = self.used_on(room_type, day)
                marks = []
                for reason in self.blocked_on(room_type, day):
                    marks.append("封锁:%s" % reason)
                status = "占用 %d/%d" % (used, quota)
                if used > quota:
                    status += " 【超卖！】"
                suffix = ("  " + "、".join(marks)) if marks else ""
                out.append("  %s  %s%s" % (day.isoformat(), status, suffix))
        out.append("")
        out.append("=" * 60)
        out.append("有效订单（最终状态）")
        out.append("=" * 60)
        active = [o for o in self.orders.values() if o.active]
        if not active:
            out.append("（无）")
        for order in sorted(active, key=lambda o: o.order_id):
            out.append("  %s  房型[%s]  %s ~ %s"
                       % (order.order_id, order.room_type,
                          order.checkin.isoformat(), order.checkout.isoformat()))
        out.append("")
        out.append("=" * 60)
        out.append("错误清单（共 %d 条）" % len(self.errors))
        out.append("=" * 60)
        if not self.errors:
            out.append("（无错误）")
        for i, err in enumerate(self.errors, 1):
            out.append("  %2d. %s" % (i, err))
        return "\n".join(out)


DEMO_INPUT = """\
# ---- 配额重算样例：覆盖预订/退订/改期/封锁/错误定位 ----
ROOM 大床房 2
ROOM 双床房 1

# 预订流
BOOK O1 大床房 2026-10-01 2026-10-03
BOOK O2 大床房 2026-10-02 2026-10-04
BOOK O3 大床房 2026-10-02 2026-10-03
BOOK O4 双床房 2026-10-01 2026-10-05
BOOK O5 双床房 2026-10-02 2026-10-03
BOOK O6 海景房 2026-10-01 2026-10-02
BOOK O7 大床房 2026-10-03 2026-10-04

# 变更流（状态自预订流延续）
CANCEL O1
BOOK O8 大床房 2026-10-01 2026-10-03
CANCEL O1
CANCEL O9
CHANGE O2 2026-10-03 2026-10-05
CHANGE O7 2026-10-02 2026-10-03
CHANGE O7 2026-10-04 2026-10-05
CHANGE O8 2026-10-04 2026-10-05
BLOCK 大床房 2026-10-10 2026-10-12 深度清洁
BOOK O10 大床房 2026-10-09 2026-10-11
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="客房配额改期仲裁器")
    parser.add_argument("input", nargs="?",
                        help="输入文件路径；'-' 表示标准输入；省略则运行内置演示样例")
    args = parser.parse_args(argv)

    if args.input is None:
        print("（未指定输入文件，运行内置演示样例）\n")
        lines = DEMO_INPUT.splitlines()
    elif args.input == "-":
        lines = sys.stdin.read().splitlines()
    else:
        with open(args.input, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    hotel = Hotel()
    hotel.run(lines)
    print(hotel.report())
    return 1 if hotel.errors else 0


if __name__ == "__main__":
    sys.exit(main())
