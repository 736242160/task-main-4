#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期货保证金监控工具（纯 Python 标准库，单文件）。

用法：
    python3 margin_tool.py --demo          # 运行内置盯市/强平样例
    python3 margin_tool.py input.txt       # 从文件读取
    cat input.txt | python3 margin_tool.py # 从标准输入读取

输入每行一条指令，按出现顺序处理，状态跨流延续；'#' 之后为注释：
    CLIENT <名称> <初始保证金>
    POS    <客户> <合约> <数量> <开仓价>   # 数量正=多头，负=空头
    MARK   <客户> <合约> <最新价>          # 盯市（逐日结算）
    TOPUP  <客户> <金额>                   # 追加保证金
    DAY                                    # 进入下一交易日

自定规则：
    1. 逐日盯市：MARK 将 (最新价 - 上次结算价) * 数量 计入客户保证金，
       并把该持仓结算价更新为最新价（开仓时结算价=开仓价）。
    2. 持仓市值 = Σ |数量| * 结算价；维持线 = 市值 * 10%，强平线 = 市值 * 5%。
    3. 保证金 < 维持线：报告催缴；回到维持线以上：报告解除催缴。
    4. 保证金 < 强平线：级联强平——按浮动盈亏（自开仓累计）从差到好逐笔平仓，
       每平一笔重算保证金与阈值，直到回到强平线以上或无仓可平。
    5. 保证金 < 0：报告穿仓（即使强平后仍为负也会报告）。
    6. 盯市/追加/持仓引用不存在的客户，或盯市引用未持有的合约：报告并忽略。
    7. 同一交易日内对同一客户重复追加：报告并忽略第二次起的追加。
"""

import sys
import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAINTENANCE_RATIO = 0.10   # 维持保证金比例
LIQUIDATION_RATIO = 0.05   # 强平线比例


@dataclass
class Position:
    qty: float
    open_price: float
    settle_price: float  # 上次结算价（初始=开仓价）

    def float_pnl(self) -> float:
        """自开仓累计浮动/已结算盈亏。"""
        return (self.settle_price - self.open_price) * self.qty

    def market_value(self) -> float:
        return abs(self.qty) * self.settle_price


@dataclass
class Client:
    name: str
    margin: float
    positions: Dict[str, Position] = field(default_factory=dict)
    call_active: bool = False       # 当前是否处于催缴状态
    negative_reported: bool = False # 穿仓是否已报告（按状态翻转去重）
    last_topup_day: Optional[int] = None


class Ledger:
    def __init__(self) -> None:
        self.clients: Dict[str, Client] = {}
        self.errors: List[str] = []
        self.day: int = 1

    def err(self, lineno: int, msg: str) -> None:
        self.errors.append(f"[第{lineno}行] {msg}")

    # ---- 阈值 ----
    @staticmethod
    def exposure(c: Client) -> float:
        return sum(p.market_value() for p in c.positions.values())

    def lines(self, c: Client):
        exp = self.exposure(c)
        return exp * MAINTENANCE_RATIO, exp * LIQUIDATION_RATIO

    # ---- 指令处理 ----
    def cmd_client(self, lineno: int, parts: List[str]) -> None:
        if len(parts) != 3:
            self.err(lineno, "CLIENT 指令格式错误（应为 CLIENT 名称 初始保证金）")
            return
        name, amount = parts[1], parts[2]
        if name in self.clients:
            self.err(lineno, f"客户 {name} 重复定义，忽略")
            return
        try:
            init = float(amount)
        except ValueError:
            self.err(lineno, f"初始保证金不是数字：{amount}")
            return
        if init < 0:
            self.err(lineno, f"客户 {name} 初始保证金不能为负")
            return
        self.clients[name] = Client(name=name, margin=init)

    def cmd_pos(self, lineno: int, parts: List[str]) -> None:
        if len(parts) != 5:
            self.err(lineno, "POS 指令格式错误（应为 POS 客户 合约 数量 开仓价）")
            return
        _, name, contract, sqty, sprice = parts
        c = self.clients.get(name)
        if c is None:
            self.err(lineno, f"持仓引用不存在的客户：{name}，忽略")
            return
        if contract in c.positions:
            self.err(lineno, f"客户 {name} 已持有合约 {contract}，忽略重复持仓")
            return
        try:
            qty, price = float(sqty), float(sprice)
        except ValueError:
            self.err(lineno, f"数量/价格不是数字：{sqty}, {sprice}")
            return
        if qty == 0 or price <= 0:
            self.err(lineno, f"客户 {name} 持仓数量或价格非法，忽略")
            return
        c.positions[contract] = Position(qty=qty, open_price=price, settle_price=price)
        self.evaluate(lineno, c)

    def cmd_mark(self, lineno: int, parts: List[str]) -> None:
        if len(parts) != 4:
            self.err(lineno, "MARK 指令格式错误（应为 MARK 客户 合约 最新价）")
            return
        _, name, contract, sprice = parts
        c = self.clients.get(name)
        if c is None:
            self.err(lineno, f"盯市引用不存在的客户：{name}，忽略")
            return
        pos = c.positions.get(contract)
        if pos is None:
            self.err(lineno, f"盯市引用客户 {name} 未持有的合约：{contract}，忽略")
            return
        try:
            price = float(sprice)
        except ValueError:
            self.err(lineno, f"最新价不是数字：{sprice}")
            return
        if price <= 0:
            self.err(lineno, f"客户 {name} 合约 {contract} 最新价非法，忽略")
            return
        pnl = (price - pos.settle_price) * pos.qty
        c.margin += pnl
        pos.settle_price = price
        direction = "盈利" if pnl >= 0 else "亏损"
        self.errors.append(
            f"[第{lineno}行] 盯市 {name} {contract} 最新价={price:g}，{direction} {abs(pnl):g}，"
            f"保证金={c.margin:g}"
        )
        self.evaluate(lineno, c)

    def cmd_topup(self, lineno: int, parts: List[str]) -> None:
        if len(parts) != 3:
            self.err(lineno, "TOPUP 指令格式错误（应为 TOPUP 客户 金额）")
            return
        _, name, samt = parts
        c = self.clients.get(name)
        if c is None:
            self.err(lineno, f"追加保证金引用不存在的客户：{name}，忽略")
            return
        try:
            amount = float(samt)
        except ValueError:
            self.err(lineno, f"追加金额不是数字：{samt}")
            return
        if amount <= 0:
            self.err(lineno, f"客户 {name} 追加金额必须为正数，忽略")
            return
        if c.last_topup_day == self.day:
            self.err(lineno, f"客户 {name} 在第 {self.day} 个交易日重复追加保证金 {amount:g}，忽略")
            return
        c.margin += amount
        c.last_topup_day = self.day
        self.errors.append(
            f"[第{lineno}行] 追加 {name} {amount:g}，保证金={c.margin:g}"
        )
        self.evaluate(lineno, c)

    def cmd_day(self, lineno: int, parts: List[str]) -> None:
        if len(parts) != 1:
            self.err(lineno, "DAY 指令不带参数")
            return
        self.day += 1

    # ---- 级联风控评估 ----
    def evaluate(self, lineno: int, c: Client) -> None:
        # 1) 强平级联：保证金低于强平线，先平亏损最大的持仓，平完重算
        while c.positions:
            _, liq_line = self.lines(c)
            if c.margin >= liq_line:
                break
            contract = min(c.positions, key=lambda k: c.positions[k].float_pnl())
            pos = c.positions.pop(contract)
            self.errors.append(
                f"[第{lineno}行] 强平 {c.name} {contract} {pos.qty:g}手 @ {pos.settle_price:g}"
                f"（开仓 {pos.open_price:g}，累计盈亏 {pos.float_pnl():g}），保证金={c.margin:g}"
            )

        # 2) 穿仓：保证金为负
        if c.margin < 0:
            if not c.negative_reported:
                self.errors.append(f"[第{lineno}行] 穿仓！客户 {c.name} 保证金为负：{c.margin:g}")
                c.negative_reported = True
        else:
            c.negative_reported = False

        # 3) 催缴 / 解除催缴
        maint_line, liq_line = self.lines(c)
        if c.positions and liq_line <= c.margin < maint_line:
            if not c.call_active:
                self.errors.append(
                    f"[第{lineno}行] 催缴 {c.name}：保证金 {c.margin:g} < 维持线 {maint_line:g}"
                )
                c.call_active = True
        elif c.call_active:
            self.errors.append(
                f"[第{lineno}行] 解除催缴 {c.name}：保证金 {c.margin:g} >= 维持线 {maint_line:g}"
            )
            c.call_active = False

    # ---- 主循环 ----
    def run(self, lines: List[str]) -> None:
        handlers = {
            "CLIENT": self.cmd_client,
            "POS": self.cmd_pos,
            "MARK": self.cmd_mark,
            "TOPUP": self.cmd_topup,
            "DAY": self.cmd_day,
        }
        for lineno, raw in enumerate(lines, start=1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            handler = handlers.get(parts[0])
            if handler is None:
                self.err(lineno, f"未知指令：{parts[0]}")
                continue
            handler(lineno, parts)

    # ---- 输出 ----
    def report(self) -> str:
        out: List[str] = []
        out.append("=" * 72)
        out.append("客户保证金与持仓状态")
        out.append("=" * 72)
        if not self.clients:
            out.append("（无客户）")
        for c in self.clients.values():
            maint_line, liq_line = self.lines(c)
            if c.margin < 0:
                status = "穿仓"
            elif c.positions and c.margin < liq_line:
                status = "强平风险"
            elif c.positions and c.margin < maint_line:
                status = "待催缴"
            else:
                status = "正常"
            out.append(
                f"- {c.name}：保证金={c.margin:g}，状态={status}，"
                f"持仓市值={self.exposure(c):g}，维持线={maint_line:g}，强平线={liq_line:g}"
            )
            if not c.positions:
                out.append("    持仓：无")
            for contract, p in c.positions.items():
                side = "多" if p.qty > 0 else "空"
                out.append(
                    f"    持仓：{contract} {abs(p.qty):g}手{side}，开仓={p.open_price:g}，"
                    f"现价={p.settle_price:g}，累计盈亏={p.float_pnl():g}"
                )

        out.append("")
        out.append("=" * 72)
        out.append(f"盯市/追加/错误报告（共 {len(self.errors)} 条，按发生顺序）")
        out.append("=" * 72)
        if not self.errors:
            out.append("（无）")
        else:
            out.extend(self.errors)
        return "\n".join(out)


SAMPLE = """
# ===== 期货保证金监控样例：盯市、催缴、追加解除、强平、穿仓、错误引用 =====
CLIENT 张三 1000
CLIENT 李四 1000

DAY
# 张三 1 手多头 @3800；李四 2 手多头 @3800
POS 张三 IF2412 1 3800
POS 李四 IF2412 2 3800

# 盯市：价格跌到 3500。张三亏 300 保证金=700（维持线350，安全）；
# 李四亏 600 保证金=400（市值7000，维持线700）-> 催缴
MARK 张三 IF2412 3500
MARK 李四 IF2412 3500

# 李四追加 500 -> 保证金=900 >= 维持线700，解除催缴
TOPUP 李四 500
# 同一交易日重复追加 -> 报告并忽略
TOPUP 李四 100

# 再次盯市暴跌到 3000：李四再亏 1000 -> 保证金=-100，穿仓并触发级联强平
MARK 李四 IF2412 3000

# 下一交易日张三仍可正常盯市（跨流状态延续）
DAY
MARK 张三 IF2412 3400

# 错误引用：不存在的客户、未持有的合约
MARK 王五 IF2412 3000
MARK 张三 IF9999 3000
TOPUP 王五 1000
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="期货保证金逐日盯市与级联强平工具")
    parser.add_argument("file", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置样例")
    args = parser.parse_args()

    if args.demo:
        lines = SAMPLE.splitlines()
    elif args.file:
        with open(args.file, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    ledger = Ledger()
    ledger.run(lines)
    print(ledger.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
