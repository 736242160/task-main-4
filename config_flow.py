#!/usr/bin/env python3
"""配置变更流工具（纯标准库，单文件）。

输入：事件流文本，每行一个事件（# 开头为注释，空行忽略）。
四条流（定义/基线/变更/审批）按提交顺序交错，状态跨流延续。

事件语法：
  DEF      <项> <默认值>                     定义配置项
  APPROVER <审批人> <层级>                   定义审批人层级（数字越大越高）
  BASELINE <环境> <版本> [项=值 ...]         建立环境基线（未列项取默认值）
  CHANGE   <变更号> <环境> <项> <新值>       创建变更（DRAFT）
  SUBMIT   <变更号>                          DRAFT -> SUBMITTED
  APPROVE  <变更号> <审批人>                 审批链：层级须不降，达到所需层级即 APPROVED
  REJECT   <变更号>                          SUBMITTED/APPROVED -> REJECTED
  APPLY    <变更号>                          APPROVED -> EFFECTIVE，基线版本级联+1
  ROLLBACK <变更号>                          EFFECTIVE -> ROLLED_BACK，恢复上一生效值
  QUERY    <环境> <项>                       查询当前生效值（未生效变更不影响）

审批规则（自定）：
  - 变更需审批链中最高层级 >= REQUIRED_LEVEL 才视为 APPROVED；
  - 链内审批人层级必须单调不降（逐级上报）；
  - 同一审批人重复审批同一变更 -> 报错。

引用规则（自定）：变更未 EFFECTIVE 前，QUERY 与其他变更的 old_value 均取当前生效（旧）值。

用法：python3 config_flow.py [事件文件]   （无参数时运行内置示例）
"""

import sys

REQUIRED_LEVEL = 2  # 变更生效所需的最低审批层级

STATUSES = ("DRAFT", "SUBMITTED", "APPROVED", "EFFECTIVE", "REJECTED", "ROLLED_BACK")

ALLOWED = {
    ("DRAFT", "SUBMITTED"),
    ("SUBMITTED", "APPROVED"),
    ("SUBMITTED", "REJECTED"),
    ("APPROVED", "EFFECTIVE"),
    ("APPROVED", "REJECTED"),
    ("EFFECTIVE", "ROLLED_BACK"),
}


class Change:
    def __init__(self, cid, env, item, new_value):
        self.cid = cid
        self.env = env
        self.item = item
        self.new_value = new_value
        self.old_value = None
        self.status = "DRAFT"
        self.approvals = []  # [(approver, level)]


class Engine:
    def __init__(self):
        self.items = {}       # 项 -> 默认值
        self.approvers = {}   # 审批人 -> 层级
        self.baselines = {}   # 环境 -> {"version": int, "values": {项: 值}}
        self.changes = {}     # 变更号 -> Change
        self.errors = []      # [(行号, 信息)]
        self.queries = []     # [(行号, 环境, 项, 值)]

    def error(self, lineno, msg):
        self.errors.append((lineno, msg))

    def transition(self, lineno, change, target):
        if (change.status, target) not in ALLOWED:
            self.error(lineno, "变更 %s 非法状态跳转 %s -> %s（已拦截）"
                       % (change.cid, change.status, target))
            return False
        change.status = target
        return True

    def get_change(self, lineno, cid):
        change = self.changes.get(cid)
        if change is None:
            self.error(lineno, "引用不存在的变更号 %s" % cid)
        return change

    def run(self, lines):
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            op, args = parts[0].upper(), parts[1:]
            handler = getattr(self, "do_" + op, None)
            if handler is None:
                self.error(lineno, "未知事件类型 %s" % parts[0])
                continue
            try:
                handler(lineno, args)
            except (IndexError, ValueError):
                self.error(lineno, "事件参数格式错误: %s" % line)

    def do_DEF(self, lineno, args):
        name, default = args[0], args[1]
        if name in self.items:
            self.error(lineno, "重复定义配置项 %s" % name)
            return
        self.items[name] = default

    def do_APPROVER(self, lineno, args):
        self.approvers[args[0]] = int(args[1])

    def do_BASELINE(self, lineno, args):
        env, version = args[0], int(args[1])
        if env in self.baselines:
            self.error(lineno, "环境 %s 基线已存在（版本 %d），重复建立"
                       % (env, self.baselines[env]["version"]))
            return
        values = dict(self.items)
        for pair in args[2:]:
            key, _, val = pair.partition("=")
            if key not in self.items:
                self.error(lineno, "基线引用不存在的配置项 %s" % key)
                continue
            values[key] = val
        self.baselines[env] = {"version": version, "values": values}

    def do_CHANGE(self, lineno, args):
        cid, env, item, new_value = args[0], args[1], args[2], args[3]
        if cid in self.changes:
            self.error(lineno, "变更号 %s 重复创建" % cid)
            return
        if env not in self.baselines:
            self.error(lineno, "变更 %s 引用不存在的环境 %s" % (cid, env))
            return
        if item not in self.items:
            self.error(lineno, "变更 %s 引用不存在的配置项 %s" % (cid, item))
            return
        self.changes[cid] = Change(cid, env, item, new_value)

    def do_SUBMIT(self, lineno, args):
        change = self.get_change(lineno, args[0])
        if change:
            self.transition(lineno, change, "SUBMITTED")

    def do_APPROVE(self, lineno, args):
        cid, who = args[0], args[1]
        change = self.get_change(lineno, cid)
        if change is None:
            return
        if who not in self.approvers:
            self.error(lineno, "未知审批人 %s" % who)
            return
        if change.status not in ("SUBMITTED", "APPROVED"):
            self.error(lineno, "变更 %s 当前状态 %s 不可审批" % (cid, change.status))
            return
        if any(a == who for a, _ in change.approvals):
            self.error(lineno, "审批人 %s 重复审批同一变更 %s" % (who, cid))
            return
        level = self.approvers[who]
        if change.approvals and level < change.approvals[-1][1]:
            self.error(lineno, "审批链层级倒挂：%s(层级%d) 低于前一级（逐级上报规则，已驳回本次审批）"
                       % (who, level))
            return
        change.approvals.append((who, level))
        if level >= REQUIRED_LEVEL and change.status == "SUBMITTED":
            self.transition(lineno, change, "APPROVED")

    def do_REJECT(self, lineno, args):
        change = self.get_change(lineno, args[0])
        if change:
            self.transition(lineno, change, "REJECTED")

    def do_APPLY(self, lineno, args):
        change = self.get_change(lineno, args[0])
        if change is None:
            return
        if not self.transition(lineno, change, "EFFECTIVE"):
            return
        baseline = self.baselines[change.env]
        change.old_value = baseline["values"][change.item]
        baseline["values"][change.item] = change.new_value
        baseline["version"] += 1  # 生效后基线版本级联推进

    def do_ROLLBACK(self, lineno, args):
        change = self.get_change(lineno, args[0])
        if change is None:
            return
        if change.status == "REJECTED":
            self.error(lineno, "变更 %s 已被驳回，不可回滚（已拦截）" % change.cid)
            return
        if not self.transition(lineno, change, "ROLLED_BACK"):
            return
        baseline = self.baselines[change.env]
        baseline["values"][change.item] = change.old_value  # 恢复上一生效值
        baseline["version"] += 1

    def do_QUERY(self, lineno, args):
        env, item = args[0], args[1]
        if env not in self.baselines:
            self.error(lineno, "查询不存在的环境 %s" % env)
            return
        if item not in self.items:
            self.error(lineno, "查询不存在的配置项 %s" % item)
            return
        self.queries.append((lineno, env, item, self.baselines[env]["values"][item]))

    def consistency_report(self):
        pairs = []
        envs = sorted(self.baselines)
        for item in sorted(self.items):
            for i in range(len(envs)):
                for j in range(i + 1, len(envs)):
                    va = self.baselines[envs[i]]["values"].get(item)
                    vb = self.baselines[envs[j]]["values"].get(item)
                    if va != vb:
                        pairs.append((item, envs[i], va, envs[j], vb))
        return pairs

    def report(self, out):
        w = out.write
        w("===== 配置状态 =====\n")
        for env in sorted(self.baselines):
            b = self.baselines[env]
            w("环境 %s (基线版本 v%d):\n" % (env, b["version"]))
            for item in sorted(b["values"]):
                w("  %s = %s\n" % (item, b["values"][item]))
        if self.changes:
            w("变更清单:\n")
            for cid in sorted(self.changes):
                c = self.changes[cid]
                chain = ",".join("%s(L%d)" % a for a in c.approvals) or "无"
                w("  %s [%s] %s.%s -> %s (审批链: %s)\n"
                  % (c.cid, c.status, c.env, c.item, c.new_value, chain))
        if self.queries:
            w("查询结果（生效前值引用旧值）:\n")
            for lineno, env, item, val in self.queries:
                w("  行%d: %s.%s = %s\n" % (lineno, env, item, val))
        w("===== 多环境一致性 =====\n")
        pairs = self.consistency_report()
        if not pairs:
            w("所有环境同项取值一致\n")
        for item, ea, va, eb, vb in pairs:
            w("不一致: 项 %s | 环境 %s=%s <-> 环境 %s=%s\n" % (item, ea, va, eb, vb))
        w("===== 错误报告 =====\n")
        if not self.errors:
            w("无错误\n")
        for lineno, msg in self.errors:
            w("行%d: %s\n" % (lineno, msg))


DEMO = r"""
# ---- 审批链与基线交叉示例 ----
DEF thread_pool 8
DEF cache_size 256
DEF log_level info

APPROVER alice 1
APPROVER bob 2
APPROVER carol 3

BASELINE dev   1 thread_pool=4
BASELINE stage 1 thread_pool=8
BASELINE prod  1 thread_pool=16 cache_size=512

# 变更 C1：完整审批链（alice L1 -> bob L2 达标），生效后 prod 基线级联到 v2
CHANGE C1 prod thread_pool 32
SUBMIT C1
QUERY prod thread_pool
APPROVE C1 alice
QUERY prod thread_pool
APPROVE C1 bob
APPROVE C1 bob
APPLY C1
QUERY prod thread_pool

# 变更 C2：审批层级不足（仅 alice L1），APPLY 属非法跳转被拦截
CHANGE C2 prod cache_size 1024
SUBMIT C2
APPROVE C2 alice
APPLY C2

# 变更 C3：驳回后回滚被拦截
CHANGE C3 prod log_level debug
SUBMIT C3
REJECT C3
ROLLBACK C3

# 变更 C4：生效后回滚，stage 基线恢复上一生效值并推进版本
CHANGE C4 stage cache_size 128
SUBMIT C4
APPROVE C4 alice
APPROVE C4 carol
APPLY C4
ROLLBACK C4

# 引用不存在的项与变更
CHANGE C5 prod no_such_item 1
APPROVE C9 bob

# 变更 C6：仅 dev 环境生效，制造跨环境不一致
CHANGE C6 dev log_level warn
SUBMIT C6
APPROVE C6 bob
APPLY C6
"""


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = DEMO.splitlines()
    engine = Engine()
    engine.run(lines)
    engine.report(sys.stdout)
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
