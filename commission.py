#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
层级佣金结算工具（单文件，纯 Python 标准库）

用法:
    python3 commission.py 流文件1 [流文件2 ...]     # 多个流按顺序处理，状态跨流延续
    python3 commission.py -                        # 从标准输入读取
    python3 commission.py --demo                   # 运行内置「折减」与「追回/挂账」示例

流文件格式（行指令，# 为注释）:
    member  <会员> <上级|->                 定义会员及其上级（- 表示无上级）
    ratio   <层级> <比例>                   设置某层级佣金比例，如 ratio 1 0.10
    order   <订单号> <周期> <下单人> <商品> <金额>
    return  <订单号>                        退货，触发逐级追回
    settle  <周期> <资金池预算>             结算某周期，预算不足按比例折减
    withdraw <会员> <金额>                  提现（规则：仅可提已结算余额）
    report                                立即输出当前状态报告

业务规则:
  1. 下单后佣金按层级逐级入池（默认 1级10% 2级5% 3级2%，可用 ratio 调整）。
  2. 佣金比例总和超过 MAX_TOTAL_RATIO(30%) 或单级为负 -> 报告错误。
  3. 结算时资金池不足 -> 所有应付款按同一比例折减，逐条报告折减明细，
     折减部分记为损失不再补发。
  4. 退货后已结算佣金逐级追回：先扣已结算余额，再扣待结算池，
     仍不足（下级已提现）-> 记挂账并报告。
  5. 会员链成环 -> 报告；自购（上级是自己）与互购（互为上级）-> 拦截。
  6. 同一订单重复结算 / 重复退货 / 周期重复结算 -> 拦截并报告。
"""
import sys
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
MAX_LEVEL = 3
MAX_TOTAL_RATIO = Decimal("0.30")
DEFAULT_RATIOS = {1: Decimal("0.10"), 2: Decimal("0.05"), 3: Decimal("0.02")}

PENDING, SETTLED, CANCELLED, CLAWED = "待结算", "已结算", "已取消", "已追回"


def money(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


class Entry:
    """一条佣金流水"""
    __slots__ = ("order_id", "period", "level", "member", "amount", "paid", "state")

    def __init__(self, order_id, period, level, member, amount):
        self.order_id = order_id
        self.period = period
        self.level = level
        self.member = member
        self.amount = amount      # 应发
        self.paid = Decimal("0")  # 实发（折减后）
        self.state = PENDING


class Engine:
    def __init__(self):
        self.ratios = dict(DEFAULT_RATIOS)
        self.members = {}            # id -> upline id or None
        self.orders = {}             # oid -> dict(period, buyer, product, amount, returned)
        self.entries = []            # 全部佣金流水
        self.settled_periods = set()
        self.bal = {}                # member -> {pending, settled, withdrawn, debt}
        self.errors = []
        self.reductions = []         # 折减明细
        self.clawbacks = []          # 追回明细
        self.debts = []              # 挂账明细
        self._check_ratios()

    # ---------- 工具 ----------
    def _err(self, msg, where):
        self.errors.append("[%s] %s" % (where, msg))

    def _acct(self, m):
        return self.bal.setdefault(m, {"pending": Decimal("0"), "settled": Decimal("0"),
                                       "withdrawn": Decimal("0"), "debt": Decimal("0")})

    def _check_ratios(self):
        total = Decimal("0")
        for lv in sorted(self.ratios):
            r = self.ratios[lv]
            if r < 0:
                self.errors.append("[配置] 层级 %d 佣金比例为负: %s" % (lv, r))
            total += r
        if total > MAX_TOTAL_RATIO:
            self.errors.append("[配置] 佣金比例超限: 总和 %s > 上限 %s" % (total, MAX_TOTAL_RATIO))

    def _chain(self, start):
        """向上遍历上级链，返回 [(level, member)]；发现环返回 (None, 环描述)。"""
        seen, chain, cur = set(), [], start
        while cur is not None and len(chain) < MAX_LEVEL + len(self.members) + 1:
            up = self.members.get(cur)
            if up is None:
                break
            if up in seen or up == start:
                loop = " -> ".join(list(seen) + [up])
                return None, "会员链成环: ...%s" % loop
            seen.add(cur)
            chain.append(up)
            if len(chain) >= MAX_LEVEL:
                break
            cur = up
        return list(enumerate(chain, 1)), None

    # ---------- 指令 ----------
    def cmd_member(self, m, up, where):
        if m in self.members:
            self._err("会员重复定义: %s" % m, where)
            return
        up = None if up == "-" else up
        if up == m:
            self._err("自购拦截: 会员 %s 的上级是自己" % m, where)
            up = None
        elif up in self.members and self.members[up] == m:
            self._err("互购拦截: %s 与 %s 互为上级" % (m, up), where)
            up = None
        self.members[m] = up
        self._acct(m)
        # 全量环检测（新边可能成环）
        seen_path, cur = [], m
        while cur is not None:
            if cur in seen_path:
                self._err("会员链成环: %s" % " -> ".join(seen_path + [cur]), where)
                break
            seen_path.append(cur)
            cur = self.members.get(cur)

    def cmd_ratio(self, level, ratio, where):
        try:
            lv = int(level)
            r = Decimal(ratio)
        except Exception:
            self._err("ratio 参数非法: %s %s" % (level, ratio), where)
            return
        if lv < 1 or lv > MAX_LEVEL:
            self._err("层级超出范围(1-%d): %d" % (MAX_LEVEL, lv), where)
            return
        self.ratios[lv] = r
        self._check_ratios()

    def cmd_order(self, oid, period, buyer, product, amount, where):
        if oid in self.orders:
            self._err("订单重复: %s" % oid, where)
            return
        if buyer not in self.members:
            self._err("下单人不存在: %s" % buyer, where)
            return
        if period in self.settled_periods:
            self._err("重复结算拦截: 周期 %s 已结算，拒绝订单 %s 入池" % (period, oid), where)
            return
        try:
            amt = money(amount)
            if amt <= 0:
                raise ValueError
        except Exception:
            self._err("订单金额非法: %s" % amount, where)
            return
        chain, loop = self._chain(buyer)
        if chain is None:
            self._err("订单 %s 拦截: %s" % (oid, loop), where)
            return
        self.orders[oid] = {"period": period, "buyer": buyer, "product": product,
                            "amount": amt, "returned": False}
        for level, m in chain:
            r = self.ratios.get(level, Decimal("0"))
            if r <= 0:
                continue
            e = Entry(oid, period, level, m, money(amt * r))
            self.entries.append(e)
            self._acct(m)["pending"] += e.amount

    def cmd_return(self, oid, where):
        o = self.orders.get(oid)
        if o is None:
            self._err("退货失败: 订单不存在 %s" % oid, where)
            return
        if o["returned"]:
            self._err("重复退货拦截: 订单 %s 已退" % oid, where)
            return
        o["returned"] = True
        for e in [x for x in self.entries if x.order_id == oid]:
            acct = self._acct(e.member)
            if e.state == PENDING:
                e.state = CANCELLED
                acct["pending"] -= e.amount
                self.clawbacks.append("订单%s L%d %s: 取消待结算佣金 %s"
                                      % (oid, e.level, e.member, e.amount))
            elif e.state == SETTLED:
                e.state = CLAWED
                need = e.paid
                take = min(acct["settled"], need)
                acct["settled"] -= take
                need -= take
                src = "已结算余额扣回 %s" % take
                if need > 0:
                    take2 = min(acct["pending"], need)
                    acct["pending"] -= take2
                    need -= take2
                    src += "，待结算池扣回 %s" % take2
                if need > 0:
                    acct["debt"] += need
                    src += "，已提现无法扣回 -> 挂账 %s" % need
                    self.debts.append("会员 %s 挂账 %s（订单%s L%d，佣金已提现）"
                                      % (e.member, need, oid, e.level))
                self.clawbacks.append("订单%s L%d %s: 追回 %s（%s）"
                                      % (oid, e.level, e.member, e.paid, src))

    def cmd_settle(self, period, budget, where):
        if period in self.settled_periods:
            self._err("重复结算拦截: 周期 %s 已结算过" % period, where)
            return
        try:
            budget = money(budget)
            if budget < 0:
                raise ValueError
        except Exception:
            self._err("结算预算非法: %s" % budget, where)
            return
        due = [e for e in self.entries if e.period == period and e.state == PENDING]
        self.settled_periods.add(period)
        if not due:
            self._err("周期 %s 无待结算佣金" % period, where)
            return
        total = sum((e.amount for e in due), Decimal("0"))
        scale = Decimal("1") if budget >= total else budget / total
        for e in due:
            e.paid = money(e.amount * scale)
            e.state = SETTLED
            acct = self._acct(e.member)
            acct["pending"] -= e.amount
            acct["settled"] += e.paid
            if e.paid < e.amount:
                self.reductions.append(
                    "周期%s 订单%s L%d %s: 应发 %s 实发 %s 折减 %s"
                    % (period, e.order_id, e.level, e.member,
                       e.amount, e.paid, e.amount - e.paid))
        if scale < 1:
            self.reductions.append("周期%s 资金池不足: 应付 %s 预算 %s，统一折减系数 %.6f"
                                   % (period, total, budget, scale))

    def cmd_withdraw(self, m, amount, where):
        if m not in self.members:
            self._err("提现失败: 会员不存在 %s" % m, where)
            return
        try:
            amt = money(amount)
            if amt <= 0:
                raise ValueError
        except Exception:
            self._err("提现金额非法: %s" % amount, where)
            return
        acct = self._acct(m)
        if amt > acct["settled"]:
            self._err("提现拦截: %s 可提余额 %s < 请求 %s" % (m, acct["settled"], amt), where)
            return
        acct["settled"] -= amt
        acct["withdrawn"] += amt

    # ---------- 报告 ----------
    def report(self, out=sys.stdout):
        w = out.write
        w("==== 会员佣金池 ====\n")
        w("%-10s %12s %12s %12s %12s\n" % ("会员", "待结算", "已结算", "已提现", "挂账"))
        for m in sorted(self.bal):
            a = self.bal[m]
            w("%-10s %12s %12s %12s %12s\n" % (m, a["pending"], a["settled"],
                                               a["withdrawn"], a["debt"]))
        for title, rows in (("折减明细", self.reductions),
                            ("追回明细", self.clawbacks),
                            ("挂账明细", self.debts),
                            ("错误清单", self.errors)):
            w("==== %s（%d 条）====\n" % (title, len(rows)))
            for r in rows:
                w("  %s\n" % r)
            if not rows:
                w("  （无）\n")

    # ---------- 流处理 ----------
    def feed_line(self, line, where):
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        t = line.split()
        cmd, args = t[0].lower(), t[1:]
        try:
            if cmd == "member" and len(args) == 2:
                self.cmd_member(args[0], args[1], where)
            elif cmd == "ratio" and len(args) == 2:
                self.cmd_ratio(args[0], args[1], where)
            elif cmd == "order" and len(args) == 5:
                self.cmd_order(*args, where)
            elif cmd == "return" and len(args) == 1:
                self.cmd_return(args[0], where)
            elif cmd == "settle" and len(args) == 2:
                self.cmd_settle(args[0], args[1], where)
            elif cmd == "withdraw" and len(args) == 2:
                self.cmd_withdraw(args[0], args[1], where)
            elif cmd == "report" and not args:
                self.report()
            else:
                self._err("无法解析的指令: %s" % line, where)
        except Exception as ex:  # 单条指令异常不中断整个流
            self._err("指令执行异常 %r: %s" % (line, ex), where)

    def feed_text(self, text, source):
        for i, line in enumerate(text.splitlines(), 1):
            self.feed_line(line, "%s:%d" % (source, i))


DEMO_REDUCTION = """
# ===== 示例一：结算资金池不足，按比例折减 =====
member 老总 -
member 大区 老总
member 城市 大区
member 店员 城市
order  D1 2026-09 店员 手机 10000
# 应发: 城市1000 大区500 老总200 共1700，预算只有1000 -> 按比例折减
settle 2026-09 1000
"""

DEMO_CLAWBACK = """
# ===== 示例二：退货追回 + 下级已提现挂账 =====
member 老总 -
member 大区 老总
member 城市 大区
member 店员 城市
order  R1 2026-10 店员 电脑 20000
settle 2026-10 999999
withdraw 城市 2000
# 城市已提现 2000，退货后无法扣回 -> 挂账；大区/老总从未提现余额扣回
return  R1
"""


def main(argv):
    if "--demo" in argv:
        for title, text in (("示例一：折减", DEMO_REDUCTION),
                            ("示例二：追回与挂账", DEMO_CLAWBACK)):
            print("########## %s ##########" % title)
            eng = Engine()
            eng.feed_text(text, "<demo>")
            eng.report()
            print()
        return 0
    files = [a for a in argv[1:]]
    if not files:
        print(__doc__)
        return 0
    eng = Engine()
    for path in files:
        if path == "-":
            eng.feed_text(sys.stdin.read(), "<stdin>")
        else:
            with open(path, encoding="utf-8") as f:
                eng.feed_text(f.read(), path)
    eng.report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
