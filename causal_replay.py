#!/usr/bin/env python3
"""
causal_replay.py — 乱序事件流因果重放工具（纯 Python 标准库，单文件）

输入格式（文本文件或标准输入；'#' 起为注释，空行忽略）：

    DEF <序号> deposit  <账户> <金额> [DEP <前置序号>]
    DEF <序号> transfer <源账户> <目标账户> <金额> [DEP <前置序号>]
    DEF <序号> reverse  <目标事件序号> [DEP <前置序号>]
    STREAM <序号> [<序号> ...]        # 事件实际到达顺序，可写多行

说明：
    - deposit  （入账）：贷记账户；账户不存在则自动开立。
    - transfer （转账）：两个账户都必须已存在，否则报错且事件失败。
    - reverse  （冲正）：撤销目标事件已产生的账务效果（记反向分录）。
      冲正隐式依赖其目标事件，无需再写 DEP。

用法：
    python3 causal_replay.py [输入文件]     # 缺省读标准输入
    python3 causal_replay.py --selftest     # 运行内置自测
"""

import random
import sys
from dataclasses import dataclass, field
from decimal import Decimal

# 事件处理状态
PENDING = "pending"    # 已到达，等待前置
APPLIED = "applied"    # 已重放成功
FAILED = "failed"      # 已重放但业务失败（视为已了结，不阻塞后继）
CYCLE = "cycle"        # 处于依赖环中，永不重放


@dataclass
class Event:
    seq: int
    kind: str                       # deposit / transfer / reverse
    args: tuple                     # deposit:(acct,amt) transfer:(src,dst,amt) reverse:(target_seq,)
    dep: int = None                 # 显式前置事件序号


# ---------------------------------------------------------------- 输入解析

def parse_input(text):
    """解析输入文本，返回 (defs: {seq: Event}, stream: [seq], parse_errors: [str])。"""
    defs = {}
    stream = []
    errors = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tok = line.split()
        head = tok[0].upper()
        if head == "STREAM":
            for t in tok[1:]:
                try:
                    stream.append(int(t))
                except ValueError:
                    errors.append(f"第{lineno}行: STREAM 序号非法: {t!r}")
            continue
        if head != "DEF":
            errors.append(f"第{lineno}行: 未知指令 {tok[0]!r}（应为 DEF 或 STREAM）")
            continue
        # DEF <seq> <kind> <args...> [DEP <dep>]
        dep = None
        body = tok[1:]
        if "DEP" in [t.upper() for t in body]:
            i = [t.upper() for t in body].index("DEP")
            if i + 1 >= len(body):
                errors.append(f"第{lineno}行: DEP 后缺少前置序号")
                continue
            try:
                dep = int(body[i + 1])
            except ValueError:
                errors.append(f"第{lineno}行: DEP 前置序号非法: {body[i + 1]!r}")
                continue
            body = body[:i]
        if len(body) < 2:
            errors.append(f"第{lineno}行: DEF 参数不足")
            continue
        try:
            seq = int(body[0])
        except ValueError:
            errors.append(f"第{lineno}行: 事件序号非法: {body[0]!r}")
            continue
        kind = body[1].lower()
        rest = body[2:]
        ev = None
        try:
            if kind in ("deposit", "入账") and len(rest) == 2:
                ev = Event(seq, "deposit", (rest[0], Decimal(rest[1])), dep)
            elif kind in ("transfer", "转账") and len(rest) == 3:
                ev = Event(seq, "transfer", (rest[0], rest[1], Decimal(rest[2])), dep)
            elif kind in ("reverse", "冲正") and len(rest) == 1:
                ev = Event(seq, "reverse", (int(rest[0]),), dep)
            else:
                errors.append(f"第{lineno}行: 事件类型或参数个数不符: {line!r}")
                continue
        except (ValueError, ArithmeticError):
            errors.append(f"第{lineno}行: 数值解析失败: {line!r}")
            continue
        if seq in defs:
            errors.append(f"第{lineno}行: 重复定义事件序号 {seq}，已拦截（保留先到的定义）")
            continue
        defs[seq] = ev
    return defs, stream, errors


# ---------------------------------------------------------------- 依赖环检测

def find_cycles(defs):
    """在依赖图上找环。返回 (环成员集合, [环路径, ...])。

    依赖边 = 显式 DEP 边 + 冲正事件到其目标的隐式边（只统计已定义的序号）。
    """
    def edges(seq):
        ev = defs[seq]
        out = []
        if ev.dep is not None and ev.dep in defs:
            out.append(ev.dep)
        if ev.kind == "reverse":
            tgt = ev.args[0]
            if tgt in defs:
                out.append(tgt)
        return out

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {s: WHITE for s in defs}
    members, cycles = set(), []

    def dfs(seq, path):
        color[seq] = GRAY
        path.append(seq)
        for nxt in edges(seq):
            if color[nxt] == GRAY:                      # 回边 -> 发现环
                i = path.index(nxt)
                cycles.append(path[i:] + [nxt])
                members.update(path[i:])
            elif color[nxt] == WHITE:
                dfs(nxt, path)
        path.pop()
        color[seq] = BLACK

    for s in sorted(defs):
        if color[s] == WHITE:
            dfs(s, [])
    return members, cycles


# ---------------------------------------------------------------- 重放引擎

class Engine:
    """按因果顺序重放事件流。

    核心不变式：一个事件只有在其全部因果前置（显式 DEP + 冲正目标）
    都已了结（applied/failed）后才会被重放；就绪队列按序号升序排空，
    因此任意到达次序最终得到与顺序重放一致的账务状态。
    """

    def __init__(self, defs):
        self.defs = defs
        self.balances = {}                 # account -> Decimal
        self.status = {}                   # seq -> PENDING/APPLIED/FAILED/CYCLE
        self.effects = {}                  # seq -> [(account, delta)] 已生效分录
        self.pending = {}                  # seq -> Event（已到达、等待前置）
        self.arrived = set()
        self.errors = []
        # 依赖环：预处理阶段标记，环上事件永不重放
        self.cycle_members, cycles = find_cycles(defs)
        for cyc in cycles:
            self.errors.append("依赖成环: " + " -> ".join(map(str, cyc)))
        for seq in self.cycle_members:
            self.status[seq] = CYCLE

    # -- 因果前置是否已了结 --
    def _resolved(self, seq):
        return self.status.get(seq) in (APPLIED, FAILED)

    def _deps_met(self, ev):
        if ev.dep is not None and not self._resolved(ev.dep):
            return False
        if ev.kind == "reverse" and not self._resolved(ev.args[0]):
            return False
        return True

    # -- 事件到达 --
    def arrive(self, seq):
        if seq in self.arrived:
            self.errors.append(f"事件 {seq}: 同序号重复到达，已拦截")
            return
        self.arrived.add(seq)
        ev = self.defs.get(seq)
        if ev is None:
            self.errors.append(f"事件 {seq}: 未定义的事件到达，已丢弃")
            return
        if seq in self.cycle_members:
            return  # 环上事件已在预处理时报告，永不重放
        self.pending[seq] = ev
        self.status[seq] = PENDING
        self._drain()

    # -- 排空就绪队列（按序号升序，保证确定性） --
    def _drain(self):
        while True:
            ready = [s for s, ev in self.pending.items() if self._deps_met(ev)]
            if not ready:
                return
            seq = min(ready)
            ev = self.pending.pop(seq)
            self._apply(ev)

    # -- 重放单个事件 --
    def _apply(self, ev):
        seq = ev.seq
        if ev.kind == "deposit":
            acct, amt = ev.args
            self._post([(acct, amt)], seq)
        elif ev.kind == "transfer":
            src, dst, amt = ev.args
            missing = [a for a in (src, dst) if a not in self.balances]
            if missing:
                self.errors.append(
                    f"事件 {seq}: 转账引用不存在账户 {', '.join(sorted(set(missing)))}，转账失败")
                self.status[seq] = FAILED
                self.effects[seq] = []
            else:
                self._post([(src, -amt), (dst, amt)], seq)
        elif ev.kind == "reverse":
            tgt = ev.args[0]
            if tgt not in self.defs:
                self.errors.append(f"事件 {seq}: 冲正目标事件 {tgt} 不存在，冲正失败")
                self.status[seq] = FAILED
                self.effects[seq] = []
            elif self.status.get(tgt) != APPLIED:
                self.errors.append(
                    f"事件 {seq}: 冲正目标事件 {tgt} 未成功入账，无法冲正")
                self.status[seq] = FAILED
                self.effects[seq] = []
            else:
                # 对目标事件的全部已生效分录记反向分录，精确撤销其效果
                inv = [(acct, -delta) for acct, delta in self.effects[tgt]]
                self._post(inv, seq)

    def _post(self, entries, seq):
        for acct, delta in entries:
            self.balances[acct] = self.balances.get(acct, Decimal(0)) + delta
        self.effects[seq] = list(entries)
        self.status[seq] = APPLIED

    # -- 流结束：报告悬挂事件 --
    def finish(self):
        for seq in sorted(self.pending):
            ev = self.pending[seq]
            causes = []
            if ev.dep is not None and not self._resolved(ev.dep):
                causes.append(f"前置事件 {ev.dep} {self._why_missing(ev.dep)}")
            if ev.kind == "reverse" and not self._resolved(ev.args[0]):
                causes.append(f"冲正目标事件 {ev.args[0]} {self._why_missing(ev.args[0])}")
            self.errors.append(f"事件 {seq}: 悬挂（" + "；".join(causes) + "）")
        return self

    def _why_missing(self, seq):
        if seq not in self.defs:
            return "未定义"
        if seq in self.cycle_members:
            return "处于依赖环中"
        if seq not in self.arrived:
            return "永远未到达"
        return "自身也在等待前置"


# ---------------------------------------------------------------- 输出报告

def fmt_amount(d):
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def render(engine, parse_errors):
    out = ["=== 账户余额 ==="]
    if engine.balances:
        for acct in sorted(engine.balances):
            out.append(f"{acct}: {fmt_amount(engine.balances[acct])}")
    else:
        out.append("（无账户）")
    out.append("")
    out.append("=== 错误报告 ===")
    all_errors = parse_errors + engine.errors
    if all_errors:
        for i, e in enumerate(all_errors, 1):
            out.append(f"[E{i:02d}] {e}")
    else:
        out.append("（无错误）")
    return "\n".join(out)


def run(text):
    defs, stream, parse_errors = parse_input(text)
    engine = Engine(defs)
    for seq in stream:
        engine.arrive(seq)
    engine.finish()
    return engine, parse_errors


# ---------------------------------------------------------------- 自测

def selftest():
    # 场景 1：乱序到达 + 依赖挂起 + 冲正，结果须与顺序重放一致
    base = """
        DEF 1 deposit alice 100
        DEF 2 deposit bob 0 DEP 1
        DEF 3 transfer alice bob 30 DEP 2
        DEF 4 reverse 3 DEP 3
        DEF 5 deposit carol 50 DEP 1
        DEF 6 transfer carol alice 20 DEP 5
    """
    ordered, _ = run(base + "\nSTREAM 1 2 3 4 5 6")
    for seed in range(50):
        seqs = [1, 2, 3, 4, 5, 6]
        random.Random(seed).shuffle(seqs)
        eng, perr = run(base + "\nSTREAM " + " ".join(map(str, seqs)))
        assert not perr and not eng.errors, (seed, eng.errors)
        assert eng.balances == ordered.balances, (seed, eng.balances, ordered.balances)
    assert ordered.balances == {"alice": Decimal(120), "bob": Decimal(0),
                                "carol": Decimal(30)}, ordered.balances

    # 场景 2：悬挂（前置永远不到）
    eng, _ = run("DEF 1 deposit a 10 DEP 99\nSTREAM 1")
    assert any("悬挂" in e and "99" in e for e in eng.errors), eng.errors
    assert eng.balances == {}, eng.balances

    # 场景 3：依赖成环（a 依赖 b、b 依赖 a），环上事件不重放
    eng, _ = run("DEF 1 deposit a 10 DEP 2\nDEF 2 deposit b 20 DEP 1\nSTREAM 1 2")
    assert any("成环" in e for e in eng.errors), eng.errors
    assert eng.balances == {}, eng.balances

    # 场景 4：转账引用不存在账户
    eng, _ = run("DEF 1 deposit a 10\nDEF 2 transfer a ghost 5 DEP 1\nSTREAM 1 2")
    assert any("不存在账户" in e for e in eng.errors), eng.errors
    assert eng.balances == {"a": Decimal(10)}, eng.balances

    # 场景 5：同序号重复到达被拦截
    eng, _ = run("DEF 1 deposit a 10\nSTREAM 1 1")
    assert any("重复" in e for e in eng.errors), eng.errors
    assert eng.balances == {"a": Decimal(10)}, eng.balances

    # 场景 6：冲正失败的目标 -> 报错
    eng, _ = run("DEF 1 transfer a b 5\nDEF 2 reverse 1 DEP 1\nSTREAM 1 2")
    assert any("无法冲正" in e for e in eng.errors), eng.errors

    # 场景 7：冲正的冲正（反向分录再反向 = 重新生效）
    eng, perr = run("DEF 1 deposit a 10\nDEF 2 reverse 1 DEP 1\nDEF 3 reverse 2 DEP 2\nSTREAM 3 2 1")
    assert not perr and not eng.errors, eng.errors
    assert eng.balances == {"a": Decimal(10)}, eng.balances

    print("SELFTEST OK")


# ---------------------------------------------------------------- 入口

def main(argv):
    if len(argv) > 1 and argv[1] == "--selftest":
        selftest()
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    engine, parse_errors = run(text)
    print(render(engine, parse_errors))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
