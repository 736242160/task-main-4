#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""层级佣金结算工具（纯 Python 标准库，单文件）。

能力总览：
  * 会员定义：上级关系链，检测成环 / 自购 / 互购 / 上级未定义并拦截
  * 订单流  ：佣金按层级比例逐级入池（比例自定，总和超限要报告）
  * 结算流  ：按周期结算；池内资金不足时按比例折减所有上级并报告折减明细；
              挂账优先从池内冲销
  * 退货流  ：已结算佣金逐级追回；下级已提现部分记挂账并报告
  * 拦截    ：重复订单、重复周期结算、重复退货、金额非法、比例超限
  * 状态    ：跨流延续（订单 -> 结算 -> 退货 -> 再结算共享同一引擎状态）
  * 输出    ：各会员佣金池（待结算 / 已提现 / 挂账）与错误清单

金额一律使用 Decimal 精确到分，杜绝浮点误差。
运行 `python3 commission_settlement.py` 可见折减与追回完整示例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """任意输入转 Decimal（经 str 避免浮点噪声）。"""
    return Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    """精确到分，四舍五入。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Entry:
    """一条佣金入账记录：某订单在某层级给某上级的佣金。"""

    order_id: str
    level: int            # 层级，1 = 直接上级
    recipient: str        # 佣金归属会员
    amount: Decimal       # 应得佣金
    paid: Decimal = ZERO  # 已结算部分（含被挂账冲销的部分）
    recovered: bool = False

    @property
    def unpaid(self) -> Decimal:
        return self.amount - self.paid


@dataclass
class Order:
    order_id: str
    buyer: str
    product: str
    amount: Decimal
    entries: list = field(default_factory=list)
    returned: bool = False


class CommissionEngine:
    """佣金结算引擎：所有流共用同一实例，状态天然跨流延续。"""

    def __init__(self, level_rates, max_total_rate="0.30"):
        # level_rates: 各层级比例，如 ["0.10", "0.05", "0.02"] 表示
        #   直接上级 10%、二级上级 5%、三级上级 2%
        self.level_rates = [D(r) for r in level_rates]
        self.max_total_rate = D(max_total_rate)
        self.members = {}          # 会员名 -> 上级名（None 表示根）
        self.invalid = set()       # 链异常（自购/互购/成环/上级未定义）会员
        self.pools = {}            # 会员 -> 待结算佣金池
        self.withdrawn = {}        # 会员 -> 累计已提现
        self.debts = {}            # 会员 -> 挂账（应追回未追回）
        self.orders = {}           # 订单号 -> Order
        self.member_entries = {}   # 会员 -> [Entry]，按入账先后排列（FIFO）
        self.settled_periods = set()
        self.errors = []           # 错误清单
        self.logs = []             # 过程日志（可 flush 取出）
        self.rates_ok = True
        self._reported_chain_errors = set()
        self._check_rates()

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    def _log(self, msg):
        self.logs.append(msg)

    def _error(self, msg):
        self.errors.append(msg)
        self.logs.append("  [错误] " + msg)

    def flush(self):
        """取出并清空过程日志。"""
        out, self.logs = "\n".join(self.logs), []
        return out

    def _check_rates(self):
        for r in self.level_rates:
            if r < 0:
                self.rates_ok = False
                self._error(f"佣金比例非法：存在负比例 {r}")
        total = sum(self.level_rates, ZERO)
        if total > self.max_total_rate:
            self.rates_ok = False
            self._error(
                f"佣金比例超限：各层级合计 {total} > 上限 {self.max_total_rate}，"
                "所有订单佣金已暂停入账，请修正配置"
            )

    # ------------------------------------------------------------------ #
    # 会员流
    # ------------------------------------------------------------------ #
    def add_member(self, name, upline):
        if name in self.members:
            self._error(f"会员 '{name}' 重复定义，已忽略")
            return
        self.members[name] = upline
        self.pools[name] = ZERO
        self.withdrawn[name] = ZERO
        self.debts[name] = ZERO
        self.member_entries[name] = []
        self._detect_chains()

    def _detect_chains(self):
        """全量重检会员链：自购 / 互购 / 成环 / 上级未定义。"""
        self.invalid = set()
        for start in list(self.members):
            seen, path, node = {}, [], start
            while node is not None and node in self.members and node not in seen:
                seen[node] = len(path)
                path.append(node)
                node = self.members[node]
            if node is None:
                continue  # 正常到达根
            if node not in self.members:
                # 上级未定义：先静默标记，下单/结算时再报，避免定义顺序产生噪音
                self.invalid.update(path)
                continue
            cycle = path[seen[node]:]
            self.invalid.update(path)
            if len(cycle) == 1:
                kind, desc = "self", f"会员 '{cycle[0]}' 的上级是自己（自购）"
            elif len(cycle) == 2:
                kind, desc = "mutual", f"会员 '{cycle[0]}' 与 '{cycle[1]}' 互为上级（互购）"
            else:
                kind, desc = "cycle", "会员链成环：" + " -> ".join(cycle + [cycle[0]])
            key = (kind, tuple(sorted(cycle)))
            if key not in self._reported_chain_errors:
                self._reported_chain_errors.add(key)
                self._error(desc + "，相关会员已拦截")

    # ------------------------------------------------------------------ #
    # 订单流：佣金逐级入池
    # ------------------------------------------------------------------ #
    def place_order(self, order_id, buyer, product, amount):
        amount = cents(D(amount))
        if not self.rates_ok:
            return self._error(f"订单 {order_id} 被拒绝：佣金比例超限，请先修正配置")
        if order_id in self.orders:
            return self._error(f"订单 {order_id} 重复下单/重复结算，已拦截")
        if buyer not in self.members:
            return self._error(f"订单 {order_id} 的下单人 '{buyer}' 不存在")
        if buyer in self.invalid:
            return self._error(
                f"订单 {order_id} 被拦截：下单人 '{buyer}' 会员链异常（自购/互购/成环/上级未定义）"
            )
        if amount <= 0:
            return self._error(f"订单 {order_id} 金额非法：{amount}")

        order = Order(order_id, buyer, product, amount)
        node, level = self.members[buyer], 0
        while node is not None and node in self.members and level < len(self.level_rates):
            if node == buyer:  # 双保险：链异常时佣金绝不发给下单人自己
                return self._error(f"订单 {order_id} 被拦截：佣金链回环到下单人（自购）")
            commission = cents(amount * self.level_rates[level])
            if commission > 0:
                entry = Entry(order_id, level + 1, node, commission)
                order.entries.append(entry)
                self.member_entries[node].append(entry)
                self.pools[node] += commission
            level += 1
            node = self.members[node]
        self.orders[order_id] = order
        detail = ", ".join(f"{e.recipient} L{e.level} +{e.amount}" for e in order.entries)
        self._log(f"订单 {order_id}（{buyer} 购买 {product}，金额 {amount}）佣金入池：{detail}")

    # ------------------------------------------------------------------ #
    # 结算流：资金不足按比例折减
    # ------------------------------------------------------------------ #
    def _allocate(self, name, amount):
        """把已付金额按 FIFO 摊到该会员的佣金记录上（供退货追回判定）。"""
        for entry in self.member_entries[name]:
            if amount <= 0:
                break
            if entry.recovered:
                continue
            take = min(entry.unpaid, amount)
            entry.paid += take
            amount -= take

    def settle(self, period, fund):
        if period in self.settled_periods:
            return self._error(f"周期 {period} 重复结算，已拦截")
        self.settled_periods.add(period)
        fund = cents(D(fund))
        self._log(f"=== 结算周期 {period}，可用资金 {fund} ===")

        # 1) 挂账优先从池内冲销
        for name in self.members:
            if self.debts[name] > 0 and self.pools[name] > 0:
                offset = min(self.debts[name], self.pools[name])
                self.pools[name] -= offset
                self.debts[name] -= offset
                self._allocate(name, offset)
                self._log(f"  挂账冲销：{name} 池内抵扣 {offset}，剩余挂账 {self.debts[name]}")

        # 2) 按比例折减（资金充足则比例为 1）
        total = sum(self.pools.values())
        if total <= 0:
            self._log("  本期无待结算佣金")
            return
        ratio = Decimal("1") if fund >= total else fund / total
        if ratio < 1:
            self._log(f"  资金不足：待结算合计 {total} > 可用 {fund}，"
                      f"折减比例 {ratio:.6f}，所有上级同比例折减")
        for name in self.members:
            due = self.pools[name]
            if due <= 0:
                continue
            pay = cents(due * ratio)
            reduced = due - pay
            self.pools[name] = reduced
            self.withdrawn[name] += pay
            self._allocate(name, pay)
            if reduced > 0:
                self._log(f"  折减明细：{name} 应结 {due}，实发 {pay}，"
                          f"折减 {reduced}（结转下期）")
            else:
                self._log(f"  全额结算：{name} 实发 {pay}")

    # ------------------------------------------------------------------ #
    # 退货流：逐级追回，已提现部分挂账
    # ------------------------------------------------------------------ #
    def return_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            return self._error(f"退货失败：订单 {order_id} 不存在")
        if order.returned:
            return self._error(f"订单 {order_id} 重复退货，已拦截")
        order.returned = True
        self._log(f"=== 退货 {order_id}（{order.buyer} / {order.product} / "
                  f"{order.amount}），逐级追回佣金 ===")
        for entry in order.entries:
            entry.recovered = True
            from_pool, from_debt = entry.unpaid, entry.paid
            if from_pool > 0:
                self.pools[entry.recipient] -= from_pool
                self._log(f"  追回 L{entry.level} {entry.recipient}："
                          f"池内扣回 {from_pool}（未结算部分）")
            if from_debt > 0:
                self.debts[entry.recipient] += from_debt
                self._log(f"  追回 L{entry.level} {entry.recipient}："
                          f"已提现 {from_debt}，记挂账待追回")

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def report(self):
        lines = ["", "===== 各会员佣金池 =====",
                 f"{'会员':<8}{'待结算池':>10}{'累计已提现':>12}{'挂账':>10}  状态"]
        for name in self.members:
            tag = "链异常" if name in self.invalid else "正常"
            lines.append(f"{name:<8}{self.pools[name]:>10}{self.withdrawn[name]:>12}"
                         f"{self.debts[name]:>10}  {tag}")
        lines.append("===== 错误清单 =====")
        if self.errors:
            lines += [f"  [{i}] {e}" for i, e in enumerate(self.errors, 1)]
        else:
            lines.append("  （无）")
        return "\n".join(lines)


# ====================================================================== #
# 示例：折减与追回（含各类拦截）
# ====================================================================== #
def demo_settle_reduce_and_recover():
    print("########## 示例一：资金不足折减 + 退货追回 ##########")
    # 层级比例：一级 10%、二级 5%、三级 2%（合计 17%，上限 30%）
    eng = CommissionEngine(level_rates=["0.10", "0.05", "0.02"], max_total_rate="0.30")
    for name, up in [("公司", None), ("A", "公司"), ("B", "A"), ("C", "B"), ("D", "C"),
                     ("X", "X"),                       # 自购
                     ("Y", "Z"), ("Z", "Y"),           # 互购
                     ("P", "Q"), ("Q", "R"), ("R", "P")]:  # 成环
        eng.add_member(name, up)
    print(eng.flush())

    # 订单流：D 买 1000 -> C+100/B+50/A+20；C 买 2000 -> B+200/A+100/公司+40
    eng.place_order("O1", "D", "商品甲", "1000")
    eng.place_order("O2", "C", "商品乙", "2000")
    eng.place_order("O1", "D", "商品甲", "1000")   # 重复订单 -> 拦截
    eng.place_order("O3", "X", "商品丙", "500")    # 自购会员 -> 拦截
    eng.place_order("O4", "Y", "商品丁", "500")    # 互购会员 -> 拦截
    eng.place_order("O5", "P", "商品戊", "500")    # 成环会员 -> 拦截
    print(eng.flush())

    # 结算流 1：待结算 510，资金仅 100 -> 全体按比例折减
    eng.settle("2026-09", fund="100")
    print(eng.flush())

    # 退货流：O1 部分佣金已结算 -> 未结算部分池内扣回，已提现部分挂账
    eng.return_order("O1")
    print(eng.flush())

    # 结算流 2：资金充足，先冲销挂账再足额结算
    eng.settle("2026-10", fund="10000")
    eng.settle("2026-10", fund="10000")            # 重复结算 -> 拦截
    print(eng.flush())

    # 退货流：O2 佣金已全部提现 -> 全额挂账
    eng.return_order("O2")
    eng.return_order("O2")                         # 重复退货 -> 拦截
    print(eng.flush())

    print(eng.report())


def demo_rate_limit():
    print("\n########## 示例二：佣金比例超限 ##########")
    eng = CommissionEngine(level_rates=["0.20", "0.15", "0.10"],  # 合计 45% > 30%
                           max_total_rate="0.30")
    eng.add_member("公司", None)
    eng.add_member("A", "公司")
    eng.place_order("S1", "A", "商品", "100")      # 比例超限 -> 拒绝入账
    print(eng.flush())
    print(eng.report())


def main():
    demo_settle_reduce_and_recover()
    demo_rate_limit()


if __name__ == "__main__":
    main()
