#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
log_aggregator.py — 多格式日志识别 / 解析 / 聚合工具（纯标准库，单文件）

功能
----
1. 按模板（Apache / 键值 / JSON）识别每行日志格式；
2. 解析失败的行报告并定位行号；
3. 按行统计异常级别（ERROR / WARN 等）；
4. 跨行堆栈（traceback / Java "at ..." / "Caused by:"）合并为一条事件；
5. 同事件重复出现（指纹 = 模板 + 级别 + 脱敏消息）去重并报告；
6. 模板字段缺失 / 多余报告；
7. 时间字段跨模板统一为 ISO 8601 UTC；
8. 输出事件清单与统计（JSON）。

用法
----
    python3 log_aggregator.py -t templates.json  app.log          # 文件输入
    cat app.log | python3 log_aggregator.py -t templates.json     # 管道输入
    python3 log_aggregator.py --demo                              # 内置示例

templates.json 示例
-------------------
[
  {"name": "apache_combined", "type": "apache",
   "fields": ["ip", "ident", "user", "time", "request", "status", "size", "referer", "agent"]},
  {"name": "app_kv", "type": "kv",
   "fields": ["ts", "level", "msg"]},
  {"name": "app_json", "type": "json",
   "fields": ["timestamp", "level", "message", "service"]}
]
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------- 常量

LEVELS = ("ERROR", "WARN", "INFO", "DEBUG", "TRACE", "OTHER")

LEVEL_ALIAS = {
    "WARNING": "WARN",
    "FATAL": "ERROR",
    "CRITICAL": "ERROR",
    "SEVERE": "ERROR",
    "ERR": "ERROR",
}

LEVEL_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|SEVERE)\b",
    re.IGNORECASE,
)

# Apache common / combined
APACHE_RE = re.compile(
    r'(?P<ip>\S+)\s+(?P<ident>\S+)\s+(?P<user>\S+)\s+'
    r'\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<request>[^"]*)"\s+'
    r'(?P<status>\d{3})\s+(?P<size>\S+)'
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<agent>[^"]*)")?\s*$'
)

KV_PAIR_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S+)')

# 堆栈续行：缩进行 / Java at / Caused by / Python traceback / 省略号
CONTINUATION_RES = [
    re.compile(r"^\s+\S"),                       # 以空白开头
    re.compile(r"^at\s+[\w.$]+\("),              # Java 栈帧
    re.compile(r"^Caused by:"),                  # Java 因果链
    re.compile(r'^\s*File ".*", line \d+'),      # Python 栈帧
    re.compile(r"^Traceback \(most recent call last\):"),
    re.compile(r"^\.\.\.\s*\d*\s*(more|common frames omitted)?"),
    # 异常类行：ValueError: ... / java.lang.NullPointerException: ...
    re.compile(r"^[A-Za-z_$][\w.$]*(Error|Exception|Warning|Interrupt|Exit|Fault|Timeout|Throwable)\b"),
]

TIME_KEYS = ("time", "timestamp", "ts", "@timestamp", "time_local", "date", "datetime")
LEVEL_KEYS = ("level", "severity", "loglevel", "log_level", "pri")
MSG_KEYS = ("msg", "message", "request", "path", "error", "body")

MASK_RES = [
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    (re.compile(r"\d+"), "<N>"),
    (re.compile(r'"[^"]*"'), "<STR>"),
]


# ---------------------------------------------------------------- 时间统一

def normalize_time(raw):
    """把多种时间格式统一为 ISO 8601 UTC 字符串；失败返回 (None, 原因)。"""
    if raw is None:
        return None, "no time field"
    s = str(raw).strip()
    # epoch 秒 / 毫秒
    if re.fullmatch(r"\d{10}(?:\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc).isoformat(), None
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).isoformat(), None
    # ISO 8601（容忍 Z 后缀）
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.replace(tzinfo=timezone.utc).isoformat(), None
    except ValueError:
        pass
    for fmt in (
        "%d/%b/%Y:%H:%M:%S %z",        # Apache: 10/Oct/2000:13:55:36 -0700
        "%Y-%m-%d %H:%M:%S,%f",        # log4j 风格
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(), None
        except ValueError:
            continue
    return None, "unrecognized time format: %r" % s


# ---------------------------------------------------------------- 三种解析器

def parse_apache(line):
    m = APACHE_RE.match(line)
    if not m:
        return None
    return {k: v for k, v in m.groupdict().items() if v is not None}


def parse_kv(line):
    pairs = KV_PAIR_RE.findall(line)
    if not pairs:
        return None
    out = {}
    for key, val in pairs:
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace('\\"', '"')
        out[key] = val
    return out


def parse_json(line):
    s = line.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


PARSERS = {"apache": parse_apache, "kv": parse_kv, "json": parse_json}


# ---------------------------------------------------------------- 辅助

def is_continuation(line):
    return any(r.search(line) for r in CONTINUATION_RES)


def pick(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def extract_level(fields, raw):
    v = pick(fields, LEVEL_KEYS)
    if v:
        lv = str(v).strip().upper()
        return LEVEL_ALIAS.get(lv, lv if lv in LEVELS else "OTHER")
    m = LEVEL_RE.search(raw)
    if m:
        lv = m.group(1).upper()
        return LEVEL_ALIAS.get(lv, lv)
    return "OTHER"


def mask_message(msg):
    for rx, rep in MASK_RES:
        msg = rx.sub(rep, msg)
    return msg


def fingerprint(event):
    """去重规则：同一模板 + 同一级别 + 脱敏后相同的消息视为同一事件。"""
    return (event["template"], event["level"], mask_message(event["message"]))


# ---------------------------------------------------------------- 主流程

def aggregate(lines, templates):
    total_lines = 0
    events = []            # 事件清单
    parse_errors = []      # 解析失败 [{line, text}]
    field_issues = []      # 字段缺失/多余 [{line, template, missing, extra}]
    time_issues = []       # 时间无法统一 [{line, reason}]

    for lineno, raw in enumerate(lines, 1):
        total_lines += 1
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        # 1) 逐模板尝试解析，选“字段命中最多”的模板
        best = None  # (命中数, 模板, 字段dict)
        for tpl in templates:
            parser = PARSERS.get(tpl.get("type"))
            if parser is None:
                continue
            fields = parser(line)
            if fields is None:
                continue
            declared = set(tpl.get("fields", []))
            hit = len(declared & set(fields)) if declared else len(fields)
            if best is None or hit > best[0]:
                best = (hit, tpl, fields)

        # 2) 未匹配 → 堆栈续行合并，否则记解析失败
        if best is None:
            if events and is_continuation(line):
                ev = events[-1]
                ev["stack"].append(line)
                ev["line_end"] = lineno
                ev["raw"] += "\n" + line
            else:
                parse_errors.append({"line": lineno, "text": line})
            continue

        _, tpl, fields = best

        # 3) 字段缺失 / 多余检查
        declared = set(tpl.get("fields", []))
        actual = set(fields)
        missing = sorted(declared - actual)
        extra = sorted(actual - declared) if declared else []
        if missing or extra:
            field_issues.append({
                "line": lineno, "template": tpl.get("name"),
                "missing": missing, "extra": extra,
            })

        # 4) 时间统一
        iso, err = normalize_time(pick(fields, TIME_KEYS))
        if err:
            time_issues.append({"line": lineno, "reason": err})

        # 5) 构造事件
        events.append({
            "line_start": lineno,
            "line_end": lineno,
            "template": tpl.get("name"),
            "level": extract_level(fields, line),
            "time": iso,
            "message": str(pick(fields, MSG_KEYS) or line),
            "fields": fields,
            "stack": [],
            "raw": line,
        })

    # 6) 级别统计（按事件首行计一次）
    level_counts = Counter(ev["level"] for ev in events)
    for lv in LEVELS:
        level_counts.setdefault(lv, 0)

    # 7) 重复事件聚合
    groups = defaultdict(list)
    for ev in events:
        groups[fingerprint(ev)].append(ev)
    duplicates = []
    for (tpl, level, masked), evs in groups.items():
        if len(evs) > 1:
            duplicates.append({
                "template": tpl,
                "level": level,
                "masked_message": masked,
                "count": len(evs),
                "lines": [ev["line_start"] for ev in evs],
            })

    return {
        "events": events,
        "stats": {
            "total_lines": total_lines,
            "events": len(events),
            "parse_errors": len(parse_errors),
            "level_counts": dict(level_counts),
            "duplicate_groups": len(duplicates),
            "duplicate_events": sum(d["count"] - 1 for d in duplicates),
            "field_issues": len(field_issues),
            "time_issues": len(time_issues),
        },
        "parse_errors": parse_errors,
        "field_issues": field_issues,
        "time_issues": time_issues,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------- 演示

DEMO_TEMPLATES = [
    {"name": "apache_combined", "type": "apache",
     "fields": ["ip", "ident", "user", "time", "request", "status", "size", "referer", "agent"]},
    {"name": "app_kv", "type": "kv",
     "fields": ["ts", "level", "service", "msg"]},
    {"name": "app_json", "type": "json",
     "fields": ["timestamp", "level", "message", "service"]},
]

DEMO_LOG = r'''10.0.0.1 - frank [10/Oct/2000:13:55:36 -0700] "GET /apache_pb.gif HTTP/1.0" 200 2326 "http://www.example.com/start.html" "Mozilla/4.08"
ts=2026-10-06T08:00:01Z level=ERROR service=pay msg="charge failed order=1001"
ts=2026-10-06T08:00:02Z level=ERROR service=pay msg="charge failed order=1002"
{"timestamp": "2026-10-06 08:00:03,123", "level": "WARN", "message": "disk usage 91%", "service": "mon"}
ts=2026-10-06T08:00:04Z level=ERROR service=pay msg="charge failed order=1003"
{"timestamp": "2026-10-06T08:00:05Z", "level": "ERROR", "message": "Unhandled exception", "service": "api"}
Traceback (most recent call last):
  File "app.py", line 42, in handle
    raise ValueError("bad input")
ValueError: bad input
this line is totally unparsable @@##
ts=1696622401 level=WARN service=auth msg="token refresh slow"
{"timestamp": "bad-time", "level": "INFO", "message": "hello", "service": "api", "unexpected": 1}
'''


def run_demo():
    report = aggregate(DEMO_LOG.splitlines(), DEMO_TEMPLATES)
    print(json.dumps(report, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="多格式日志识别/聚合工具（纯标准库）")
    ap.add_argument("logfile", nargs="?", help="日志文件（缺省读 stdin）")
    ap.add_argument("-t", "--templates", help="模板 JSON 文件（缺省用内置演示模板）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        run_demo()
        return 0

    if args.templates:
        with open(args.templates, encoding="utf-8") as f:
            templates = json.load(f)
    else:
        templates = DEMO_TEMPLATES

    if args.logfile:
        with open(args.logfile, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    report = aggregate(lines, templates)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
