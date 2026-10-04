#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
causal_replay.py — 乱序事件流的因果重放账务工具（纯 Python 标准库，单文件）

输入格式（行式文本，`#` 之后为注释）：

    DEF <序号> DEPOSIT  <账户> <金额> [DEPS <前置序号>...]
    DEF <序号> TRANSFER <源账户> <目标账户> <金额> [DEPS <前置序号>...]
    DEF <序号> REVERSAL <被冲正事件序号> [DEPS <前置序号>...]
    STREAM <序号> <序号> ...        # 事件实际到达顺序（可乱序 / 重复 / 缺失）

语义约定：
  * DEPOSIT  入账：账户不存在则开户并贷记；存在则累加。
  * TRANSFER 转账：源/目标账户都必须已存在，否则报错且事件不生效。
  * REVERSAL 冲正：撤销目标事件（DEPOSIT/TRANSFER）的账务效果；
    冲正隐式依赖被冲正事件（自动加入前置依赖，无需手工声明）。
  * 金额为正整数（最小货币单位），允许账户出现负余额（不额外拦截）。

用法：
    python3 causal_replay.py input.txt     # 从文件读取定义与事件流
    python3 causal_replay.py --demo        # 运行内置演示（覆盖全部错误场景）
"""

from __future__ import annotations

import argparse
import heapq
import sys
from collections import defaultdict
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 事件定义与解析
# ---------------------------------------------------------------------------

@dataclass
class EventDef:
    seq: int                 # 事件序号（全局唯一）
    kind: str                # 'DEPOSIT' | 'TRANSFER' | 'REVERSAL'
    args: tuple              # DEPOSIT:(账户,金额) TRANSFER:(源,目标,金额) REVERSAL:(目标序号,)
    deps: tuple = ()         # 显式声明的前置事件序号


class DefError(Exception):
    """事件定义行解析错误。"""


class ApplyError(Exception):
    """事件重放（落账）阶段的业务错误。"""


def _amount(token: str) -> int:
    try:
        value = int(token)
    except ValueError:
        raise DefError(f"金额 {token!r} 不是整数")
    if value <= 0:
        raise DefError(f"金额必须为正整数，得到 {value}")
    return value


def _parse_def(tokens: list) -> EventDef:
    if len(tokens) < 2:
        raise DefError("DEF 至少需要 <序号> <类型>")
    try:
        seq = int(tokens[0])
    except ValueError:
        raise DefError(f"序号 {tokens[0]!r} 不是整数")
    kind = tokens[1].upper()
    rest = tokens[2:]

    deps: tuple = ()
    upper = [t.upper() for t in rest]
    if "DEPS" in upper:
        idx = upper.index("DEPS")
        dep_tokens, rest = rest[idx + 1:], rest[:idx]
        try:
            deps = tuple(int(t) for t in dep_tokens)
        except ValueError:
            raise DefError("DEPS 前置序号必须是整数")

    if kind == "DEPOSIT":
        if len(rest) != 2:
            raise DefError("DEPOSIT 需要 <账户> <金额>")
        args = (rest[0], _amount(rest[1]))
    elif kind == "TRANSFER":
        if len(rest) != 3:
            raise DefError("TRANSFER 需要 <源账户> <目标账户> <金额>")
        args = (rest[0], rest[1], _amount(rest[2]))
    elif kind == "REVERSAL":
        if len(rest) != 1:
            raise DefError("REVERSAL 需要 <被冲正事件序号>")
        try:
            args = (int(rest[0]),)
        except ValueError:
            raise DefError("REVERSAL 目标序号必须是整数")
    else:
        raise DefError(f"未知事件类型 {kind!r}（支持 DEPOSIT/TRANSFER/REVERSAL）")
    return EventDef(seq, kind, args, deps)


def parse_input(text: str):
    """解析完整输入，返回 (defs, stream, parse_errors)。"""
    defs: dict = {}
    stream: list = []
    errors: list = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        directive = tokens[0].upper()
        if directive == "DEF":
            try:
                evt = _parse_def(tokens[1:])
            except DefError as exc:
                errors.append(f"第{lineno}行: {exc}")
                continue
            if evt.seq in defs:
                errors.append(f"第{lineno}行: 事件定义序号 {evt.seq} 重复，已忽略后者")
                continue
            defs[evt.seq] = evt
        elif directive == "STREAM":
            for tok in tokens[1:]:
                try:
                    stream.append(int(tok))
                except ValueError:
                    errors.append(f"第{lineno}行: 事件流序号 {tok!r} 不是整数")
        else:
            errors.append(f"第{lineno}行: 无法识别的指令 {tokens[0]!r}")
    return defs, stream, errors


# ---------------------------------------------------------------------------
# 环检测：Tarjan 强连通分量（迭代实现，避免递归深度限制）
# ---------------------------------------------------------------------------

def find_cycle_members(graph: dict) -> list:
    """graph: node -> [node...]。返回所有处于依赖环上的节点（含自环）。"""
    index_of: dict = {}
    lowlink: dict = {}
    on_stack: set = set()
    stack: list = []
    counter = 0
    cyclic: set = set()

    for root in graph:
        if root in index_of:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                if len(scc) > 1:
                    cyclic.update(scc)
                elif node in graph.get(node, ()):  # 自环
                    cyclic.add(node)
    return sorted(cyclic)


# ---------------------------------------------------------------------------
# 因果重放引擎
# ---------------------------------------------------------------------------

class Replayer:
    """按因果顺序重放事件流并维护账务状态。

    调度模型：
      * arrived  —— 已从事件流到达（重复到达被拦截）
      * pending  —— 已到达但前置依赖未全部了结，挂起等待
      * ready    —— 依赖已满足，进入最小堆按序号确定性地弹出重放
      * applied / failed —— 已生效 / 已失败（两者都视为"已了结"，可解除下游阻塞）
    """

    def __init__(self, defs: dict):
        self.defs = defs
        self.deps: dict = {}              # seq -> 有效依赖集合（含冲正隐式依赖）
        self.balances: dict = {}
        self.effects: dict = {}           # 已生效事件 seq -> 效果记录（供冲正反向执行）
        self.reversed_targets: set = set()
        self.arrived: set = set()
        self.applied: set = set()
        self.failed: set = set()
        self.pending: dict = {}
        self.waiting_on: dict = defaultdict(set)   # 前置 seq -> 等待它的下游 seq
        self.ready: list = []             # 最小堆：序号越小越先重放
        self.errors: list = []            # (错误码, 涉及序号列表, 描述)
        self.cycle_members: set = set()
        self._prepare()

    # -- 准备阶段：补全依赖、检测环 --
    def _prepare(self):
        for seq, evt in self.defs.items():
            deps = set(evt.deps)
            if evt.kind == "REVERSAL":
                deps.add(evt.args[0])     # 冲正隐式依赖被冲正事件
            self.deps[seq] = deps
        graph = {seq: sorted(d for d in deps if d in self.defs)
                 for seq, deps in self.deps.items()}
        self.cycle_members = set(find_cycle_members(graph))
        if self.cycle_members:
            members = sorted(self.cycle_members)
            self.errors.append((
                "CYCLE", members,
                "依赖成环，相关事件全部挂起: " + ", ".join(map(str, members)),
            ))

    # -- 事件到达 --
    def arrive(self, seq: int):
        if seq in self.arrived:
            self.errors.append(("DUP_EVENT", [seq], f"事件 {seq} 重复到达，已拦截"))
            return
        self.arrived.add(seq)
        if seq not in self.defs:
            self.errors.append(("UNKNOWN_EVENT", [seq], f"事件 {seq} 没有对应定义，已忽略"))
            return
        if seq in self.cycle_members:
            return                        # 环上事件永不重放（准备阶段已报告）
        self._try_schedule(seq)
        self._drain_ready()

    def _resolved(self, dep: int) -> bool:
        return dep in self.applied or dep in self.failed

    def _try_schedule(self, seq: int):
        unmet = [d for d in self.deps[seq] if not self._resolved(d)]
        if unmet:
            self.pending[seq] = self.defs[seq]
            for dep in unmet:
                self.waiting_on[dep].add(seq)
        else:
            self.pending.pop(seq, None)
            heapq.heappush(self.ready, seq)

    def _drain_ready(self):
        while self.ready:
            seq = heapq.heappop(self.ready)
            self._apply(seq)
            for waiter in sorted(self.waiting_on.pop(seq, ())):
                if waiter in self.pending:
                    self._try_schedule(waiter)

    # -- 落账 --
    def _apply(self, seq: int):
        evt = self.defs[seq]
        try:
            effect = self._execute(evt)
        except ApplyError as exc:
            self.failed.add(seq)
            self.errors.append(("APPLY", [seq], f"事件 {seq} 重放失败: {exc}"))
            return
        self.applied.add(seq)
        if effect is not None:
            self.effects[seq] = effect

    def _execute(self, evt: EventDef):
        if evt.kind == "DEPOSIT":
            account, amount = evt.args
            self.balances[account] = self.balances.get(account, 0) + amount
            return ("DEPOSIT", account, amount)

        if evt.kind == "TRANSFER":
            src, dst, amount = evt.args
            missing = [a for a in (src, dst) if a not in self.balances]
            if missing:
                raise ApplyError("账户不存在: " + ", ".join(missing))
            self.balances[src] -= amount
            self.balances[dst] += amount
            return ("TRANSFER", src, dst, amount)

        # REVERSAL
        (target,) = evt.args
        if target not in self.defs:
            raise ApplyError(f"被冲正事件 {target} 未定义")
        if target in self.reversed_targets:
            raise ApplyError(f"事件 {target} 已被冲正过，不允许重复冲正")
        original = self.effects.get(target)
        if original is None:
            raise ApplyError(f"被冲正事件 {target} 未生效（不存在/未到账/已失败），无法冲正")
        if original[0] == "DEPOSIT":
            _, account, amount = original
            self.balances[account] -= amount
        else:  # TRANSFER：双向往回冲
            _, src, dst, amount = original
            self.balances[src] += amount
            self.balances[dst] -= amount
        self.reversed_targets.add(target)
        return None

    # -- 收尾：报告悬挂 / 被环阻塞的事件 --
    def finalize(self):
        for seq in sorted(self.pending):
            missing = sorted(d for d in self.deps[seq] if d not in self.arrived)
            blocked = sorted(d for d in self.deps[seq]
                             if d in self.arrived and not self._resolved(d))
            if missing:
                self.errors.append((
                    "DANGLING", [seq],
                    f"事件 {seq} 悬挂: 前置事件 {missing} 永远未到达",
                ))
            elif blocked:
                self.errors.append((
                    "BLOCKED", [seq],
                    f"事件 {seq} 无法重放: 前置事件 {blocked} 未生效（成环）",
                ))

    # -- 输出 --
    def report(self) -> str:
        lines = ["== 账户余额 =="]
        if not self.balances:
            lines.append("  (无账户)")
        for account in sorted(self.balances):
            lines.append(f"  {account}: {self.balances[account]}")
        lines.append("== 错误清单 ==")
        if not self.errors:
            lines.append("  (无错误)")
        for code, _seqs, message in self.errors:
            lines.append(f"  [{code}] {message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 一致性校验：同一到达集合按序号顺序重放，余额必须与乱序重放一致
# ---------------------------------------------------------------------------

def replay_in_order(defs: dict, stream: list) -> Replayer:
    replay = Replayer(defs)
    for seq in sorted(set(stream)):
        replay.arrive(seq)
    replay.finalize()
    return replay


# ---------------------------------------------------------------------------
# 演示
# ---------------------------------------------------------------------------

DEMO_INPUT = """\
# 基础事件
DEF 1 DEPOSIT alice 1000
DEF 2 DEPOSIT bob 500
DEF 3 TRANSFER alice bob 200 DEPS 1 2
DEF 4 REVERSAL 3
# 悬挂：前置 99 永远不会到达
DEF 5 DEPOSIT carol 300 DEPS 99
# 依赖环：6 <-> 7
DEF 6 DEPOSIT dave 100 DEPS 7
DEF 7 DEPOSIT erin 100 DEPS 6
# 转账引用不存在账户
DEF 8 TRANSFER alice nobody 50
# 无依赖，可立即重放
DEF 9 DEPOSIT alice 100
# 乱序到达，且 9 与 3 重复到达
STREAM 3 9 1 4 2 8 6 7 5 9 3
"""


def run(text: str) -> int:
    defs, stream, parse_errors = parse_input(text)
    replay = Replayer(defs)
    for seq in stream:
        replay.arrive(seq)
    replay.finalize()

    if parse_errors:
        print("== 解析错误 ==")
        for err in parse_errors:
            print(f"  [PARSE] {err}")
        print()
    print(replay.report())

    baseline = replay_in_order(defs, stream)
    consistent = replay.balances == baseline.balances
    print(f"\n乱序/顺序重放一致性校验: {'通过' if consistent else '不一致！'}")
    return 0 if consistent else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="乱序事件流的因果重放账务工具")
    parser.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置演示")
    args = parser.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    return run(text)


if __name__ == "__main__":
    sys.exit(main())
