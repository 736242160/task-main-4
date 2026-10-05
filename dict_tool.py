#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dict_tool.py — 数据字典编码树 / 重排 / 映射 单文件工具（纯标准库）

输入协议（按行处理，三流合一、状态跨流延续；# 开头为注释，空行忽略）：
    DEF     <码> <上级码|-> [名称]     定义编码（上级须已存在，'-' 表示根）
    MOVE    <码> <新上级码|->          重排：子树整体迁移，子码层级联动更新
    MAP     <业务码> <字典码>          建立/改绑映射（停用码拦截）
    UNMAP   <业务码> <字典码>          解除映射
    DISABLE <码>                       停用编码（其有效映射失效并报告）
    ENABLE  <码>                       启用编码

版本规则（自定）：版本号从 1 开始，每成功应用一条改变状态的事件后 +1；
映射历史按 (版本, 操作) 逐条记录，可按业务码追溯。

校验规则：
  - 上级/编码不存在          -> 拒绝并报告
  - 重排成环（新上级是自身或自身后代）-> 拒绝并定位成环码及成环路径
  - 重排/新增后层数超过 MAX_DEPTH     -> 拒绝并报告
  - 映射到停用码              -> 拦截并报告
  - 重排/停用后旧映射失效      -> 失效并报告（写入历史）

输出：编码树、有效映射、映射历史（按版本）、错误清单。

用法：
    python3 dict_tool.py [输入文件]     # 缺省读 stdin
    python3 dict_tool.py --demo         # 运行内置样例（含重排迁移与环检测）
"""

import sys
from collections import defaultdict

MAX_DEPTH = 6  # 允许的最大层数（根为第 1 层）
ROOT = "-"     # 根占位符


class DictState:
    def __init__(self):
        self.parent = {}          # 码 -> 上级码（根无条目）
        self.name = {}            # 码 -> 名称
        self.disabled = set()     # 停用码集合
        self.mapping = {}         # 业务码 -> 字典码（当前有效）
        self.history = defaultdict(list)  # 业务码 -> [(版本, 操作, 字典码, 备注)]
        self.version = 1
        self.errors = []          # (行号, 类型, 详情)
        self._root_set = set()    # 根码集合

    # ---------- 基础查询 ----------
    def exists(self, code):
        return code in self.parent or code in self._root_set

    def level(self, code):
        """码的层数（根=1）。码不存在返回 None。"""
        if code not in self.parent and code not in self._root_set:
            return None
        lv, cur, seen = 1, code, set()
        while cur in self.parent:
            cur = self.parent[cur]
            if cur in seen:  # 防御：数据中已有环
                return None
            seen.add(cur)
            lv += 1
        return lv

    def children_of(self, code):
        return [c for c, p in self.parent.items() if p == code]

    def subtree(self, code):
        """含自身的子树（按层序）。"""
        out, queue = [], [code]
        while queue:
            cur = queue.pop(0)
            out.append(cur)
            queue.extend(self.children_of(cur))
        return out

    def ancestor_chain(self, code, stop=None):
        """从 code 向上到根（或到 stop，含 stop）的祖先链。"""
        chain, cur, seen = [], code, set()
        while cur in self.parent:
            cur = self.parent[cur]
            if cur in seen:
                break
            seen.add(cur)
            chain.append(cur)
            if cur == stop:
                break
        return chain

    # ---------- 事件 ----------
    def define(self, lineno, code, parent, name=""):
        if code in self.parent or code in self._root_set:
            self.errors.append((lineno, "重复定义", f"码 {code} 已存在"))
            return
        if parent == ROOT:
            self._root_set.add(code)
        else:
            if not self.exists(parent):
                self.errors.append((lineno, "引用不存在编码",
                                    f"DEF {code}: 上级 {parent} 不存在"))
                return
            if self.level(parent) + 1 > MAX_DEPTH:
                self.errors.append((lineno, "层数超限",
                                    f"DEF {code}: 上级 {parent} 在第 {self.level(parent)} 层，"
                                    f"新增后为第 {self.level(parent)+1} 层，超过上限 {MAX_DEPTH}"))
                return
            self.parent[code] = parent
        self.name[code] = name or code
        self.version += 1

    def move(self, lineno, code, new_parent):
        if not self.exists(code):
            self.errors.append((lineno, "引用不存在编码",
                                f"MOVE {code}: 码不存在"))
            return
        if new_parent != ROOT and not self.exists(new_parent):
            self.errors.append((lineno, "引用不存在编码",
                                f"MOVE {code}: 新上级 {new_parent} 不存在"))
            return
        # ---- 环检测：新上级不能是自身或自身后代 ----
        if new_parent != ROOT:
            chain = self.ancestor_chain(new_parent, stop=code)
            if new_parent == code or (chain and chain[-1] == code):
                if new_parent == code:
                    cycle = f"{code} -> {code}(自环)"
                else:
                    path = list(reversed(chain))
                    cycle = " -> ".join(path + [new_parent]) + f" -> {code}(成环)"
                self.errors.append((lineno, "重排成环",
                                    f"MOVE {code} -> {new_parent}: 成环码={code}，"
                                    f"成环路径 {cycle}，已拒绝"))
                return
        # ---- 层数校验：子树整体迁移后最深一层 ----
        sub = self.subtree(code)
        rel = {code: 0}
        for node in sub[1:]:
            rel[node] = rel[self.parent[node]] + 1
        base = 0 if new_parent == ROOT else self.level(new_parent)
        deepest = base + 1 + max(rel.values())
        if deepest > MAX_DEPTH:
            self.errors.append((lineno, "层数超限",
                                f"MOVE {code} -> {new_parent}: 迁移后子树最深第 {deepest} 层，"
                                f"超过上限 {MAX_DEPTH}，已拒绝"))
            return
        # ---- 应用迁移 ----
        old_parent = self.parent.get(code, ROOT)
        if new_parent == ROOT:
            self.parent.pop(code, None)
            self._root_set.add(code)
        else:
            self.parent[code] = new_parent
            self._root_set.discard(code)
        self.version += 1
        # ---- 旧映射失效：凡指向被迁移子树的映射 ----
        moved = set(sub)
        for biz, dcode in list(self.mapping.items()):
            if dcode in moved:
                del self.mapping[biz]
                self.history[biz].append(
                    (self.version, "失效", dcode,
                     f"重排 {code}: {old_parent}->{new_parent}，旧映射失效"))
                self.errors.append((lineno, "旧映射失效",
                                    f"映射 {biz} -> {dcode} 因重排 {code} 失效"))

    def map(self, lineno, biz, dcode):
        if not self.exists(dcode):
            self.errors.append((lineno, "引用不存在编码",
                                f"MAP {biz} -> {dcode}: 字典码不存在"))
            return
        if dcode in self.disabled:
            self.errors.append((lineno, "停用码被映射",
                                f"MAP {biz} -> {dcode}: 字典码已停用，已拦截"))
            return
        old = self.mapping.get(biz)
        if old == dcode:
            return  # 幂等：重复映射同一码不产生历史
        self.mapping[biz] = dcode
        if old:
            self.history[biz].append((self.version, "改绑", old, f"改绑为 {dcode}"))
        self.history[biz].append((self.version, "生效", dcode,
                                  f"改绑自 {old}" if old else "新建"))
        self.version += 1

    def unmap(self, lineno, biz, dcode):
        if self.mapping.get(biz) != dcode:
            self.errors.append((lineno, "映射不存在",
                                f"UNMAP {biz} -> {dcode}: 当前无此有效映射"))
            return
        del self.mapping[biz]
        self.history[biz].append((self.version, "解除", dcode, "主动解除"))
        self.version += 1

    def disable(self, lineno, code):
        if not self.exists(code):
            self.errors.append((lineno, "引用不存在编码",
                                f"DISABLE {code}: 码不存在"))
            return
        self.disabled.add(code)
        self.version += 1
        for biz, dcode in list(self.mapping.items()):
            if dcode == code:
                del self.mapping[biz]
                self.history[biz].append((self.version, "失效", dcode, "字典码停用"))
                self.errors.append((lineno, "旧映射失效",
                                    f"映射 {biz} -> {dcode} 因停用失效"))

    def enable(self, lineno, code):
        if not self.exists(code):
            self.errors.append((lineno, "引用不存在编码",
                                f"ENABLE {code}: 码不存在"))
            return
        self.disabled.discard(code)
        self.version += 1

    # ---------- 输出 ----------
    def render_tree(self):
        lines = []
        def walk(code, depth):
            mark = " [停用]" if code in self.disabled else ""
            lines.append("  " * (depth - 1) + f"{code} (L{depth}) {self.name.get(code,'')}{mark}")
            for child in sorted(self.children_of(code)):
                walk(child, depth + 1)
        for root in sorted(self._root_set):
            walk(root, 1)
        return lines


def run(lines):
    st = DictState()
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        op, args = parts[0].upper(), parts[1:]
        try:
            if op == "DEF" and len(args) >= 2:
                st.define(lineno, args[0], args[1], " ".join(args[2:]))
            elif op == "MOVE" and len(args) == 2:
                st.move(lineno, args[0], args[1])
            elif op == "MAP" and len(args) == 2:
                st.map(lineno, args[0], args[1])
            elif op == "UNMAP" and len(args) == 2:
                st.unmap(lineno, args[0], args[1])
            elif op == "DISABLE" and len(args) == 1:
                st.disable(lineno, args[0])
            elif op == "ENABLE" and len(args) == 1:
                st.enable(lineno, args[0])
            else:
                st.errors.append((lineno, "语法错误", f"无法解析: {line}"))
        except Exception as exc:  # 防御：单行错误不中断后续流
            st.errors.append((lineno, "内部错误", f"{op}: {exc}"))
    return st


def report(st):
    out = []
    out.append("== 编码树 ==")
    out.extend(st.render_tree() or ["(空)"])
    out.append("")
    out.append("== 有效映射 ==")
    if st.mapping:
        for biz in sorted(st.mapping):
            out.append(f"  {biz} -> {st.mapping[biz]}")
    else:
        out.append("  (无)")
    out.append("")
    out.append("== 映射历史（按版本追溯） ==")
    if st.history:
        for biz in sorted(st.history):
            out.append(f"  {biz}:")
            for ver, op, dcode, note in st.history[biz]:
                out.append(f"    v{ver:<3} {op:<4} {dcode:<8} {note}")
    else:
        out.append("  (无)")
    out.append("")
    out.append("== 错误清单 ==")
    if st.errors:
        for lineno, kind, msg in st.errors:
            out.append(f"  行{lineno:<3} [{kind}] {msg}")
    else:
        out.append("  (无)")
    out.append("")
    out.append(f"当前版本: v{st.version}")
    return "\n".join(out)


DEMO = """\
# ---- 样例：建树 + 映射 + 重排迁移 + 环检测 ----
DEF 中国 - 根
DEF 浙江 中国 省
DEF 杭州 浙江 市
DEF 西湖区 杭州 区
DEF 江苏 中国 省
DEF 南京 江苏 市
DEF 北京 - 直辖市
DEF 朝阳区 北京 区
MAP BIZ001 西湖区
MAP BIZ002 南京
MAP BIZ003 朝阳区
# 重排：杭州(含西湖区)整体迁到江苏下，子码层级联动更新
MOVE 杭州 江苏
# 重排后旧映射失效：BIZ001 -> 西湖区 应报告失效
# 环检测：把 中国 挂到 西湖区 下（子码成为祖先的上级）-> 拒绝并定位
MOVE 中国 西湖区
# 自环
MOVE 北京 北京
# 停用码被映射 -> 拦截
DISABLE 朝阳区
MAP BIZ004 朝阳区
# 引用不存在编码
MAP BIZ005 不存在的码
DEF 孤儿 不存在上级
# 层数超限（上限 6 层）
DEF A1 - 链1
DEF A2 A1 链2
DEF A3 A2 链3
DEF A4 A3 链4
DEF A5 A4 链5
DEF A6 A5 链6
DEF A7 A6 链7
# 改绑与历史追溯
MAP BIZ002 南京
MAP BIZ002 西湖区
UNMAP BIZ002 西湖区
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    print(report(run(lines)))


if __name__ == "__main__":
    main(sys.argv)
