#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
futures_margin.py — 期货保证金盯市 / 追保 / 强平 / 穿仓 单文件工具（纯标准库）

指令流（每行一条，空白与 # 注释忽略）：
  CONTRACT <合约> <乘数> <保证金率>   可选；缺省 乘数=1 保证金率=0.10
  CLIENT   <客户> <初始保证金>
  POS      <客户> <合约> <数量> <开仓价>   同客户同合约重复开仓按加权平均合并
  MARK     <客户> <合约> <最新价>
  TOPUP    <客户> <金额>              同一结算日内重复追加 -> 错误报告并拒绝
  DAYEND                              日终结算：盯市盈亏入账 -> 风险级联检查
  END                                 输出最终报告（可省略）

自定规则：
  盯市  ：日终按 最新价 与 上一结算价(初始为开仓价) 之差结算盈亏，直接计入保证金
  追保线：权益 < 占用保证金 * 1.00  -> 催缴，缺口 = 占用 - 权益
  强平线：权益 < 占用保证金 * 0.75  -> 先平浮动亏损最大者，直至回到强平线上或无仓可平
  穿仓  ：权益 < 0                  -> 报告
用法：
  python3 futures_margin.py            从标准输入读指令流
  python3 futures_margin.py 输入文件
  python3 futures_margin.py --demo     运行内置盯市+强平样例
"""
import sys

MAINT_RATIO = 1.00   # 追保线 = 占用保证金 * 1.00
LIQ_RATIO = 0.75     # 强平线 = 占用保证金 * 0.75
DEFAULT_MULT = 1.0
DEFAULT_RATE = 0.10


class Contract:
    __slots__ = ("symbol", "mult", "rate")

    def __init__(self, symbol, mult, rate):
        self.symbol, self.mult, self.rate = symbol, mult, rate


class Position:
    __slots__ = ("symbol", "qty", "open_price", "basis", "last")

    def __init__(self, symbol, qty, price):
        self.symbol = symbol
        self.qty = qty
        self.open_price = price
        self.basis = price   # 上一结算价
        self.last = price    # 最新价


class Engine:
    def __init__(self):
        self.clients = {}                 # 客户 -> 保证金权益
        self.contracts = {}               # 合约 -> Contract
        self.positions = {}               # (客户, 合约) -> Position
        self.day = 0
        self.topups = set()               # (客户, 日) 已追加记录
        self.calls = {}                   # 客户 -> 当前催缴缺口
        self.warned_negative = set()      # (客户, 日) 穿仓已报告
        self.errors = []
        self.events = []

    # ---------- 基础量 ----------
    def _contract(self, symbol):
        c = self.contracts.get(symbol)
        if c is None:  # 未显式定义的合约按缺省参数自动登记
            c = self.contracts[symbol] = Contract(symbol, DEFAULT_MULT, DEFAULT_RATE)
        return c

    def required(self, client):
        """占用保证金 = Σ 数量 * 最新价 * 乘数 * 保证金率"""
        total = 0.0
        for (cl, sym), p in self.positions.items():
            if cl == client:
                c = self.contracts[sym]
                total += p.qty * p.last * c.mult * c.rate
        return total

    def client_positions(self, client):
        return [p for (cl, _), p in self.positions.items() if cl == client]

    # ---------- 指令 ----------
    def do_contract(self, symbol, mult, rate):
        self.contracts[symbol] = Contract(symbol, mult, rate)
        self.events.append(f"[登记] 合约 {symbol} 乘数={mult:g} 保证金率={rate:g}")

    def do_client(self, name, amount):
        if name in self.clients:
            self.errors.append(f"第{self.day}日: 重复定义客户 {name}，忽略")
            return
        self.clients[name] = amount
        self.events.append(f"[开户] {name} 初始保证金 {amount:,.2f}")

    def do_pos(self, client, symbol, qty, price):
        if client not in self.clients:
            self.errors.append(f"第{self.day}日: 开仓引用不存在客户 {client}，忽略")
            return
        self._contract(symbol)
        key = (client, symbol)
        if key in self.positions:  # 同合约加仓：加权平均开仓价
            p = self.positions[key]
            p.open_price = (p.open_price * p.qty + price * qty) / (p.qty + qty)
            p.qty += qty
            p.basis = p.last
        else:
            self.positions[key] = Position(symbol, qty, price)
        self.events.append(f"[开仓] {client} {symbol} {qty:g}手 @{price:,.2f}")

    def do_mark(self, client, symbol, price):
        if client not in self.clients:
            self.errors.append(f"第{self.day}日: 盯市引用不存在客户 {client}，忽略")
            return
        if (client, symbol) not in self.positions:
            self.errors.append(
                f"第{self.day}日: 盯市引用不存在持仓/合约 {client}/{symbol}，忽略")
            return
        self.positions[(client, symbol)].last = price
        self.events.append(f"[盯市] {client} {symbol} 最新价 {price:,.2f}")

    def do_topup(self, client, amount):
        if client not in self.clients:
            self.errors.append(f"第{self.day}日: 追加引用不存在客户 {client}，忽略")
            return
        if (client, self.day) in self.topups:
            self.errors.append(
                f"第{self.day}日: 客户 {client} 同一结算时段重复追加，拒绝本次 {amount:,.2f}")
            return
        self.topups.add((client, self.day))
        self.clients[client] += amount
        eq, req = self.clients[client], self.required(client)
        self.events.append(
            f"[追加] {client} +{amount:,.2f} -> 权益 {eq:,.2f}（占用 {req:,.2f}）")
        if client in self.calls and eq >= req * MAINT_RATIO:
            del self.calls[client]
            self.events.append(f"[解除] {client} 追加后权益已回到追保线以上，强平风险解除")
        elif eq < req * MAINT_RATIO:
            self.events.append(
                f"[提示] {client} 追加后仍低于追保线，缺口 {req * MAINT_RATIO - eq:,.2f}")

    # ---------- 日终结算与风险级联 ----------
    def do_dayend(self):
        self.day += 1
        self.events.append(f"===== 第{self.day}日 日终结算 =====")
        # 1) 逐日盯市：盈亏 = (最新价 - 上一结算价) * 数量 * 乘数，计入保证金
        for client in self.clients:
            pnl = 0.0
            for p in self.client_positions(client):
                c = self.contracts[p.symbol]
                pnl += (p.last - p.basis) * p.qty * c.mult
                p.basis = p.last
            if pnl:
                self.clients[client] += pnl
                self.events.append(
                    f"[结算] {client} 盯市盈亏 {pnl:+,.2f} -> 权益 {self.clients[client]:,.2f}")
            else:
                self.events.append(f"[结算] {client} 无盯市盈亏，权益 {self.clients[client]:,.2f}")
        # 2) 风险级联：强平 -> 催缴 -> 穿仓
        for client in self.clients:
            self._risk_check(client)

    def _risk_check(self, client):
        eq = self.clients[client]
        req = self.required(client)
        if req > 0 and eq < req * LIQ_RATIO:
            self.events.append(
                f"[强平] {client} 权益 {eq:,.2f} 低于强平线 {req * LIQ_RATIO:,.2f}，启动强制平仓")
            self._liquidate(client)
            eq, req = self.clients[client], self.required(client)
        if req > 0 and eq < req * MAINT_RATIO:
            gap = req * MAINT_RATIO - eq
            self.calls[client] = gap
            self.events.append(
                f"[催缴] {client} 权益 {eq:,.2f} 低于追保线 {req * MAINT_RATIO:,.2f}，"
                f"请追加保证金 {gap:,.2f}")
        elif client in self.calls and eq >= req * MAINT_RATIO:
            del self.calls[client]
            self.events.append(f"[解除] {client} 权益已回到追保线以上")
        if eq < 0 and (client, self.day) not in self.warned_negative:
            self.warned_negative.add((client, self.day))
            self.errors.append(
                f"第{self.day}日: 客户 {client} 穿仓！保证金为负 {eq:,.2f}，请立即上报")

    def _liquidate(self, client):
        """先平浮动亏损最大者；均盈利时平占用保证金最大者；直至回到强平线上。"""
        while True:
            eq = self.clients[client]
            req = self.required(client)
            poss = self.client_positions(client)
            if not poss or eq >= req * LIQ_RATIO:
                break
            def unrealized(p):
                c = self.contracts[p.symbol]
                return (p.last - p.open_price) * p.qty * c.mult
            losers = sorted((p for p in poss if unrealized(p) < 0),
                            key=unrealized)
            if losers:
                victim = losers[0]
            else:  # 无亏损持仓：平占用保证金最大的
                victim = max(poss, key=lambda p: p.qty * p.last
                             * self.contracts[p.symbol].mult
                             * self.contracts[p.symbol].rate)
            c = self.contracts[victim.symbol]
            freed = victim.qty * victim.last * c.mult * c.rate
            # 日终已盯市结算，平仓价=最新价，无额外盈亏，仅释放占用
            del self.positions[(client, victim.symbol)]
            self.events.append(
                f"[强平] 平仓 {client} {victim.symbol} {victim.qty:g}手 @{victim.last:,.2f}"
                f"（浮动盈亏 {unrealized(victim):+,.2f}，释放占用 {freed:,.2f}）")
            eq, req = self.clients[client], self.required(client)
            self.events.append(
                f"[重算] {client} 强平后权益 {eq:,.2f}，占用 {req:,.2f}，"
                f"强平线 {req * LIQ_RATIO:,.2f}")

    # ---------- 最终报告 ----------
    def report(self):
        out = ["", "========== 客户保证金与持仓状态 =========="]
        for name in self.clients:
            eq, req = self.clients[name], self.required(name)
            if eq < 0:
                status = "穿仓"
            elif req > 0 and eq < req * LIQ_RATIO:
                status = "强平风险"
            elif req > 0 and eq < req * MAINT_RATIO:
                status = "追保中"
            else:
                status = "正常"
            out.append(f"{name}: 保证金 {eq:,.2f} | 占用 {req:,.2f} | 状态 {status}")
            for p in self.client_positions(name):
                c = self.contracts[p.symbol]
                fl = (p.last - p.open_price) * p.qty * c.mult
                out.append(
                    f"    持仓 {p.symbol} {p.qty:g}手 开仓@{p.open_price:,.2f} "
                    f"最新@{p.last:,.2f} 浮动盈亏 {fl:+,.2f}")
        out.append("========== 错误清单 ==========")
        if self.errors:
            out.extend(f"  {i}. {e}" for i, e in enumerate(self.errors, 1))
        else:
            out.append("  （无）")
        return "\n".join(out)


DEMO = """\
# ===== 内置样例：盯市 + 追保 + 追加 + 强平 + 穿仓 =====
CONTRACT CU 1 0.10
CONTRACT AL 1 0.10
CLIENT 张三 100000
CLIENT 李四 30000
POS 张三 CU 10 70000
POS 张三 AL 5 20000
POS 李四 CU 5 60000
# --- 第1日：CU 跌到 66000，AL 涨到 21000 ---
MARK 张三 CU 66000
MARK 张三 AL 21000
MARK 李四 CU 66000
DAYEND
# --- 张三被催缴后追加，同日重复追加应报错 ---
TOPUP 张三 12000
TOPUP 张三 5000
# --- 第2日：CU 继续暴跌，张三触发强平，李四穿仓 ---
MARK 张三 CU 60000
MARK 张三 AL 20500
MARK 李四 CU 52000
DAYEND
# --- 错误引用样例 ---
MARK 张三 XX999 1
TOPUP 王五 1000
MARK 王五 CU 60000
END
"""


def run(lines):
    eng = Engine()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "CONTRACT":
                eng.do_contract(args[0], float(args[1]), float(args[2]))
            elif cmd == "CLIENT":
                eng.do_client(args[0], float(args[1]))
            elif cmd == "POS":
                eng.do_pos(args[0], args[1], float(args[2]), float(args[3]))
            elif cmd == "MARK":
                eng.do_mark(args[0], args[1], float(args[2]))
            elif cmd == "TOPUP":
                eng.do_topup(args[0], float(args[1]))
            elif cmd == "DAYEND":
                eng.do_dayend()
            elif cmd == "END":
                break
            else:
                eng.errors.append(f"第{lineno}行: 未知指令 {cmd}，忽略")
        except (IndexError, ValueError):
            eng.errors.append(f"第{lineno}行: 指令格式错误: {line}，忽略")
    return eng


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    eng = run(lines)
    print("\n".join(eng.events))
    print(eng.report())


if __name__ == "__main__":
    main(sys.argv)
