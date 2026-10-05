#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
转会费摊销与注册联动工具（纯 Python 标准库，单文件）

输入：
  - 球队定义：{球队名: 注册名额}
  - 转会流：TransferEvent(seq, 动作[签约/注册/解除], 球员, 原队, 新队, 转会费, 合同年限, 年度)
  - 摊销流：AmortEvent(seq, 年度, 球员, 金额=None)   金额为 None 时按合同年均额自动计提

自定规则：
  1. 摊销：直线法，年均额 = 转会费 / 合同年限（保留 2 位小数），每次入账后
     剩余摊销额 = 转会费 - 累计已摊销，自动重算；单笔不得超过剩余额，
     累计不得超过转会费总额，否则报错拦截。
  2. 冻结：球员剩余摊销额 > 0 时，对其发起新“签约”即视为未摊销完转会，冻结拦截；
     须先“解除”（解约）或摊销完毕方可再次转会。
  3. 解约级联调整：解除时未摊销余额一次性计入解约年度费用（加速摊销/解约损失），
     剩余摊销额清零，并联动释放其所占注册名额。
  4. 注册：占用“新队”一个名额，名额满则报告还缺几个；同一球员已在一队注册，
     再注册另一队即拦截；解除/再签约自动释放原名额。
  5. 引用不存在的球员或球队一律报错，不影响后续事件（跨流状态按 seq 延续）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")

SIGN, REGISTER, RELEASE = "签约", "注册", "解除"


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 输入事件

@dataclass
class TransferEvent:
    seq: int                      # 跨流排序序号（状态延续依据）
    action: str                   # 签约 / 注册 / 解除
    player: str
    from_team: str | None = None  # 原队
    to_team: str | None = None    # 新队
    fee: object = None            # 转会费（签约时必填）
    years: int | None = None      # 合同年限（签约时必填）
    year: int | None = None       # 解约调整入账年度（解除时可选）


@dataclass
class AmortEvent:
    seq: int
    year: int
    player: str
    amount: object = None         # None = 按合同年均额自动计提（不超过剩余额）


# ---------------------------------------------------------------- 内部状态

@dataclass
class TeamState:
    name: str
    slots: int
    registered: set = field(default_factory=set)


@dataclass
class PlayerState:
    name: str
    team: str
    fee: Decimal
    years: int
    amortized: Decimal = ZERO
    registered_in: str | None = None
    released: bool = False
    postings: list = field(default_factory=list)  # [(标签, 金额)]

    @property
    def remaining(self) -> Decimal:
        return self.fee - self.amortized

    @property
    def annual(self) -> Decimal:
        return money(self.fee / self.years)


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self, team_slots: dict):
        self.teams = {n: TeamState(n, s) for n, s in team_slots.items()}
        self.players: dict[str, PlayerState] = {}
        self.log: list[str] = []

    # 两个流按 seq 归并，保证跨流状态延续
    def run(self, transfer_stream, amort_stream):
        events = sorted(
            list(transfer_stream) + list(amort_stream),
            key=lambda e: e.seq,
        )
        for e in events:
            if isinstance(e, TransferEvent):
                self._transfer(e)
            else:
                self._amortize(e)
        return self

    def _ok(self, seq, msg):
        self.log.append(f"[✓] #{seq} {msg}")

    def _err(self, seq, msg):
        self.log.append(f"[✗] #{seq} 拦截/报错：{msg}")

    # ---- 转会流 ----
    def _transfer(self, e: TransferEvent):
        if e.action == SIGN:
            self._sign(e)
        elif e.action == REGISTER:
            self._register(e)
        elif e.action == RELEASE:
            self._release(e)
        else:
            self._err(e.seq, f"未知动作 {e.action!r}")

    def _sign(self, e: TransferEvent):
        if e.to_team not in self.teams:
            self._err(e.seq, f"新队 {e.to_team!r} 不存在"); return
        if e.from_team is not None and e.from_team not in self.teams:
            self._err(e.seq, f"原队 {e.from_team!r} 不存在"); return
        if e.fee is None or not e.years or e.years <= 0:
            self._err(e.seq, "签约必须给出转会费与正数合同年限"); return
        p = self.players.get(e.player)
        note = ""
        if p is not None:
            if p.remaining > ZERO:
                self._err(e.seq, f"{e.player} 剩余摊销额 {p.remaining} 未摊销完，转会冻结")
                return
            if e.from_team is not None and p.team != e.from_team:
                self._err(e.seq, f"{e.player} 现属 {p.team}，与所述原队 {e.from_team} 不符")
                return
            if p.registered_in is not None:   # 转会联动释放原队名额
                self.teams[p.registered_in].registered.discard(p.name)
                note = f"，原队 {p.registered_in} 注册名额已联动释放"
                p.registered_in = None
            p.team, p.fee, p.years = e.to_team, money(e.fee), e.years
            p.amortized, p.released = ZERO, False
        else:
            self.players[e.player] = PlayerState(
                name=e.player, team=e.to_team, fee=money(e.fee), years=e.years)
        self._ok(e.seq, f"签约：{e.player} {e.from_team}→{e.to_team}，"
                        f"转会费 {money(e.fee)}，合同 {e.years} 年，"
                        f"年均摊销 {money(money(e.fee) / e.years)}{note}")

    def _register(self, e: TransferEvent):
        p = self.players.get(e.player)
        if p is None:
            self._err(e.seq, f"球员 {e.player!r} 不存在"); return
        target = e.to_team or p.team
        if target not in self.teams:
            self._err(e.seq, f"球队 {target!r} 不存在"); return
        if p.registered_in == target:
            self._err(e.seq, f"{e.player} 已注册于 {target}，重复注册"); return
        if p.registered_in is not None:
            self._err(e.seq, f"{e.player} 已注册于 {p.registered_in}，禁止同时注册两队（{target}）")
            return
        if target != p.team:
            self._err(e.seq, f"{e.player} 现属 {p.team}，不能注册到 {target}"); return
        t = self.teams[target]
        if len(t.registered) >= t.slots:
            lack = len(t.registered) + 1 - t.slots
            self._err(e.seq, f"{target} 名额已满（{len(t.registered)}/{t.slots}），"
                             f"注册 {e.player} 还缺 {lack} 个名额")
            return
        t.registered.add(p.name)
        p.registered_in, p.released = target, False
        self._ok(e.seq, f"注册：{e.player} → {target}，占用名额 {len(t.registered)}/{t.slots}")

    def _release(self, e: TransferEvent):
        p = self.players.get(e.player)
        if p is None:
            self._err(e.seq, f"球员 {e.player!r} 不存在"); return
        if e.from_team is not None and e.from_team != p.team:
            self._err(e.seq, f"{e.player} 现属 {p.team}，与所述球队 {e.from_team} 不符"); return
        freed = ""
        if p.registered_in is not None:
            self.teams[p.registered_in].registered.discard(p.name)
            freed = f"，释放 {p.registered_in} 注册名额"
            p.registered_in = None
        adj = ""
        if p.remaining > ZERO:   # 级联调整：未摊销部分一次性计入解约年度
            year = e.year if e.year is not None else "当期"
            p.postings.append((f"{year} 解约级联调整", p.remaining))
            adj = f"，级联调整：未摊销 {p.remaining} 一次性计入 {year} 费用"
            p.amortized = p.fee
        p.released = True
        self._ok(e.seq, f"解除：{e.player} 与 {p.team} 解约{freed}{adj}，剩余摊销额重算为 0")

    # ---- 摊销流 ----
    def _amortize(self, e: AmortEvent):
        p = self.players.get(e.player)
        if p is None:
            self._err(e.seq, f"球员 {e.player!r} 不存在"); return
        amount = money(e.amount) if e.amount is not None else min(p.annual, p.remaining)
        if amount <= ZERO:
            self._err(e.seq, f"{e.player} 摊销金额非正（剩余可摊 {p.remaining}）"); return
        if amount > p.remaining:
            self._err(e.seq, f"摊销超转会费总额：{e.player} 本次 {amount} + 已摊 {p.amortized} "
                             f"= {amount + p.amortized} > 总额 {p.fee}（剩余可摊 {p.remaining}）")
            return
        p.amortized += amount
        p.postings.append((f"{e.year} 年度摊销", amount))
        self._ok(e.seq, f"摊销入账：{e.player} {e.year} 年 {amount}，"
                        f"累计已摊 {p.amortized}/{p.fee}，剩余摊销额重算为 {p.remaining}")

    # ---- 输出 ----
    def report(self) -> str:
        lines = ["=== 事件流水 ===", *self.log, "", "=== 注册状态清单 ==="]
        for t in self.teams.values():
            names = "、".join(sorted(t.registered)) or "（空）"
            lines.append(f"{t.name}: 名额 {len(t.registered)}/{t.slots}，在册：{names}")
        lines += ["", "=== 摊销状态清单 ==="]
        for p in self.players.values():
            status = "已解约" if p.released else ("已注册" if p.registered_in else "已签约未注册")
            lines.append(f"{p.name}: 现属 {p.team}，{status}，转会费 {p.fee} / {p.years} 年，"
                         f"年均 {p.annual}，累计已摊 {money(p.amortized)}，剩余摊销额 {money(p.remaining)}")
            for label, amt in p.postings:
                lines.append(f"    - {label}: {amt}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 样例（含摊销联动）

def demo():
    teams = {"甲队": 2, "乙队": 1, "丙队": 1}

    transfers = [
        TransferEvent(1,  SIGN,    "球员A", "丙队", "甲队", fee=1000, years=5),
        TransferEvent(2,  REGISTER, "球员A", to_team="甲队"),
        TransferEvent(3,  SIGN,    "球员B", "丙队", "甲队", fee=600, years=3),
        TransferEvent(4,  REGISTER, "球员B", to_team="甲队"),
        TransferEvent(5,  SIGN,    "球员C", "丙队", "甲队", fee=300, years=3),
        TransferEvent(6,  REGISTER, "球员C", to_team="甲队"),          # 甲队满，报告缺几个
        TransferEvent(9,  SIGN,    "球员A", "甲队", "乙队", fee=800, years=4),  # 未摊销完→冻结
        TransferEvent(11, RELEASE, "球员A", "甲队", year=2027),        # 解约级联调整+释放名额
        TransferEvent(12, REGISTER, "球员C", to_team="甲队"),          # 名额已释放→成功
        TransferEvent(13, SIGN,    "球员A", "甲队", "乙队", fee=500, years=5),  # 已结清→放行
        TransferEvent(14, REGISTER, "球员A", to_team="乙队"),
        TransferEvent(15, REGISTER, "球员A", to_team="甲队"),          # 同时注册两队→拦截
        TransferEvent(16, SIGN,    "球员D", "甲队", "丁队", fee=100, years=2),  # 球队不存在
        TransferEvent(17, REGISTER, "球员X", to_team="甲队"),          # 球员不存在
    ]
    amortizations = [
        AmortEvent(7,  2026, "球员A"),                 # 自动按年均 200 计提
        AmortEvent(8,  2026, "球员B"),                 # 200
        AmortEvent(10, 2027, "球员A"),                 # 200，剩余 600
        AmortEvent(10, 2027, "球员B"),                 # 200，剩余 200
        AmortEvent(18, 2028, "球员B", amount=500),     # 超剩余额→摊销超总额报错
        AmortEvent(19, 2028, "球员B"),                 # 自动计提 200，剩余归 0
        AmortEvent(20, 2028, "球员X"),                 # 球员不存在
    ]

    engine = Engine(teams).run(transfers, amortizations)
    print(engine.report())


if __name__ == "__main__":
    demo()
