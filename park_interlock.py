#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游乐园设施联动控制工具(纯 Python 标准库, 单文件)

输入格式(行式文本, # 后为注释, 空行忽略, 按行顺序处理, 状态跨流延续):

    DEF     <名称> <日检组> [依赖设施...]     设施定义(应先于各流出现)
    INSPECT <设施组> <YYYY-MM-DD> <PASS|FAIL> 日检流: 同组设施共享结果
    RUN     <设施> <START|STOP>               运行流: 开机 / 停机
    FAULT   <设施> <FAULT|CLEAR>              故障流: 报故障 / 解除故障

联锁规则(自定):
    * 依赖就绪规则: 设施开机时, 其全部依赖设施必须处于"运行"状态,
      否则拦截并逐一报告缺哪个依赖及其当前状态。
    * 日检拦截: 设施所属组最新一次日检为 FAIL 时, 开机被拦截。
    * 故障级联: 设施故障(或运行中停机)时, 所有直接/间接依赖它的
      在运行设施被联锁停机(INTERLOCKED)。
    * 逐级放行: 故障解除且上游恢复运行后, 被联锁的下游设施按依赖
      层级自动逐级放行恢复运行(依赖未就绪的继续等待)。
    * 依赖成环、重复开机、引用不存在设施均报告错误并拦截。

用法:
    python3 park_interlock.py 输入文件      # 或省略文件名从标准输入读
    python3 park_interlock.py --demo        # 运行内置联锁停机与放行样例
"""

import argparse
import datetime
import sys
from collections import defaultdict

STOPPED = "STOPPED"          # 停机
RUNNING = "RUNNING"          # 运行
FAULT = "FAULT"              # 故障
INTERLOCKED = "INTERLOCKED"  # 联锁停机(被上游故障/停机 cascade 停掉)

STATE_CN = {
    STOPPED: "停机",
    RUNNING: "运行",
    FAULT: "故障",
    INTERLOCKED: "联锁停机",
}

DEMO = """\
# ===== 设施定义: DEF <名称> <日检组> [依赖设施...] =====
DEF 旋转木马 G1
DEF 小火车   G1
DEF 过山车   G2 旋转木马 小火车
DEF 跳楼机   G2 过山车
DEF 摩天轮   G3
DEF 甲 G9 乙
DEF 乙 G9 甲

# ===== 日检流: INSPECT <设施组> <日期> <PASS|FAIL> =====
INSPECT G1 2026-10-01 PASS
INSPECT G2 2026-10-01 PASS
INSPECT G3 2026-10-01 FAIL

# ===== 运行流: RUN <设施> <START|STOP> =====
RUN 过山车 START      # 依赖未就绪 -> 拦截并报告缺哪个依赖
RUN 旋转木马 START
RUN 小火车 START
RUN 过山车 START
RUN 跳楼机 START
RUN 摩天轮 START      # G3 日检不合格 -> 拦截
RUN 过山车 START      # 重复开机 -> 拦截
RUN 海盗船 START      # 不存在的设施 -> 报告
RUN 甲 START          # 依赖成环 -> 拦截

# ===== 故障流: FAULT <设施> <FAULT|CLEAR> =====
FAULT 小火车 FAULT    # 级联: 过山车、跳楼机 联锁停机
RUN 跳楼机 START      # 上游未恢复 -> 拦截
FAULT 小火车 CLEAR    # 故障解除(下游仍等待上游恢复)
RUN 小火车 START      # 上游恢复后逐级放行: 过山车 -> 跳楼机
"""


class Park:
    def __init__(self):
        self.deps = {}                     # 设施名 -> [依赖设施名]
        self.group_of = {}                 # 设施名 -> 日检组
        self.status = {}                   # 设施名 -> 状态
        self.def_line = {}                 # 设施名 -> 定义所在行号
        self.inspections = defaultdict(list)  # 日检组 -> [(日期, 结果)]
        self.errors = []
        self.log = []
        self.cyclic = set()                # 处于依赖环中的设施
        self._reported_cycles = set()

    def error(self, lineno, msg):
        self.errors.append("行{}: {}".format(lineno, msg))

    # ---------- 设施定义 ----------
    def define(self, lineno, name, group, deps):
        if name in self.deps:
            self.error(lineno, "设施重复定义: {}".format(name))
            return
        self.deps[name] = list(deps)
        self.group_of[name] = group
        self.status[name] = STOPPED
        self.def_line[name] = lineno
        self.detect_cycles(lineno)

    def detect_cycles(self, lineno):
        """全图 DFS 检测依赖环, 新环报告一次, 环上设施记入 self.cyclic。"""
        color = {n: 0 for n in self.deps}  # 0=未访 1=在栈 2=完成
        self.cyclic = set()
        stack = []

        def dfs(u):
            color[u] = 1
            stack.append(u)
            for v in self.deps.get(u, []):
                if v not in self.deps:
                    continue
                if color[v] == 1:
                    cyc = stack[stack.index(v):] + [v]
                    key = frozenset(cyc)
                    if key not in self._reported_cycles:
                        self._reported_cycles.add(key)
                        self.error(lineno, "依赖成环: {}".format(" -> ".join(cyc)))
                    self.cyclic.update(cyc)
                elif color[v] == 0:
                    dfs(v)
            stack.pop()
            color[u] = 2

        for n in self.deps:
            if color[n] == 0:
                dfs(n)

    def validate_refs(self):
        """输入结束后校验: 定义中引用的依赖设施必须存在。"""
        for name, deps in self.deps.items():
            for d in deps:
                if d not in self.deps:
                    self.error(self.def_line[name],
                               "设施 {} 引用了不存在的依赖设施: {}".format(name, d))

    # ---------- 日检流 ----------
    def inspect(self, lineno, group, date, result):
        if result not in ("PASS", "FAIL"):
            self.error(lineno, "日检结果无效: {} (应为 PASS/FAIL)".format(result))
            return
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            self.error(lineno, "日期格式无效: {} (应为 YYYY-MM-DD)".format(date))
            return
        members = [n for n in self.deps if self.group_of[n] == group]
        if not members:
            self.error(lineno, "日检引用了不存在的设施组: {}".format(group))
            return
        self.inspections[group].append((date, result))
        self.log.append("行{}: 日检 组{} {} {} -> 同组共享: {}".format(
            lineno, group, date, result, "、".join(members)))

    def inspection_ok(self, name):
        """返回 (是否允许开机, 拦截原因)。同组共享最新一次日检结果。"""
        records = self.inspections.get(self.group_of[name])
        if not records:
            return True, None
        date, result = max(records, key=lambda r: r[0])
        if result == "FAIL":
            return False, "组{} 最新日检({})不合格".format(self.group_of[name], date)
        return True, None

    # ---------- 运行流 ----------
    def run(self, lineno, name, action):
        if name not in self.deps:
            self.error(lineno, "运行流引用了不存在的设施: {}".format(name))
            return
        if action == "START":
            if self._try_start(lineno, name, manual=True):
                self.log.append("行{}: {} 开机运行".format(lineno, name))
                self._release(lineno)
        elif action == "STOP":
            if self.status[name] != RUNNING:
                self.error(lineno, "停机无效: {} 当前未在运行 ({})".format(
                    name, STATE_CN[self.status[name]]))
                return
            self.status[name] = STOPPED
            self.log.append("行{}: {} 停机".format(lineno, name))
            self._cascade(name, lineno)
        else:
            self.error(lineno, "运行操作无效: {} (应为 START/STOP)".format(action))

    def _try_start(self, lineno, name, manual):
        """尝试开机; manual=False 时不记错误(用于自动放行场景)。"""
        st = self.status[name]
        if st == RUNNING:
            if manual:
                self.error(lineno, "重复开机被拦截: {} 已在运行".format(name))
            return False
        if st == FAULT:
            if manual:
                self.error(lineno, "开机被拦截: {} 处于故障状态, 需先解除故障".format(name))
            return False
        if name in self.cyclic:
            if manual:
                self.error(lineno, "开机被拦截: {} 处于依赖环中, 无法就绪".format(name))
            return False
        ok, why = self.inspection_ok(name)
        if not ok:
            if manual:
                self.error(lineno, "开机被拦截: {} {}".format(name, why))
            return False
        missing = []
        for d in self.deps[name]:
            if d not in self.deps:
                missing.append("{}(不存在)".format(d))
            elif self.status[d] != RUNNING:
                missing.append("{}({})".format(d, STATE_CN[self.status[d]]))
        if missing:
            if manual:
                self.error(lineno, "开机被拦截: {} 依赖未就绪, 缺: {}".format(
                    name, ", ".join(missing)))
            return False
        self.status[name] = RUNNING
        return True

    # ---------- 故障流 ----------
    def fault(self, lineno, name, action):
        if name not in self.deps:
            self.error(lineno, "故障流引用了不存在的设施: {}".format(name))
            return
        if action == "FAULT":
            if self.status[name] == FAULT:
                self.error(lineno, "重复报故障: {} 已处于故障状态".format(name))
                return
            was_running = self.status[name] == RUNNING
            self.status[name] = FAULT
            self.log.append("行{}: {} 故障{}".format(
                lineno, name, "(运行中->故障停机)" if was_running else ""))
            self._cascade(name, lineno)
        elif action == "CLEAR":
            if self.status[name] != FAULT:
                self.error(lineno, "解除故障无效: {} 当前无故障".format(name))
                return
            self.status[name] = STOPPED
            self.log.append("行{}: {} 故障解除".format(lineno, name))
            self._release(lineno)
        else:
            self.error(lineno, "故障操作无效: {} (应为 FAULT/CLEAR)".format(action))

    # ---------- 级联与放行 ----------
    def _downstream(self, name):
        """所有直接/间接依赖 name 的设施。"""
        dependents = defaultdict(list)
        for n, ds in self.deps.items():
            for d in ds:
                dependents[d].append(n)
        seen, queue = set(), [name]
        while queue:
            cur = queue.pop()
            for nxt in dependents.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def _cascade(self, name, lineno):
        """name 不再可用, 下游在运行设施全部联锁停机。"""
        for n in sorted(self._downstream(name)):
            if self.status[n] == RUNNING:
                self.status[n] = INTERLOCKED
                self.log.append("行{}: 联锁停机 {} (因 {} 不可用)".format(lineno, n, name))

    def _release(self, lineno):
        """逐级放行: 反复扫描被联锁设施, 依赖全部运行的恢复运行,
        直到没有可放行的设施(下游需等上游先放行, 自然逐级)。"""
        while True:
            progress = False
            for n in sorted(self.deps):
                if self.status[n] != INTERLOCKED or n in self.cyclic:
                    continue
                ok, _ = self.inspection_ok(n)
                if not ok:
                    continue
                if all(self.status.get(d) == RUNNING for d in self.deps[n]):
                    self.status[n] = RUNNING
                    self.log.append("行{}: 放行恢复 {} (依赖已就绪, 逐级放行)".format(lineno, n))
                    progress = True
            if not progress:
                break


def parse_and_run(text):
    park = Park()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        if cmd == "DEF":
            if len(args) < 2:
                park.error(lineno, "DEF 格式: DEF <名称> <日检组> [依赖设施...]")
            else:
                park.define(lineno, args[0], args[1], args[2:])
        elif cmd == "INSPECT":
            if len(args) != 3:
                park.error(lineno, "INSPECT 格式: INSPECT <设施组> <日期> <PASS|FAIL>")
            else:
                park.inspect(lineno, args[0], args[1], args[2].upper())
        elif cmd == "RUN":
            if len(args) != 2:
                park.error(lineno, "RUN 格式: RUN <设施> <START|STOP>")
            else:
                park.run(lineno, args[0], args[1].upper())
        elif cmd == "FAULT":
            if len(args) != 2:
                park.error(lineno, "FAULT 格式: FAULT <设施> <FAULT|CLEAR>")
            else:
                park.fault(lineno, args[0], args[1].upper())
        else:
            park.error(lineno, "未知指令: {}".format(parts[0]))
    park.validate_refs()
    return park


def render(park):
    out = ["=== 事件日志 ==="]
    out.extend(park.log or ["(无)"])
    out.append("")
    out.append("=== 设施状态 ===")
    out.append("设施 | 日检组 | 状态 | 依赖")
    for name in park.deps:
        deps = ",".join(park.deps[name]) or "-"
        out.append("{} | {} | {} | {}".format(
            name, park.group_of[name], STATE_CN[park.status[name]], deps))
    out.append("")
    out.append("=== 错误清单 ===")
    out.extend(park.errors or ["(无)"])
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="游乐园设施联动控制工具")
    ap.add_argument("input", nargs="?", help="输入文件(缺省或 - 表示标准输入)")
    ap.add_argument("--demo", action="store_true", help="运行内置联锁停机与放行样例")
    args = ap.parse_args(argv)
    if args.demo:
        text = DEMO
    elif args.input and args.input != "-":
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    park = parse_and_run(text)
    print(render(park))
    return 1 if park.errors else 0


if __name__ == "__main__":
    sys.exit(main())
