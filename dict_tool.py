#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据字典编码树管理工具（纯 Python 标准库，单文件）。

输入：命令流（stdin 或文件），每行一条命令，# 开头为注释：

    DEF     <码> <上级码|->          定义编码（- 表示根），上级必须已存在
    DISABLE <码>                     停用编码
    ENABLE  <码>                     启用编码
    MOVE    <码> <新上级码>          重排：子树整体迁移，子码层级联动更新
    MAP     <业务码> <字典码>        建立映射（停用/不存在编码被拦截）
    UNMAP   <业务码>                 解除映射

版本规则（自定）：初始版本为 0；每次成功的 MOVE 使版本 +1。
映射历史按版本追溯：每条映射记录其建立版本与失效版本，
重排导致子树内编码的活跃映射全部失效并报告。

校验规则：
  - 上级必须存在（DEF/MOVE），引用不存在编码要报告
  - MOVE 成环（新上级是被移动码的子孙）要报告并定位成环码
  - 层数超过 MAX_DEPTH 要报告并拒绝
  - 停用码仍被 MAP 要拦截
  - 重排后子树内旧映射失效并报告

输出：编码树、当前映射、映射历史（按版本）、错误清单。
"""

import sys

MAX_DEPTH = 8


class Node:
    __slots__ = ("code", "parent", "status")

    def __init__(self, code, parent):
        self.code = code
        self.parent = parent      # None 表示根
        self.status = "ENABLED"


class DictState:
    def __init__(self):
        self.nodes = {}           # code -> Node
        self.children = {}        # parent_code -> [child_code]
        self.version = 0
        self.errors = []          # (行号, 类别, 消息)
        self.mappings = {}        # biz -> dict_code（当前活跃映射）
        self.map_history = []     # {biz, dict, from_ver, to_ver, note}

    # ---------- 基础工具 ----------
    def err(self, lineno, kind, msg):
        self.errors.append((lineno, kind, msg))

    def level(self, code):
        """动态计算层级（根为 1）。父指针联动，子码层级随重排自动更新。"""
        lvl, cur = 0, code
        while cur is not None:
            lvl += 1
            cur = self.nodes[cur].parent
        return lvl

    def subtree(self, code):
        """返回 code 及其全部子孙（BFS）。"""
        out, stack = [], [code]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.children.get(cur, ()))
        return out

    def path_to_root(self, code):
        """从 code 沿父链到根的路径（含自身）。"""
        path, cur = [], code
        while cur is not None:
            path.append(cur)
            cur = self.nodes[cur].parent
        return path

    # ---------- 命令处理 ----------
    def do_def(self, lineno, code, parent):
        if code in self.nodes:
            self.err(lineno, "重复定义", f"编码 {code} 已存在")
            return
        if parent == "-":
            parent = None
        elif parent not in self.nodes:
            self.err(lineno, "上级不存在",
                     f"定义 {code} 失败：上级编码 {parent} 不存在")
            return
        lvl = 1 if parent is None else self.level(parent) + 1
        if lvl > MAX_DEPTH:
            self.err(lineno, "层数超限",
                     f"定义 {code} 失败：层级 {lvl} 超过上限 {MAX_DEPTH}")
            return
        self.nodes[code] = Node(code, parent)
        if parent is not None:
            self.children.setdefault(parent, []).append(code)

    def do_disable(self, lineno, code, status):
        node = self.nodes.get(code)
        if node is None:
            self.err(lineno, "编码不存在", f"编码 {code} 不存在")
            return
        node.status = status

    def do_move(self, lineno, code, new_parent):
        node = self.nodes.get(code)
        if node is None:
            self.err(lineno, "编码不存在", f"重排失败：编码 {code} 不存在")
            return
        if new_parent not in self.nodes:
            self.err(lineno, "上级不存在",
                     f"重排 {code} 失败：新上级 {new_parent} 不存在")
            return
        if new_parent == code:
            self.err(lineno, "重排成环",
                     f"重排 {code} 失败：不能挂到自身，成环码={code}")
            return
        # 环检测：新上级若是 code 的子孙，则成环；定位成环路径上的码
        chain = self.path_to_root(new_parent)
        if code in chain:
            idx = chain.index(code)
            cycle = " -> ".join([code] + chain[:idx + 1])
            self.err(lineno, "重排成环",
                     f"重排 {code} -> {new_parent} 失败：成环码={code}，"
                     f"环路径：{cycle}")
            return
        # 层数校验：移动后子树最大深度不得超限
        new_level = self.level(new_parent) + 1
        delta = new_level - self.level(code)
        sub = self.subtree(code)
        max_sub = max(self.level(c) + delta for c in sub)
        if max_sub > MAX_DEPTH:
            self.err(lineno, "层数超限",
                     f"重排 {code} -> {new_parent} 失败：子树最大层级 "
                     f"{max_sub} 超过上限 {MAX_DEPTH}")
            return
        # 执行迁移：摘下旧挂点，挂到新上级，子树随父指针整体迁移
        if node.parent is not None:
            self.children[node.parent].remove(code)
        node.parent = new_parent
        self.children.setdefault(new_parent, []).append(code)
        self.version += 1
        # 旧映射失效：子树内编码的活跃映射全部失效并报告
        sub_set = set(sub)
        for biz, dcode in list(self.mappings.items()):
            if dcode in sub_set:
                self.mappings.pop(biz)
                for rec in reversed(self.map_history):
                    if rec["biz"] == biz and rec["to_ver"] is None:
                        rec["to_ver"] = self.version
                        rec["note"] = f"重排 {code} 导致失效"
                        break
                self.err(lineno, "旧映射失效",
                         f"映射 {biz} -> {dcode} 因重排 {code} "
                         f"(版本 {self.version}) 失效")

    def do_map(self, lineno, biz, dcode):
        node = self.nodes.get(dcode)
        if node is None:
            self.err(lineno, "编码不存在",
                     f"映射 {biz} -> {dcode} 失败：字典码不存在")
            return
        if node.status == "DISABLED":
            self.err(lineno, "停用码被映射",
                     f"映射 {biz} -> {dcode} 被拦截：字典码已停用")
            return
        if biz in self.mappings:
            old = self.mappings[biz]
            for rec in reversed(self.map_history):
                if rec["biz"] == biz and rec["to_ver"] is None:
                    rec["to_ver"] = self.version
                    rec["note"] = f"改映射为 {dcode}"
                    break
        self.mappings[biz] = dcode
        self.map_history.append({
            "biz": biz, "dict": dcode,
            "from_ver": self.version, "to_ver": None, "note": "建立",
        })

    def do_unmap(self, lineno, biz):
        if biz not in self.mappings:
            self.err(lineno, "映射不存在", f"业务码 {biz} 无活跃映射")
            return
        self.mappings.pop(biz)
        for rec in reversed(self.map_history):
            if rec["biz"] == biz and rec["to_ver"] is None:
                rec["to_ver"] = self.version
                rec["note"] = "主动解除"
                break

    # ---------- 输出 ----------
    def render_tree(self):
        lines = []
        roots = [c for c, n in self.nodes.items() if n.parent is None]

        def walk(code, depth):
            node = self.nodes[code]
            tag = "" if node.status == "ENABLED" else " [停用]"
            lines.append("  " * (depth - 1) + f"{code} (L{depth}){tag}")
            for child in self.children.get(code, ()):
                walk(child, depth + 1)

        for root in roots:
            walk(root, 1)
        return lines


def run(lines):
    state = DictState()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "DEF" and len(args) == 2:
                state.do_def(lineno, args[0], args[1])
            elif cmd == "DISABLE" and len(args) == 1:
                state.do_disable(lineno, args[0], "DISABLED")
            elif cmd == "ENABLE" and len(args) == 1:
                state.do_disable(lineno, args[0], "ENABLED")
            elif cmd == "MOVE" and len(args) == 2:
                state.do_move(lineno, args[0], args[1])
            elif cmd == "MAP" and len(args) == 2:
                state.do_map(lineno, args[0], args[1])
            elif cmd == "UNMAP" and len(args) == 1:
                state.do_unmap(lineno, args[0])
            else:
                state.err(lineno, "语法错误", f"无法解析：{line}")
        except Exception as exc:  # 单条命令异常不中断整体流
            state.err(lineno, "内部错误", f"{cmd}: {exc}")
    return state


def report(state):
    out = []
    out.append("== 编码树 ==")
    out.extend(state.render_tree() or ["(空)"])
    out.append("")
    out.append(f"== 当前映射（版本 {state.version}）==")
    if state.mappings:
        out.extend(f"{biz} -> {d}" for biz, d in sorted(state.mappings.items()))
    else:
        out.append("(无)")
    out.append("")
    out.append("== 映射历史（按版本追溯）==")
    if state.map_history:
        for rec in state.map_history:
            end = rec["to_ver"] if rec["to_ver"] is not None else "至今"
            out.append(f"{rec['biz']} -> {rec['dict']}  "
                       f"v{rec['from_ver']} ~ v{end}  [{rec['note']}]")
    else:
        out.append("(无)")
    out.append("")
    out.append("== 错误清单 ==")
    if state.errors:
        out.extend(f"行{ln} [{kind}] {msg}" for ln, kind, msg in state.errors)
    else:
        out.append("(无错误)")
    return "\n".join(out)


DEMO = """\
# ---- 定义编码树 ----
DEF  ROOT   -
DEF  A      ROOT
DEF  A1     A
DEF  A2     A
DEF  A11    A1
DEF  B      ROOT
DEF  B1     B
# ---- 建立映射（v0）----
MAP  biz001 A11
MAP  biz002 A2
MAP  biz003 B1
# ---- 重排：A1 子树整体迁到 B 下（v1），A11 层级 3->4 联动更新 ----
MOVE A1 B
# ---- 重排后旧映射失效：biz001(A11 在迁移子树内) 被报告 ----
# ---- 环检测：把 B 挂到其子孙 B1 下，成环，报告并定位 ----
MOVE B B1
# ---- 停用码仍被映射，拦截 ----
DISABLE A2
MAP  biz004 A2
# ---- 引用不存在编码 ----
MAP  biz005 NOPE
DEF  X      GHOST
# ---- 层数超限演示：深链已占满 8 层，把 D 挂到 C6 下会超限 ----
DEF  C      ROOT
DEF  C1     C
DEF  C2     C1
DEF  C3     C2
DEF  C4     C3
DEF  C5     C4
DEF  C6     C5
DEF  D      ROOT
MOVE D C6
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    print(report(run(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
