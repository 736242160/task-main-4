#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游乐园设施联动安全联锁模拟器（纯 Python 标准库，单文件）。

输入（JSON，文件路径或 stdin）：
{
  "facilities":  [{"name": "过山车", "group": "A区", "deps": ["配电房"]}, ...],
  "inspections": [{"group": "A区", "date": "2026-10-07", "result": "pass"}, ...],
  "events": [
    {"stream": "run",   "facility": "配电房", "status": "on"},
    {"stream": "fault", "facility": "配电房", "status": "fault"},
    ...
  ]
}

也可用 "run_stream" / "fault_stream" 两个数组代替 "events"
（各自元素 {"facility": ..., "status": ...}，按 run_stream 在前、
fault_stream 在后的顺序合并为事件流）。

规则（自定，均会在违反时记录错误并拦截）：
  R1 开机条件：设施存在、未在运行、无故障、不在故障级联封锁中、
     所属设施组最近一次日检结果为 pass、所有依赖设施均“就绪”。
  R2 依赖就绪 = 依赖设施存在 且 正在运行 且 无故障 且 未被级联封锁。
     不就绪时在错误中逐一报告缺哪个依赖。
  R3 故障/停机级联：设施故障或停机时，其全部下游（传递）依赖设施
     强制停机并记入级联封锁，故障解除前不得开机。
  R4 逐级放行：故障解除仅解除封锁，不自动复机；下游须待上游重新
     运行后（由 R1/R2 天然保证）逐级手动开机。
  R5 依赖成环在加载期报告，环上设施禁止开机。
  R6 重复开机、重复报故障、解除不存在的故障、引用不存在设施均报错。

用法：
  python3 park_interlock.py input.json
  cat input.json | python3 park_interlock.py
  python3 park_interlock.py --demo     # 内置联锁停机与逐级放行样例
"""

import argparse
import json
import sys
from collections import defaultdict

PASS_WORDS = {"pass", "ok", "合格", "通过", True}
ON_WORDS = {"on", "start", "run", "开机", "启动", True}
FAULT_WORDS = {"fault", "on", "故障", True}


class Park:
    def __init__(self):
        self.facilities = {}          # name -> {"group": str|None, "deps": [str]}
        self.dependents = defaultdict(set)  # name -> 直接下游设施集合
        self.inspections = {}         # group -> {"date": str, "result": str}
        self.running = set()
        self.faults = set()
        self.blocked_by = defaultdict(set)  # 设施 -> 造成级联封锁的源头设施集合
        self.cycle_members = set()
        self.errors = []
        self.log = []

    # ---------- 加载 ----------

    def load_facilities(self, defs):
        for d in defs or []:
            name = d.get("name")
            if not name:
                self.errors.append("设施定义缺少 name 字段，已跳过")
                continue
            if name in self.facilities:
                self.errors.append("设施定义重复: %s" % name)
                continue
            self.facilities[name] = {
                "group": d.get("group"),
                "deps": list(d.get("deps") or []),
            }
        for name, info in self.facilities.items():
            for dep in info["deps"]:
                if dep not in self.facilities:
                    self.errors.append(
                        "设施 %s 引用了不存在的依赖设施: %s" % (name, dep))
                else:
                    self.dependents[dep].add(name)
        self._detect_cycles()

    def _detect_cycles(self):
        WHITE, GRAY, BLACK = 0, 1, 2
        color = defaultdict(int)
        stack = []

        def dfs(node):
            color[node] = GRAY
            stack.append(node)
            for dep in self.facilities[node]["deps"]:
                if dep not in self.facilities:
                    continue
                if color[dep] == GRAY:
                    cycle = stack[stack.index(dep):] + [dep]
                    self.errors.append(
                        "检测到依赖成环: %s；环上设施禁止开机"
                        % " -> ".join(cycle))
                    self.cycle_members.update(cycle)
                elif color[dep] == WHITE:
                    dfs(dep)
            stack.pop()
            color[node] = BLACK

        for name in self.facilities:
            if color[name] == WHITE:
                dfs(name)

    def load_inspections(self, items):
        for it in items or []:
            group = it.get("group")
            if not group:
                self.errors.append("日检记录缺少 group 字段，已跳过")
                continue
            known = any(f["group"] == group for f in self.facilities.values())
            if not known:
                self.errors.append("日检引用了不存在的设施组: %s" % group)
            self.inspections[group] = {
                "date": it.get("date", ""),
                "result": it.get("result", ""),
            }
            self.log.append("日检登记: 组 %s 日期 %s 结果 %s（同组设施共享）"
                            % (group, it.get("date", ""), it.get("result", "")))

    # ---------- 判定 ----------

    def inspection_ok(self, name):
        group = self.facilities[name]["group"]
        if group is None:
            return False, "未分配设施组，无日检结果"
        rec = self.inspections.get(group)
        if rec is None:
            return False, "设施组 %s 无日检记录" % group
        if rec["result"] in PASS_WORDS:
            return True, ""
        return False, "设施组 %s 日检不合格（%s，%s）" % (
            group, rec["date"], rec["result"])

    def unready_deps(self, name):
        """返回不就绪依赖及其原因列表。"""
        problems = []
        for dep in self.facilities[name]["deps"]:
            if dep not in self.facilities:
                problems.append("%s（设施不存在）" % dep)
            elif dep in self.faults:
                problems.append("%s（故障中）" % dep)
            elif self.blocked_by.get(dep):
                problems.append("%s（被级联封锁，源头: %s）"
                                % (dep, "、".join(sorted(self.blocked_by[dep]))))
            elif dep not in self.running:
                problems.append("%s（未运行）" % dep)
        return problems

    # ---------- 事件 ----------

    def _cascade_stop(self, source, reason):
        """停机/故障级联：停掉所有传递下游并记录封锁源头。"""
        queue = list(self.dependents.get(source, ()))
        seen = set()
        while queue:
            node = queue.pop(0)
            if node in seen:
                continue
            seen.add(node)
            self.blocked_by[node].add(source)
            if node in self.running:
                self.running.discard(node)
                self.log.append("级联停机: %s（因 %s）" % (node, reason))
            queue.extend(self.dependents.get(node, ()))

    def start(self, name):
        if name not in self.facilities:
            self.errors.append("开机请求引用了不存在的设施: %s" % name)
            return
        if name in self.cycle_members:
            self.errors.append("开机拦截: %s 位于依赖环上，禁止开机" % name)
            return
        if name in self.running:
            self.errors.append("开机拦截: %s 重复开机" % name)
            return
        if name in self.faults:
            self.errors.append("开机拦截: %s 故障未解除" % name)
            return
        if self.blocked_by.get(name):
            self.errors.append("开机拦截: %s 处于故障级联封锁（源头: %s），"
                               "需故障解除后逐级放行"
                               % (name, "、".join(sorted(self.blocked_by[name]))))
            return
        ok, why = self.inspection_ok(name)
        if not ok:
            self.errors.append("开机拦截: %s 日检不合格/缺失——%s" % (name, why))
            return
        problems = self.unready_deps(name)
        if problems:
            self.errors.append("开机拦截: %s 依赖未就绪，缺: %s"
                               % (name, "；".join(problems)))
            return
        self.running.add(name)
        self.log.append("开机: %s" % name)

    def stop(self, name):
        if name not in self.facilities:
            self.errors.append("停机请求引用了不存在的设施: %s" % name)
            return
        if name not in self.running:
            self.errors.append("停机拦截: %s 本就不在运行" % name)
            return
        self.running.discard(name)
        self.log.append("停机: %s" % name)
        self._cascade_stop(name, "上游 %s 停机" % name)

    def fault_on(self, name):
        if name not in self.facilities:
            self.errors.append("故障上报引用了不存在的设施: %s" % name)
            return
        if name in self.faults:
            self.errors.append("故障拦截: %s 重复上报故障" % name)
            return
        self.faults.add(name)
        if name in self.running:
            self.running.discard(name)
            self.log.append("故障停机: %s" % name)
        else:
            self.log.append("故障登记: %s（本未运行）" % name)
        self._cascade_stop(name, "上游 %s 故障" % name)

    def fault_off(self, name):
        if name not in self.facilities:
            self.errors.append("故障解除引用了不存在的设施: %s" % name)
            return
        if name not in self.faults:
            self.errors.append("故障解除拦截: %s 当前无故障记录" % name)
            return
        self.faults.discard(name)
        self.log.append("故障解除: %s" % name)
        # 逐级放行：仅解除封锁标记，复机仍需逐条 run on 并满足依赖就绪
        released = []
        for node in sorted(self.blocked_by):
            if name in self.blocked_by[node]:
                self.blocked_by[node].discard(name)
                if not self.blocked_by[node]:
                    released.append(node)
        for node in released:
            self.log.append("放行: %s 的级联封锁已解除，可在依赖就绪后重新开机"
                            % node)

    # ---------- 驱动 ----------

    def run_events(self, events):
        for i, ev in enumerate(events or [], 1):
            stream = ev.get("stream", "run")
            fac = ev.get("facility", "")
            status = ev.get("status", "")
            before = len(self.errors)
            if stream == "run":
                if status in ON_WORDS:
                    self.start(fac)
                else:
                    self.stop(fac)
            elif stream == "fault":
                if status in FAULT_WORDS and status not in ("off", "clear"):
                    self.fault_on(fac)
                else:
                    self.fault_off(fac)
            else:
                self.errors.append("事件 #%d 未知流类型: %s" % (i, stream))
            for j in range(before, len(self.errors)):
                self.errors[j] = "事件 #%d(%s %s %s): %s" % (
                    i, stream, fac, status, self.errors[j])

    def report(self):
        facs = {}
        for name in sorted(self.facilities):
            info = self.facilities[name]
            group = info["group"]
            rec = self.inspections.get(group)
            insp = "无记录" if rec is None else "%s(%s)" % (
                rec["result"], rec["date"])
            if name in self.faults:
                state = "故障"
            elif name in self.running:
                state = "运行中"
            elif self.blocked_by.get(name):
                state = "级联封锁停机"
            else:
                state = "停机"
            facs[name] = {
                "状态": state,
                "设施组": group,
                "日检": insp,
                "依赖": info["deps"],
                "封锁源头": sorted(self.blocked_by.get(name, ())),
            }
        return {"设施状态": facs, "事件日志": self.log, "错误清单": self.errors}


def normalize_input(data):
    """支持 events 单流，或 run_stream/fault_stream 双流合并。"""
    events = list(data.get("events") or [])
    if not events:
        for e in data.get("run_stream") or []:
            events.append({"stream": "run", **e})
        for e in data.get("fault_stream") or []:
            events.append({"stream": "fault", **e})
    return events


def run(data):
    park = Park()
    park.load_facilities(data.get("facilities"))
    park.load_inspections(data.get("inspections"))
    park.run_events(normalize_input(data))
    return park.report()


DEMO = {
    "facilities": [
        {"name": "配电房", "group": "动力组", "deps": []},
        {"name": "过山车", "group": "A区", "deps": ["配电房"]},
        {"name": "摩天轮", "group": "A区", "deps": ["配电房"]},
        {"name": "观景塔", "group": "B区", "deps": ["过山车"]},
        {"name": "旋转木马", "group": "C区", "deps": []},
        {"name": "环甲", "group": "D区", "deps": ["环乙"]},
        {"name": "环乙", "group": "D区", "deps": ["环甲"]},
    ],
    "inspections": [
        {"group": "动力组", "date": "2026-10-07", "result": "pass"},
        {"group": "A区", "date": "2026-10-07", "result": "pass"},
        {"group": "B区", "date": "2026-10-07", "result": "pass"},
        {"group": "C区", "date": "2026-10-07", "result": "fail"},
        {"group": "D区", "date": "2026-10-07", "result": "pass"},
    ],
    "events": [
        {"stream": "run", "facility": "过山车", "status": "on"},
        {"stream": "run", "facility": "配电房", "status": "on"},
        {"stream": "run", "facility": "配电房", "status": "on"},
        {"stream": "run", "facility": "过山车", "status": "on"},
        {"stream": "run", "facility": "摩天轮", "status": "on"},
        {"stream": "run", "facility": "观景塔", "status": "on"},
        {"stream": "run", "facility": "旋转木马", "status": "on"},
        {"stream": "run", "facility": "环甲", "status": "on"},
        {"stream": "run", "facility": "鬼屋", "status": "on"},
        {"stream": "fault", "facility": "配电房", "status": "fault"},
        {"stream": "run", "facility": "观景塔", "status": "on"},
        {"stream": "run", "facility": "过山车", "status": "on"},
        {"stream": "fault", "facility": "配电房", "status": "clear"},
        {"stream": "run", "facility": "观景塔", "status": "on"},
        {"stream": "run", "facility": "配电房", "status": "on"},
        {"stream": "run", "facility": "观景塔", "status": "on"},
        {"stream": "run", "facility": "过山车", "status": "on"},
        {"stream": "run", "facility": "观景塔", "status": "on"},
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="游乐园设施联动安全联锁模拟器")
    ap.add_argument("input", nargs="?", help="输入 JSON 文件（缺省读 stdin）")
    ap.add_argument("--demo", action="store_true",
                    help="运行内置联锁停机与逐级放行样例")
    args = ap.parse_args(argv)

    if args.demo:
        data = DEMO
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    json.dump(run(data), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
