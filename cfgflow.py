#!/usr/bin/env python3
"""cfgflow.py — 配置项 / 基线 / 变更 / 审批 单文件状态机工具（仅 Python 标准库）。

用法:
    python3 cfgflow.py              运行内置示例（审批链 + 跨环境基线参照）并输出报告
    python3 cfgflow.py spec.json    处理自定义规格文件
    cat spec.json | python3 cfgflow.py -    从标准输入读取规格

规格(JSON)结构:
    items:     [{"name": "db.port", "default": "5432"}, ...]        配置项定义流
    approvers: [{"name": "alice", "level": 1}, ...]                 审批人层级表
    rules:     {"approval_chain": [1, 2, 3]}                        自定规则: 逐级审批链
    events:    按时间顺序混合的三条流（跨流状态延续，单遍顺序处理）:
        {"type": "baseline", "env": "dev", "version": 1,
         "items": {"db.host": "dev-db", "cache.ttl": "@prod:cache.ttl"}}
            基线流: 版本必须连续(上一版本+1); 值 "@env:item" 表示参照其他环境基线
        {"type": "change", "id": "C1", "env": "dev", "item": "db.port",
         "value": "5433", "action": "submit" | "effect" | "rollback"}
            变更流: 状态机 NEW->SUBMITTED->APPROVED->EFFECTIVE->ROLLED_BACK
                    审批层级不足时 SUBMITTED->REJECTED; 非法跳转一律拦截并报错
        {"type": "approval", "change": "C1", "approver": "alice"}
            审批流: 逐级满足 approval_chain 后变更自动进入 APPROVED

取值规则(自定): 变更未 EFFECTIVE 前，任何取值/参照都解析到旧值
(环境基线值，其次配置项默认值); 变更生效时目标环境基线版本级联 +1;
回滚把基线恢复到上一生效值并再次级联 +1。
"""

import json
import sys

TERMINAL_STATES = {"REJECTED", "ROLLED_BACK"}


class Engine:
    def __init__(self, spec):
        self.defaults = {}
        for entry in spec.get("items", []):
            self.defaults[entry["name"]] = entry.get("default")
        self.approver_level = {}
        for entry in spec.get("approvers", []):
            self.approver_level[entry["name"]] = int(entry["level"])
        rules = spec.get("rules", {})
        self.chain = [int(x) for x in rules.get("approval_chain", [1])]
        self.baselines = {}   # env -> {"version": int, "values": {item: val}, "history": {item: [(had, old)]}}
        self.changes = {}     # id -> {env, item, value, state, step, approved_by}
        self.errors = []
        self.seq = 0

    # ---- 取值规则: 未生效变更不可见，只看基线与默认值 ----
    def resolve(self, env, item):
        baseline = self.baselines.get(env)
        if baseline and item in baseline["values"]:
            return baseline["values"][item]
        return self.defaults.get(item)

    def _resolve_value(self, env, raw):
        if isinstance(raw, str) and raw.startswith("@") and ":" in raw[1:]:
            ref_env, ref_item = raw[1:].split(":", 1)
            if ref_env not in self.baselines:
                self._error("基线参照失败: 环境 '%s' 不存在 (引用 '%s')" % (ref_env, raw))
                return None
            if ref_item not in self.defaults:
                self._error("基线参照失败: 配置项 '%s' 未定义 (引用 '%s')" % (ref_item, raw))
                return None
            return self.resolve(ref_env, ref_item)
        return raw

    def _error(self, message):
        self.errors.append({"seq": self.seq, "message": message})

    # ---- 三条流的事件处理 ----
    def process(self, events):
        for event in events:
            self.seq += 1
            kind = event.get("type")
            if kind == "baseline":
                self._on_baseline(event)
            elif kind == "change":
                self._on_change(event)
            elif kind == "approval":
                self._on_approval(event)
            else:
                self._error("未知事件类型: %r" % (kind,))

    def _on_baseline(self, event):
        env = event.get("env")
        version = event.get("version")
        baseline = self.baselines.setdefault(env, {"version": 0, "values": {}, "history": {}})
        if version != baseline["version"] + 1:
            self._error("环境 '%s' 基线版本不连续: 期望 %d, 收到 %r (已忽略)"
                        % (env, baseline["version"] + 1, version))
            return
        for item, raw in event.get("items", {}).items():
            if item not in self.defaults:
                self._error("基线引用了不存在的配置项 '%s' (环境 '%s', 已跳过)" % (item, env))
                continue
            value = self._resolve_value(env, raw)
            if value is not None:
                baseline["values"][item] = value
        baseline["version"] = version

    def _on_change(self, event):
        action = event.get("action")
        cid = event.get("id")
        if action == "submit":
            self._submit(event)
        elif action == "effect":
            self._effect(cid)
        elif action == "rollback":
            self._rollback(cid)
        else:
            self._error("变更 '%s' 未知动作: %r" % (cid, action))

    def _submit(self, event):
        cid, env, item = event.get("id"), event.get("env"), event.get("item")
        if cid in self.changes:
            self._error("变更编号 '%s' 重复提交 (已拦截)" % cid)
            return
        if item not in self.defaults:
            self._error("变更 '%s' 引用了不存在的配置项 '%s' (已拦截)" % (cid, item))
            return
        if env not in self.baselines:
            self._error("变更 '%s' 目标环境 '%s' 没有基线 (已拦截)" % (cid, env))
            return
        self.changes[cid] = {"env": env, "item": item, "value": event.get("value"),
                             "state": "SUBMITTED", "step": 0, "approved_by": []}

    def _effect(self, cid):
        change = self.changes.get(cid)
        if change is None:
            self._error("生效请求引用了不存在的变更 '%s' (已拦截)" % cid)
            return
        if change["state"] != "APPROVED":
            self._error("变更 '%s' 非法状态跳转: %s -> EFFECTIVE (已拦截)"
                        % (cid, change["state"]))
            return
        env, item = change["env"], change["item"]
        baseline = self.baselines[env]
        value = self._resolve_value(env, change["value"])
        if value is None:
            self._error("变更 '%s' 取值参照解析失败, 保持 APPROVED 未生效" % cid)
            return
        had_old = item in baseline["values"]
        old = baseline["values"].get(item)
        baseline["history"].setdefault(item, []).append((had_old, old))
        baseline["values"][item] = value
        baseline["version"] += 1   # 生效后该环境基线版本级联推进
        change["state"] = "EFFECTIVE"

    def _rollback(self, cid):
        change = self.changes.get(cid)
        if change is None:
            self._error("回滚请求引用了不存在的变更 '%s' (已拦截)" % cid)
            return
        if change["state"] == "REJECTED":
            self._error("变更 '%s' 已被驳回, 禁止回滚 (已拦截)" % cid)
            return
        if change["state"] != "EFFECTIVE":
            self._error("变更 '%s' 非法状态跳转: %s -> ROLLED_BACK (已拦截)"
                        % (cid, change["state"]))
            return
        env, item = change["env"], change["item"]
        baseline = self.baselines[env]
        had_old, old = baseline["history"][item].pop()
        if had_old:
            baseline["values"][item] = old   # 恢复上一生效值
        else:
            baseline["values"].pop(item, None)  # 之前基线没有该项, 回落到默认值
        baseline["version"] += 1
        change["state"] = "ROLLED_BACK"

    def _on_approval(self, event):
        cid, who = event.get("change"), event.get("approver")
        change = self.changes.get(cid)
        if change is None:
            self._error("审批引用了不存在的变更 '%s' (审批人 '%s')" % (cid, who))
            return
        if who not in self.approver_level:
            self._error("审批人 '%s' 不在审批人层级表中 (变更 '%s')" % (who, cid))
            return
        if who in change["approved_by"]:
            self._error("审批人 '%s' 重复审批同一变更 '%s' (已忽略)" % (who, cid))
            return
        if change["state"] != "SUBMITTED":
            self._error("变更 '%s' 已处于 %s 状态, 审批人 '%s' 的审批无效 (重复/超时审批)"
                        % (cid, change["state"], who))
            return
        required = self.chain[change["step"]]
        level = self.approver_level[who]
        if level < required:
            change["state"] = "REJECTED"
            self._error("变更 '%s' 第 %d 级审批要求层级 >= %d, 审批人 '%s' 层级 %d 不足, 变更已驳回"
                        % (cid, change["step"] + 1, required, who, level))
            return
        change["approved_by"].append(who)
        change["step"] += 1
        if change["step"] == len(self.chain):
            change["state"] = "APPROVED"

    # ---- 输出: 配置状态 + 多环境不一致 + 错误清单 ----
    def report(self):
        config_state = {}
        for env in sorted(self.baselines):
            baseline = self.baselines[env]
            config_state[env] = {
                "baseline_version": baseline["version"],
                "values": {item: self.resolve(env, item) for item in sorted(self.defaults)},
            }
        inconsistencies = []
        envs = sorted(self.baselines)
        for item in sorted(self.defaults):
            for i in range(len(envs)):
                for j in range(i + 1, len(envs)):
                    va, vb = self.resolve(envs[i], item), self.resolve(envs[j], item)
                    if va != vb:
                        inconsistencies.append({"item": item,
                                                "env_a": envs[i], "value_a": va,
                                                "env_b": envs[j], "value_b": vb})
        changes = {cid: {"env": c["env"], "item": c["item"], "value": c["value"],
                         "state": c["state"], "approved_by": list(c["approved_by"])}
                   for cid, c in sorted(self.changes.items())}
        return {"config_state": config_state, "changes": changes,
                "inconsistencies": inconsistencies, "errors": self.errors}


def demo_spec():
    """内置示例: 三级审批链 + 跨环境基线参照, 覆盖全部规则分支。"""
    return {
        "items": [{"name": "db.host", "default": "localhost"},
                  {"name": "db.port", "default": "5432"},
                  {"name": "cache.ttl", "default": "300"}],
        "approvers": [{"name": "alice", "level": 1},
                      {"name": "bob", "level": 2},
                      {"name": "carol", "level": 3},
                      {"name": "dave", "level": 1}],
        "rules": {"approval_chain": [1, 2, 3]},
        "events": [
            {"type": "baseline", "env": "dev", "version": 1,
             "items": {"db.host": "dev-db.internal", "db.port": "5432"}},
            {"type": "baseline", "env": "staging", "version": 1,
             "items": {"db.host": "@dev:db.host", "cache.ttl": "120"}},
            {"type": "baseline", "env": "prod", "version": 1,
             "items": {"db.host": "prod-db.internal", "db.port": "5432",
                       "cache.ttl": "@staging:cache.ttl"}},
            {"type": "baseline", "env": "dev", "version": 5,
             "items": {"db.port": "5400"}},
            {"type": "change", "id": "C1", "env": "dev", "item": "db.port",
             "value": "5433", "action": "submit"},
            {"type": "approval", "change": "C1", "approver": "alice"},
            {"type": "approval", "change": "C1", "approver": "alice"},
            {"type": "approval", "change": "C1", "approver": "bob"},
            {"type": "change", "id": "C1", "action": "effect"},
            {"type": "approval", "change": "C1", "approver": "carol"},
            {"type": "change", "id": "C1", "action": "effect"},
            {"type": "change", "id": "C2", "env": "prod", "item": "cache.ttl",
             "value": "@staging:cache.ttl", "action": "submit"},
            {"type": "approval", "change": "C2", "approver": "alice"},
            {"type": "approval", "change": "C2", "approver": "bob"},
            {"type": "approval", "change": "C2", "approver": "carol"},
            {"type": "change", "id": "C2", "action": "effect"},
            {"type": "change", "id": "C3", "env": "dev", "item": "db.host",
             "value": "dev-db-2.internal", "action": "submit"},
            {"type": "approval", "change": "C3", "approver": "alice"},
            {"type": "approval", "change": "C3", "approver": "dave"},
            {"type": "change", "id": "C3", "action": "rollback"},
            {"type": "change", "id": "C1", "action": "rollback"},
            {"type": "change", "id": "C4", "env": "dev", "item": "no.such.item",
             "value": "x", "action": "submit"},
            {"type": "approval", "change": "C99", "approver": "alice"},
            {"type": "change", "id": "C99", "action": "rollback"},
        ],
    }


def main(argv):
    if len(argv) < 2 or argv[1] == "demo":
        spec = demo_spec()
    elif argv[1] == "-":
        spec = json.load(sys.stdin)
    else:
        with open(argv[1], "r", encoding="utf-8") as fh:
            spec = json.load(fh)
    engine = Engine(spec)
    engine.process(spec.get("events", []))
    json.dump(engine.report(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
