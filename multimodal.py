#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""multimodal.py — 多式联运提单全程跟踪与核验工具（纯 Python 标准库，单文件）

输入格式（UTF-8 文本，每行一条记录，# 开头为注释，四类流可任意交错，
跨流共享同一提单状态，即"跨流状态延续"）：

  CARGO <提单号> <起运地> <目的地>
  SEG   <提单号> <顺序号> <公路|铁路|海运> <起点> <讫点> <发运|到达>
  SHIFT <提单号> <顺序号> <完成|异常>      # 顺序号 = 换装发生在第几段之后
  DELAY <提单号> <顺序号> <天数>

核验规则：
  1. 段顺序错乱（未发运先到达、前序段未到达就发运下一段）报告并定位行号；
  2. 运输方式变化时须有"完成"的换装记录，否则下一段发运被拦截（段置为受阻）；
  3. 换装异常后级联调整：所有后续未到达段挂起，其后的段事件一律拒绝；
  4. 段衔接：上段到达点须等于下段发运点，首段发运点须等于提单起运地，
     末段讫点须等于提单目的地；
  5. 延误按段累计，预计到达顺延天数 = 本段及之前所有段延误之和（级联顺延）；
  6. 引用不存在的提单或段、重复到达同一段、重复发运、重复换装记录均报告。

用法：
  python3 multimodal.py [输入文件]     # 缺省读标准输入
退出码：有错误为 1，否则为 0。
"""

import sys
from dataclasses import dataclass, field

MODES = {"公路", "铁路", "海运"}


@dataclass
class Segment:
    seq: int
    mode: str
    origin: str
    dest: str
    dispatched: bool = False
    arrived: bool = False
    blocked: bool = False     # 被拦截：换装未完成/前序未就绪
    suspended: bool = False   # 换装异常级联挂起
    delay_days: int = 0

    @property
    def status(self):
        if self.suspended:
            return "挂起(换装异常级联调整)"
        if self.blocked:
            return "受阻(已拦截)"
        if self.arrived:
            return "已到达"
        if self.dispatched:
            return "在途"
        return "待发运"


@dataclass
class Cargo:
    bl: str
    origin: str
    dest: str
    segments: dict = field(default_factory=dict)  # seq -> Segment
    shifts: dict = field(default_factory=dict)    # 第 seq 段之后的换装 -> 状态

    def sorted_segments(self):
        return [self.segments[k] for k in sorted(self.segments)]

    def overall_status(self):
        segs = self.sorted_segments()
        if any(s == "异常" for s in self.shifts.values()):
            return "换装异常"
        if segs and all(s.arrived for s in segs) and segs[-1].dest == self.dest:
            return "已到达"
        if any(s.blocked for s in segs):
            return "受阻"
        for s in segs:
            if s.dispatched and not s.arrived:
                return "在途(第%d段 %s)" % (s.seq, s.mode)
        if segs and segs[-1].arrived:
            return "换装中/待续运"
        return "待发运"


class Tracker:
    def __init__(self):
        self.cargos = {}   # 提单号 -> Cargo
        self.errors = []   # (位置, 提单号, 消息)

    def error(self, lineno, bl, msg):
        loc = "行%d" % lineno if lineno else "汇总"
        self.errors.append((loc, bl or "-", msg))

    # ---- 货物定义 ----
    def handle_cargo(self, lineno, bl, origin, dest):
        if bl in self.cargos:
            self.error(lineno, bl, "重复定义提单")
            return
        self.cargos[bl] = Cargo(bl, origin, dest)

    # ---- 段流 ----
    def handle_seg(self, lineno, bl, seq, mode, origin, dest, status):
        cargo = self.cargos.get(bl)
        if cargo is None:
            self.error(lineno, bl, "引用不存在的提单")
            return
        if mode not in MODES:
            self.error(lineno, bl, "未知运输方式: %s" % mode)
            return
        seg = cargo.segments.get(seq)
        if seg is None:
            seg = Segment(seq, mode, origin, dest)
            cargo.segments[seq] = seg
            # 换装异常发生在本段之前 -> 本段级联挂起
            if any(s == "异常" for k, s in cargo.shifts.items() if k < seq):
                seg.suspended = True
        elif (seg.mode, seg.origin, seg.dest) != (mode, origin, dest):
            self.error(lineno, bl,
                       "段%d定义冲突: 首次定义为 %s %s->%s，本次为 %s %s->%s"
                       % (seq, seg.mode, seg.origin, seg.dest, mode, origin, dest))
        if status == "发运":
            self._dispatch(lineno, cargo, seg)
        else:
            self._arrive(lineno, cargo, seg)

    def _dispatch(self, lineno, cargo, seg):
        if seg.suspended:
            self.error(lineno, cargo.bl,
                       "段%d已因换装异常级联挂起，发运被拒绝" % seg.seq)
            return
        if seg.dispatched:
            self.error(lineno, cargo.bl, "段%d重复发运" % seg.seq)
            return
        if seg.seq == 1:
            if seg.origin != cargo.origin:
                self.error(lineno, cargo.bl,
                           "首段发运点%s与提单起运地%s不一致"
                           % (seg.origin, cargo.origin))
        else:
            prev = cargo.segments.get(seg.seq - 1)
            if prev is None:
                self.error(lineno, cargo.bl,
                           "段顺序错乱: 前序段%d不存在，段%d发运被拦截"
                           % (seg.seq - 1, seg.seq))
                seg.blocked = True
                return
            if not prev.arrived:
                self.error(lineno, cargo.bl,
                           "段顺序错乱: 段%d尚未到达，段%d不得发运，已拦截"
                           % (prev.seq, seg.seq))
                seg.blocked = True
                return
            if prev.dest != seg.origin:
                self.error(lineno, cargo.bl,
                           "段衔接不一致: 段%d到达点%s != 段%d发运点%s"
                           % (prev.seq, prev.dest, seg.seq, seg.origin))
            if prev.mode != seg.mode:
                shift = cargo.shifts.get(seg.seq - 1)
                if shift is None:
                    self.error(lineno, cargo.bl,
                               "换装未登记: %s->%s 无换装记录，段%d发运被拦截"
                               % (prev.mode, seg.mode, seg.seq))
                    seg.blocked = True
                    return
                if shift == "异常":
                    seg.suspended = True
                    self.error(lineno, cargo.bl,
                               "段%d后换装异常，段%d发运被拒绝并级联挂起"
                               % (seg.seq - 1, seg.seq))
                    return
        seg.dispatched = True
        seg.blocked = False

    def _arrive(self, lineno, cargo, seg):
        if seg.suspended:
            self.error(lineno, cargo.bl,
                       "段%d已因换装异常级联挂起，到达事件被拒绝" % seg.seq)
            return
        if not seg.dispatched:
            self.error(lineno, cargo.bl,
                       "段顺序错乱: 段%d未发运先到达" % seg.seq)
            return
        if seg.arrived:
            self.error(lineno, cargo.bl, "段%d重复到达" % seg.seq)
            return
        seg.arrived = True

    # ---- 换装流 ----
    def handle_shift(self, lineno, bl, seq, status):
        cargo = self.cargos.get(bl)
        if cargo is None:
            self.error(lineno, bl, "引用不存在的提单")
            return
        if seq not in cargo.segments:
            self.error(lineno, bl, "换装引用不存在的段%d" % seq)
            return
        if seq in cargo.shifts:
            self.error(lineno, bl, "段%d后重复换装记录" % seq)
            return
        cargo.shifts[seq] = status
        if status == "异常":
            # 级联调整：所有后续未到达段挂起
            for s in cargo.segments.values():
                if s.seq > seq and not s.arrived:
                    s.suspended = True

    # ---- 延误流 ----
    def handle_delay(self, lineno, bl, seq, days_text):
        cargo = self.cargos.get(bl)
        if cargo is None:
            self.error(lineno, bl, "引用不存在的提单")
            return
        seg = cargo.segments.get(seq)
        if seg is None:
            self.error(lineno, bl, "延误引用不存在的段%d" % seq)
            return
        try:
            days = int(days_text)
            if days < 0:
                raise ValueError
        except ValueError:
            self.error(lineno, bl, "延误天数非法: %s" % days_text)
            return
        seg.delay_days += days

    # ---- 汇总校验 ----
    def finalize(self):
        for cargo in self.cargos.values():
            segs = cargo.sorted_segments()
            if segs and segs[-1].dest != cargo.dest:
                self.error(0, cargo.bl,
                           "末段讫点%s与提单目的地%s不一致"
                           % (segs[-1].dest, cargo.dest))

    # ---- 输出 ----
    def report(self, out):
        out.append("=" * 56)
        out.append("联运状态")
        out.append("=" * 56)
        for bl in sorted(self.cargos):
            cargo = self.cargos[bl]
            out.append("提单 %s  %s -> %s  状态: %s"
                       % (bl, cargo.origin, cargo.dest, cargo.overall_status()))
            cumulative = 0
            for seg in cargo.sorted_segments():
                cumulative += seg.delay_days
                line = "  段%d %s %s->%s  %s" % (
                    seg.seq, seg.mode, seg.origin, seg.dest, seg.status)
                notes = []
                if seg.delay_days:
                    notes.append("本段延误+%d天" % seg.delay_days)
                if cumulative and not seg.arrived:
                    notes.append("预计到达顺延+%d天(累计)" % cumulative)
                if notes:
                    line += "  [" + "; ".join(notes) + "]"
                out.append(line)
            for seq in sorted(cargo.shifts):
                out.append("  换装: 第%d段后 %s" % (seq, cargo.shifts[seq]))
        out.append("")
        out.append("=" * 56)
        out.append("错误清单 (%d 条)" % len(self.errors))
        out.append("=" * 56)
        if not self.errors:
            out.append("无")
        for loc, bl, msg in self.errors:
            out.append("[%s] %s: %s" % (loc, bl, msg))
        return "\n".join(out)


def process(tracker, lines):
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind, args = parts[0], parts[1:]
        if kind == "CARGO" and len(args) == 3:
            tracker.handle_cargo(lineno, *args)
        elif kind == "SEG" and len(args) == 6:
            bl, seq_text, mode, origin, dest, status = args
            if status not in ("发运", "到达"):
                tracker.error(lineno, bl, "未知段状态: %s" % status)
                continue
            try:
                seq = int(seq_text)
            except ValueError:
                tracker.error(lineno, bl, "顺序号非法: %s" % seq_text)
                continue
            tracker.handle_seg(lineno, bl, seq, mode, origin, dest, status)
        elif kind == "SHIFT" and len(args) == 3:
            bl, seq_text, status = args
            if status not in ("完成", "异常"):
                tracker.error(lineno, bl, "未知换装状态: %s" % status)
                continue
            try:
                seq = int(seq_text)
            except ValueError:
                tracker.error(lineno, bl, "顺序号非法: %s" % seq_text)
                continue
            tracker.handle_shift(lineno, bl, seq, status)
        elif kind == "DELAY" and len(args) == 3:
            bl, seq_text, days = args
            try:
                seq = int(seq_text)
            except ValueError:
                tracker.error(lineno, bl, "顺序号非法: %s" % seq_text)
                continue
            tracker.handle_delay(lineno, bl, seq, days)
        else:
            tracker.error(lineno, "-", "格式错误: %s" % line)
    tracker.finalize()


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    tracker = Tracker()
    process(tracker, lines)
    print(tracker.report([]))
    return 1 if tracker.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
