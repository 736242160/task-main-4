#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pawnshop.py — 典当行当票管理工具（纯 Python 标准库，单文件）

用法:
    python3 pawnshop.py 脚本文件          # 从文件读指令流
    python3 pawnshop.py < 脚本文件        # 从标准输入读
    python3 pawnshop.py --rate 0.02 脚本  # 指定每期利率（默认 0.02）
    python3 pawnshop.py --demo            # 运行内置样例（重估联动 + 复利）

指令流（每行一条，# 开头为注释，空行忽略；顺序执行，跨流状态延续）:
    ITEM    当品编号 初始估值            定义当品
    REVAL   当品编号 新估值              重估（联动检查在当票）
    PAWN    票号 当品编号 当金 当期(期数)  开当票
    RENEW   票号 期数                    续当（利息复利资本化）
    REDEEM  票号 金额                    赎当（须 >= 当金+累计利息）
    FORFEIT 票号                         死当
    TOPUP   票号 金额                    补足（重估下调后追缴）
    TICK    [期数]                       推进期钟（默认 1），到期未赎未续自动死当

规则（自定部分明示）:
  * 利息: 每期复利，应付 = 计息本金 * (1+rate)^经过期数，金额精确到分(0.01, 四舍五入)。
  * 续当: 先把截至当期的累计利息复利资本化进计息本金，再顺延到期期。
  * 在当金额口径: 票当前应付金额（当金+累计利息）。同一当品同一时刻仅允许一张未结清票。
  * 重估下调: 若在当票应付 > 新估值，报告超出额并生成追缴通知(MARGIN_CALL)，
    须用 TOPUP 补足超出额或 FORFEIT 死当；到期仍未处理随到期级联死当。
  * 到期: TICK 推进后，到期期 < 当前期的未结清票自动级联死当并报告。
  * 死当/赎当后当品释放，可再次典当。
"""
import sys
import shlex
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")

# 当票状态机
ST_ACTIVE = "在当"
ST_RENEWED = "续当"
ST_REDEEMED = "赎当"
ST_FORFEITED = "死当"
OPEN_STATES = (ST_ACTIVE, ST_RENEWED)
# 允许的跳转: 事件 -> 允许的源状态
TRANSITIONS = {
    "RENEW": OPEN_STATES,
    "REDEEM": OPEN_STATES,
    "FORFEIT": OPEN_STATES,
    "TOPUP": OPEN_STATES,
}


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class Ticket:
    def __init__(self, tid, item_id, principal, term, now):
        self.tid = tid
        self.item_id = item_id
        self.principal = money(principal)   # 初始当金
        self.base = money(principal)        # 当前计息本金（续当/补足时重定）
        self.term = term
        self.granted_at = now
        self.settled_at = now               # 利息已结算到哪一期
        self.expiry = now + term            # 到期期
        self.renewals = 0
        self.status = ST_ACTIVE
        self.margin_call = Decimal("0.00")  # 待补足额（重估下调追缴）


class Item:
    def __init__(self, iid, valuation):
        self.iid = iid
        self.valuation = money(valuation)
        self.ticket = None  # 当前未结清票号


class Engine:
    def __init__(self, rate):
        self.rate = Decimal(rate)
        self.now = 0
        self.items = {}
        self.tickets = {}
        self.reports = []

    # ---- 基础 ----
    def report(self, level, msg):
        self.reports.append("[期 %d][%s] %s" % (self.now, level, msg))

    def owed(self, t):
        """票当前应付金额 = 计息本金 * (1+rate)^未结算期数（复利）"""
        elapsed = self.now - t.settled_at
        return money(t.base * ((Decimal(1) + self.rate) ** elapsed))

    def settle(self, t):
        """把累计利息资本化进计息本金"""
        t.base = self.owed(t)
        t.settled_at = self.now

    def release_item(self, t):
        item = self.items.get(t.item_id)
        if item and item.ticket == t.tid:
            item.ticket = None

    def get_open_ticket(self, tid, action):
        t = self.tickets.get(tid)
        if t is None:
            self.report("拒绝", "%s 失败: 票号 %s 不存在" % (action, tid))
            return None
        if t.status not in TRANSITIONS[action]:
            self.report("拦截", "非法跳转: 票 %s 当前状态[%s]，不允许 %s"
                        % (tid, t.status, action))
            return None
        return t

    # ---- 指令 ----
    def do_item(self, iid, valuation):
        if iid in self.items:
            self.report("拒绝", "当品 %s 已存在" % iid)
            return
        self.items[iid] = Item(iid, valuation)
        self.report("受理", "当品 %s 初始估值 %s" % (iid, money(valuation)))

    def do_reval(self, iid, new_val):
        item = self.items.get(iid)
        if item is None:
            self.report("拒绝", "重估失败: 当品 %s 不存在" % iid)
            return
        old = item.valuation
        item.valuation = money(new_val)
        self.report("重估", "当品 %s 估值 %s -> %s" % (iid, old, item.valuation))
        # 联动: 检查在当票是否超新估值
        if item.ticket:
            t = self.tickets[item.ticket]
            due = self.owed(t)
            if due > item.valuation:
                excess = money(due - item.valuation)
                t.margin_call = excess
                self.report("追缴", "重估下调: 票 %s 应付 %s 超当品 %s 新估值 %s，"
                            "超出 %s；级联要求 TOPUP 补足 %s 或 FORFEIT 死当"
                            % (t.tid, due, iid, item.valuation, excess, excess))

    def do_pawn(self, tid, iid, principal, term):
        if tid in self.tickets:
            self.report("拒绝", "开票失败: 票号 %s 已存在" % tid)
            return
        item = self.items.get(iid)
        if item is None:
            self.report("拒绝", "开票失败: 当品 %s 不存在" % iid)
            return
        if item.ticket is not None:
            self.report("拦截", "当品 %s 有未结清票 %s，不可再典当" % (iid, item.ticket))
            return
        principal = money(principal)
        if principal > item.valuation:
            excess = money(principal - item.valuation)
            self.report("拦截", "票 %s 当金 %s 超当品 %s 当前估值 %s，超出 %s，开票拦截"
                        % (tid, principal, iid, item.valuation, excess))
            return
        if term <= 0:
            self.report("拒绝", "开票失败: 当期须为正整数")
            return
        t = Ticket(tid, iid, principal, term, self.now)
        self.tickets[tid] = t
        item.ticket = tid
        self.report("受理", "开票 %s: 当品 %s 当金 %s 当期 %d（到期期 %d）"
                    % (tid, iid, principal, term, t.expiry))

    def do_renew(self, tid, periods):
        t = self.get_open_ticket(tid, "RENEW")
        if t is None:
            return
        if periods <= 0:
            self.report("拒绝", "续当失败: 期数须为正整数")
            return
        self.settle(t)  # 复利资本化
        t.expiry += periods
        t.renewals += 1
        t.status = ST_RENEWED
        self.report("受理", "票 %s 续当 %d 期: 利息资本化后计息本金 %s，新到期期 %d"
                    % (tid, periods, t.base, t.expiry))

    def do_redeem(self, tid, amount):
        t = self.get_open_ticket(tid, "REDEEM")
        if t is None:
            return
        amount = money(amount)
        due = self.owed(t)
        if amount < due:
            self.report("拦截", "票 %s 赎当金额 %s 不足，应付（当金+累计利息）%s，差 %s"
                        % (tid, amount, due, money(due - amount)))
            return
        t.status = ST_REDEEMED
        t.margin_call = Decimal("0.00")
        self.release_item(t)
        extra = "" if amount == due else "（多付 %s 退回）" % money(amount - due)
        self.report("结清", "票 %s 赎当: 实收 %s = 应付 %s%s，当品 %s 释放"
                    % (tid, amount, due, extra, t.item_id))

    def do_forfeit(self, tid, auto=False):
        t = self.get_open_ticket(tid, "FORFEIT")
        if t is None:
            return
        t.status = ST_FORFEITED
        t.margin_call = Decimal("0.00")
        self.release_item(t)
        prefix = "到期未赎未续，级联死当" if auto else "死当"
        self.report("死当", "票 %s %s: 当品 %s 由当铺处置并释放占用（结欠 %s）"
                    % (tid, prefix, t.item_id, self.owed(t)))

    def do_topup(self, tid, amount):
        t = self.get_open_ticket(tid, "TOPUP")
        if t is None:
            return
        amount = money(amount)
        if amount <= 0:
            self.report("拒绝", "补足金额须为正")
            return
        due = self.owed(t)
        pay = min(amount, due)
        t.base = money(due - pay)   # 结算利息并冲减本金
        t.settled_at = self.now
        if t.margin_call > 0:
            t.margin_call = max(Decimal("0.00"), money(t.margin_call - pay))
        tail = "，追缴已清" if t.margin_call == 0 else "，仍欠追缴 %s" % t.margin_call
        self.report("受理", "票 %s 补足 %s: 计息本金降至 %s%s" % (tid, pay, t.base, tail))

    def do_tick(self, n=1):
        self.now += n
        self.report("时钟", "期钟推进到第 %d 期" % self.now)
        for t in list(self.tickets.values()):
            if t.status in OPEN_STATES and t.expiry < self.now:
                self.do_forfeit(t.tid, auto=True)

    # ---- 输出 ----
    def print_final(self, out):
        print("== 事件报告 ==", file=out)
        for line in self.reports:
            print(line, file=out)
        print("\n== 当票状态清单 ==", file=out)
        print("%-6s %-6s %10s %10s %10s %-4s %6s %4s"
              % ("票号", "当品", "初始当金", "计息本金", "当前应付", "状态", "到期期", "续当"), file=out)
        for tid in sorted(self.tickets):
            t = self.tickets[tid]
            due = self.owed(t) if t.status in OPEN_STATES else money(0)
            print("%-6s %-6s %10s %10s %10s %-4s %6d %4d"
                  % (t.tid, t.item_id, t.principal, t.base, due,
                     t.status, t.expiry, t.renewals), file=out)
        print("\n== 当品状态清单 ==", file=out)
        print("%-6s %10s %-6s %-6s %10s" % ("编号", "当前估值", "状态", "在当票", "在当金额"), file=out)
        for iid in sorted(self.items):
            it = self.items[iid]
            if it.ticket:
                t = self.tickets[it.ticket]
                print("%-6s %10s %-6s %-6s %10s"
                      % (iid, it.valuation, "在当", it.ticket, self.owed(t)), file=out)
            else:
                print("%-6s %10s %-6s %-6s %10s" % (iid, it.valuation, "空闲", "-", money(0)), file=out)


DEMO_SCRIPT = """\
# ===== 样例: 重估联动 + 复利 =====
# 利率 2%/期。手算锚点: 8000*1.02^2 = 8323.20; 5000*1.02^3 = 5306.04
ITEM A 10000
ITEM B 5000
PAWN T1 A 8000 3
TICK 2
# 此时 T1 应付 8323.20；重估下调到 8000 -> 超出 323.20，级联追缴
REVAL A 8000
TOPUP T1 323.20
RENEW T1 2
TICK 2
# 续当后本金 8000 又走 2 期复利: 应付 8323.20，足额赎当，当品 A 释放
REDEEM T1 8323.20
# 复利跨续当验证: 5000 押 2 期 -> 续当 1 期 -> 应付 5000*1.02^3 = 5306.04
PAWN T4 A 5000 2
TICK 2
RENEW T4 1
TICK 1
REDEEM T4 5306.04
# 到期未赎未续 -> 级联死当
PAWN T2 B 4000 1
TICK 2
# 异常路径: 重估不存在当品 / 已死当后当品可再当 / 已赎票再赎被拦截 / 超估值开票被拦截
REVAL C 100
PAWN T3 B 100 1
REDEEM T1 1
PAWN T9 A 99999 1
"""


def run_script(engine, lines, out):
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            engine.report("拒绝", "第 %d 行解析失败: %s" % (lineno, exc))
            continue
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "ITEM" and len(args) == 2:
                engine.do_item(args[0], Decimal(args[1]))
            elif cmd == "REVAL" and len(args) == 2:
                engine.do_reval(args[0], Decimal(args[1]))
            elif cmd == "PAWN" and len(args) == 4:
                engine.do_pawn(args[0], args[1], Decimal(args[2]), int(args[3]))
            elif cmd == "RENEW" and len(args) == 2:
                engine.do_renew(args[0], int(args[1]))
            elif cmd == "REDEEM" and len(args) == 2:
                engine.do_redeem(args[0], Decimal(args[1]))
            elif cmd == "FORFEIT" and len(args) == 1:
                engine.do_forfeit(args[0])
            elif cmd == "TOPUP" and len(args) == 2:
                engine.do_topup(args[0], Decimal(args[1]))
            elif cmd == "TICK" and len(args) <= 1:
                engine.do_tick(int(args[0]) if args else 1)
            else:
                engine.report("拒绝", "第 %d 行未知指令或参数个数错误: %s" % (lineno, line))
        except (InvalidOperation, ValueError):
            engine.report("拒绝", "第 %d 行数值非法: %s" % (lineno, line))


def main(argv):
    rate = "0.02"
    demo = False
    path = None
    it = iter(argv[1:])
    for a in it:
        if a == "--rate":
            rate = next(it)
        elif a == "--demo":
            demo = True
        else:
            path = a
    engine = Engine(rate)
    if demo:
        print("== 样例指令流（利率 %s/期）==" % rate)
        print(DEMO_SCRIPT)
        run_script(engine, DEMO_SCRIPT.splitlines(), sys.stdout)
    elif path:
        with open(path, encoding="utf-8") as f:
            run_script(engine, f, sys.stdout)
    else:
        run_script(engine, sys.stdin, sys.stdout)
    engine.print_final(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
