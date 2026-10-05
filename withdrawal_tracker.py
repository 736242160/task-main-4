#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兽药休药期 / 转群继承 / 出栏拦截 追踪工具（纯 Python 标准库，单文件）

规则约定（自定规则均在此声明）：
1. 休药期到期日 = 用药日期 + 休药天数；到期日当天及以后可出栏，
   剩余天数 = (到期日 - 当日).days，> 0 则拦截。
2. 同一群重复/叠加用药：到期日取所有用药记录的最大值（即按最长剩余期）。
3. 转群：目标群继承源群全部用药记录（合并后等效于取最长剩余期）；
   源群数量归零即解散，其用药记录随之清空。
4. 混群（多源并入同一目标群）：目标群累积所有来源的用药记录，
   出栏按合并后的最长剩余期拦截。
5. 已解散的群再用药：报错并忽略，须先“建群”重新注册（自定规则）。
6. 检疫不合格的群：出栏一律拦截，直至再次检疫合格。
7. 引用不存在的群或兽药：记入错误清单，事件忽略。
8. 跨流状态延续：所有事件按日期统一排序处理；
   同日优先级：建群 < 检疫 < 用药 < 转群 < 出栏。

输入流格式（每行一条，# 开头为注释）：
  兽药   <兽药名> <休药天数>
  建群   <群号> <日期YYYY-MM-DD> <数量>
  用药   <群号> <兽药名> <日期>
  转群   <源群> <目标群> <日期> <数量>
  检疫   <群号> <日期> <合格|不合格>
  出栏   <群号> <日期> <数量>
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta


# ---------------------------------------------------------------- 数据模型

@dataclass
class MedRecord:
    day: date
    drug: str
    days: int

    @property
    def end(self) -> date:
        return self.day + timedelta(days=self.days)


@dataclass
class Group:
    name: str
    quantity: int
    created: date
    meds: list[MedRecord] = field(default_factory=list)
    dissolved: bool = False
    quarantine_failed: bool = False

    def withdrawal_end(self) -> date | None:
        """合并所有用药记录后的休药期到期日（取最长）。"""
        if not self.meds:
            return None
        return max(m.end for m in self.meds)

    def remaining_days(self, on: date) -> int:
        end = self.withdrawal_end()
        if end is None:
            return 0
        return max(0, (end - on).days)


# ---------------------------------------------------------------- 追踪器

class Tracker:
    def __init__(self) -> None:
        self.drugs: dict[str, int] = {}
        self.groups: dict[str, Group] = {}
        self.errors: list[str] = []     # 错误/拦截清单
        self.logs: list[str] = []       # 正常事件日志

    # ---- 事件处理 ----

    def define_drug(self, name: str, days: int) -> None:
        self.drugs[name] = days

    def register_group(self, name: str, day: date, qty: int) -> None:
        g = self.groups.get(name)
        if g is not None and not g.dissolved:
            self.errors.append(f"{day} 建群 {name}: 群已存在，忽略")
            return
        self.groups[name] = Group(name=name, quantity=qty, created=day)
        self.logs.append(f"{day} 建群 {name} 数量={qty}")

    def medicate(self, group: str, drug: str, day: date) -> None:
        if drug not in self.drugs:
            self.errors.append(f"{day} 用药 {group} {drug}: 兽药未定义，忽略")
            return
        g = self.groups.get(group)
        if g is None:
            self.errors.append(f"{day} 用药 {group}: 群不存在，忽略")
            return
        if g.dissolved:
            self.errors.append(f"{day} 用药 {group}: 群已解散（须重新建群），忽略")
            return
        days = self.drugs[drug]
        g.meds.append(MedRecord(day, drug, days))
        self.logs.append(
            f"{day} 用药 {group} {drug}({days}天) "
            f"-> 该记录到期 {day + timedelta(days=days)}，"
            f"群当前到期日 {g.withdrawal_end()}"
        )

    def transfer(self, src: str, dst: str, day: date, qty: int) -> None:
        s = self.groups.get(src)
        d = self.groups.get(dst)
        if s is None or s.dissolved:
            self.errors.append(f"{day} 转群 {src}->{dst}: 源群不存在或已解散，忽略")
            return
        if d is None or d.dissolved:
            self.errors.append(f"{day} 转群 {src}->{dst}: 目标群不存在或已解散，忽略")
            return
        if qty <= 0 or qty > s.quantity:
            self.errors.append(
                f"{day} 转群 {src}->{dst}: 数量{qty}非法（源群余{s.quantity}），忽略")
            return
        # 目标群继承源群用药记录（合并，等效取最长剩余期）
        d.meds.extend(s.meds)
        if s.quarantine_failed:
            d.quarantine_failed = True  # 检疫不合格随群转移
        s.quantity -= qty
        d.quantity += qty
        self.logs.append(
            f"{day} 转群 {src}->{dst} 数量={qty}，"
            f"{dst} 继承后到期日 {d.withdrawal_end()}"
        )
        if s.quantity == 0:
            s.dissolved = True
            s.meds.clear()
            self.logs.append(f"{day} 群 {src} 数量归零，解散")

    def quarantine(self, group: str, day: date, passed: bool) -> None:
        g = self.groups.get(group)
        if g is None or g.dissolved:
            self.errors.append(f"{day} 检疫 {group}: 群不存在或已解散，忽略")
            return
        g.quarantine_failed = not passed
        self.logs.append(f"{day} 检疫 {group} {'合格' if passed else '不合格'}")

    def slaughter(self, group: str, day: date, qty: int) -> None:
        g = self.groups.get(group)
        if g is None or g.dissolved:
            self.errors.append(f"{day} 出栏 {group}: 群不存在或已解散，拦截")
            return
        if g.quarantine_failed:
            self.errors.append(f"{day} 出栏 {group}: 检疫不合格，拦截")
            return
        rem = g.remaining_days(day)
        if rem > 0:
            self.errors.append(
                f"{day} 出栏 {group}: 休药期内，拦截（剩余 {rem} 天，"
                f"到期日 {g.withdrawal_end()}）"
            )
            return
        if qty <= 0 or qty > g.quantity:
            self.errors.append(
                f"{day} 出栏 {group}: 数量{qty}非法（存栏{g.quantity}），拦截")
            return
        g.quantity -= qty
        self.logs.append(f"{day} 出栏 {group} 数量={qty} 成功，余 {g.quantity}")
        if g.quantity == 0:
            g.dissolved = True
            g.meds.clear()
            self.logs.append(f"{day} 群 {group} 全部出栏，解散")

    # ---- 报告 ----

    def report(self, as_of: date) -> str:
        lines = ["=" * 60, f"养殖状态报告（截至 {as_of}）", "=" * 60]
        lines.append("\n[群状态]")
        for name in sorted(self.groups):
            g = self.groups[name]
            if g.dissolved:
                lines.append(f"  {name}: 已解散")
                continue
            end = g.withdrawal_end()
            end_s = str(end) if end else "无用药"
            rem = g.remaining_days(as_of)
            qua = "检疫不合格" if g.quarantine_failed else "检疫合格"
            ok = "可出栏" if (rem == 0 and not g.quarantine_failed) else "禁止出栏"
            lines.append(
                f"  {name}: 存栏={g.quantity} 到期日={end_s} "
                f"剩余休药={rem}天 {qua} -> {ok}"
            )
        lines.append("\n[事件日志]")
        lines += ["  " + x for x in self.logs] or ["  （无）"]
        lines.append("\n[错误/拦截清单]")
        lines += ["  " + x for x in self.errors] or ["  （无）"]
        return "\n".join(lines)


# ---------------------------------------------------------------- 输入解析

_EVENT_ORDER = {"建群": 0, "检疫": 1, "用药": 2, "转群": 3, "出栏": 4}


def load_lines(lines) -> Tracker:
    t = Tracker()
    deferred = []  # (date, order, seq, kind, args)
    seq = 0
    for ln, raw in enumerate(lines, 1):
        raw = raw.split("#", 1)[0].strip()
        if not raw:
            continue
        p = raw.split()
        kind = p[0]
        try:
            if kind == "兽药":
                t.define_drug(p[1], int(p[2]))
                continue
            # 各流字段顺序不同，逐个解析
            if kind == "建群":
                day = date.fromisoformat(p[2]); args = (p[1], day, int(p[3]))
            elif kind == "用药":
                day = date.fromisoformat(p[3]); args = (p[1], p[2], day)
            elif kind == "转群":
                day = date.fromisoformat(p[3]); args = (p[1], p[2], day, int(p[4]))
            elif kind == "检疫":
                day = date.fromisoformat(p[2]); args = (p[1], day, p[3] == "合格")
            elif kind == "出栏":
                day = date.fromisoformat(p[2]); args = (p[1], day, int(p[3]))
            else:
                t.errors.append(f"第{ln}行: 未知事件类型 {kind!r}，忽略")
                continue
            deferred.append((day, _EVENT_ORDER[kind], seq, kind, args))
            seq += 1
        except (IndexError, ValueError) as e:
            t.errors.append(f"第{ln}行: 格式错误 {raw!r} ({e})，忽略")

    for _, _, _, kind, args in sorted(deferred):
        {"建群": t.register_group, "用药": t.medicate, "转群": t.transfer,
         "检疫": t.quarantine, "出栏": t.slaughter}[kind](*args)

    t.as_of = max((d for d, *_ in deferred), default=date.today())
    return t


# ---------------------------------------------------------------- 演示

DEMO = """
# ---- 兽药定义 ----
兽药 恩诺沙星 7
兽药 磺胺间甲氧 14

# ---- 建群 ----
建群 A 2026-01-01 100
建群 B 2026-01-01 80

# ---- 重复用药叠加：A 到期日 = max(01-01+7, 01-03+14) = 01-17 ----
用药 A 恩诺沙星 2026-01-01
用药 A 磺胺间甲氧 2026-01-03
用药 B 恩诺沙星 2026-01-05

# ---- 转群继承：A 全部并入新群 C 前，先建 C ----
建群 C 2026-01-09 0
转群 A C 2026-01-10 100
# C 继承 A 的休药期，到期日 2026-01-17
出栏 C 2026-01-15 50
# ^ 应被拦截：剩余 2 天
出栏 C 2026-01-17 50
# ^ 到期日当天，允许

# ---- 混群：B 也并入 C，按最长剩余期拦截 ----
转群 B C 2026-01-11 80
# B 的恩诺沙星 01-05+7=01-12 到期，C 本身 01-17 到期，取最长 01-17
出栏 C 2026-01-16 10
# ^ 应被拦截：剩余 1 天

# ---- 解散群再用药：A 已解散 ----
用药 A 恩诺沙星 2026-01-12
# ^ 应报错：群已解散

# ---- 检疫不合格拦截 ----
建群 D 2026-01-01 60
检疫 D 2026-01-20 不合格
出栏 D 2026-01-21 10
# ^ 应被拦截：检疫不合格
检疫 D 2026-01-25 合格
出栏 D 2026-01-26 10
# ^ 复检合格，允许

# ---- 引用不存在的群 / 兽药 ----
用药 Z 恩诺沙星 2026-01-05
用药 B 神秘兽药 2026-01-06
出栏 Z 2026-02-01 5
"""


def main(argv) -> int:
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.readlines()
    else:
        print("（未提供输入文件，运行内置演示；用法: withdrawal_tracker.py <输入文件>）\n")
        lines = DEMO.splitlines()
    t = load_lines(lines)
    print(t.report(t.as_of))
    return 1 if t.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
