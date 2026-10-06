#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多式联运提单全程管控工具（纯 Python 标准库，单文件）

输入四组流：
  货物定义: (提单号, 起运地, 目的地)
  段流:     (提单号, 顺序号, 运输方式[公路/铁路/海运], 起点, 讫点, 状态[发运/到达])
  换装流:   (提单号, 顺序号, 状态[完成/异常])   # 顺序号 = 在该段结束后发生的换装
  延误流:   (提单号, 顺序号, 天数)

输出：每票联运状态 + 错误清单。

校验规则：
  1. 段顺序错乱：未发运先到达 -> 报告并定位（提单号+顺序号）
  2. 换装未完成（异常）时下一段发运 -> 拦截
  3. 换装异常后级联调整后续段：后续段全部置为“挂起-待改配”，其发运/到达事件判无效
  4. 段衔接：上段讫点必须等于下段起点
  5. 延误级联：某段延误天数向后累计，后续各段预计到达同步顺延
  6. 引用不存在的提单或段 -> 报告
  7. 同一段重复到达 -> 报告
  8. 跨流状态延续：段/换装/延误三流共享同一提单状态机
  9. 运输方式变更但无换装记录 -> 报告（衔接缺口）

用法：
  python3 multimodal.py            # 运行内置样例（含换装衔接样例）
  python3 multimodal.py data.json  # 从 JSON 文件读取四组流
"""

import json
import sys
from collections import defaultdict

MODES = {"公路", "铁路", "海运"}
SEG_STATUS = {"发运", "到达"}
TS_STATUS = {"完成", "异常"}


class Segment:
    __slots__ = ("seq", "mode", "origin", "dest",
                 "departed", "arrived", "delay_days", "suspended")

    def __init__(self, seq, mode, origin, dest):
        self.seq = seq
        self.mode = mode
        self.origin = origin
        self.dest = dest
        self.departed = False
        self.arrived = False
        self.delay_days = 0        # 本段自身延误
        self.suspended = False     # 因换装异常被级联挂起


class Bill:
    """一票提单的跨流状态机。"""

    def __init__(self, bill_no, origin, destination):
        self.bill_no = bill_no
        self.origin = origin
        self.destination = destination
        self.segments = {}         # seq -> Segment
        self.transshipments = {}   # seq -> 完成/异常（该段结束后的换装）
        self.errors = []           # 本票错误定位信息


class Engine:
    def __init__(self):
        self.bills = {}            # 提单号 -> Bill
        self.errors = []           # 全局错误清单（字符串，含定位）

    # ---------- 错误记录 ----------
    def _err(self, bill_no, where, msg):
        self.errors.append("[提单 %s | %s] %s" % (bill_no, where, msg))
        bill = self.bills.get(bill_no)
        if bill is not None:
            bill.errors.append(msg)

    # ---------- 流 1：货物定义 ----------
    def load_cargo(self, rows):
        for bill_no, origin, destination in rows:
            if bill_no in self.bills:
                self._err(bill_no, "货物定义", "重复定义提单")
                continue
            self.bills[bill_no] = Bill(bill_no, origin, destination)

    # ---------- 流 2：段流 ----------
    def load_segments(self, rows):
        for bill_no, seq, mode, origin, dest, status in rows:
            where = "段 %s" % seq
            bill = self.bills.get(bill_no)
            if bill is None:
                self._err(bill_no, where, "引用不存在的提单")
                continue
            if mode not in MODES:
                self._err(bill_no, where, "非法运输方式: %s" % mode)
                continue
            if status not in SEG_STATUS:
                self._err(bill_no, where, "非法段状态: %s" % status)
                continue
            seg = bill.segments.get(seq)
            if seg is None:
                seg = bill.segments[seq] = Segment(seq, mode, origin, dest)
            else:
                # 同一段再次出现：起讫点/方式不一致要报告
                if (seg.mode, seg.origin, seg.dest) != (mode, origin, dest):
                    self._err(bill_no, where,
                              "段定义冲突: 已有(%s %s->%s) 新报(%s %s->%s)"
                              % (seg.mode, seg.origin, seg.dest, mode, origin, dest))
            if status == "发运":
                if seg.departed:
                    self._err(bill_no, where, "重复发运")
                else:
                    seg.departed = True
            else:  # 到达
                if seg.arrived:
                    self._err(bill_no, where, "重复到达同一段")
                else:
                    seg.arrived = True

    # ---------- 流 3：换装流 ----------
    def load_transshipments(self, rows):
        for bill_no, seq, status in rows:
            where = "换装(段 %s 后)" % seq
            bill = self.bills.get(bill_no)
            if bill is None:
                self._err(bill_no, where, "引用不存在的提单")
                continue
            if status not in TS_STATUS:
                self._err(bill_no, where, "非法换装状态: %s" % status)
                continue
            if seq not in bill.segments:
                self._err(bill_no, where, "引用不存在的段")
                continue
            if seq in bill.transshipments:
                self._err(bill_no, where, "重复换装记录")
                continue
            bill.transshipments[seq] = status

    # ---------- 流 4：延误流 ----------
    def load_delays(self, rows):
        for bill_no, seq, days in rows:
            where = "延误(段 %s)" % seq
            bill = self.bills.get(bill_no)
            if bill is None:
                self._err(bill_no, where, "引用不存在的提单")
                continue
            seg = bill.segments.get(seq)
            if seg is None:
                self._err(bill_no, where, "引用不存在的段")
                continue
            if not isinstance(days, int) or days < 0:
                self._err(bill_no, where, "非法延误天数: %r" % (days,))
                continue
            seg.delay_days += days

    # ---------- 规则评估（跨流状态延续：三流汇合后统一评估） ----------
    def evaluate(self):
        for bill_no in sorted(self.bills):
            self._eval_bill(self.bills[bill_no])

    def _eval_bill(self, bill):
        bn = bill.bill_no
        seqs = sorted(bill.segments)

        # 规则 1：未发运先到达
        for seq in seqs:
            seg = bill.segments[seq]
            if seg.arrived and not seg.departed:
                self._err(bn, "段 %s" % seq,
                          "段顺序错乱: 未发运先到达 (%s %s->%s)"
                          % (seg.mode, seg.origin, seg.dest))

        # 规则 4：段衔接（上段讫点 == 下段起点）
        for prev, nxt in zip(seqs, seqs[1:]):
            a, b = bill.segments[prev], bill.segments[nxt]
            if a.dest != b.origin:
                self._err(bn, "段 %s->%s" % (prev, nxt),
                          "段衔接断裂: 上段讫点[%s] != 下段起点[%s]"
                          % (a.dest, b.origin))

        # 规则 9：方式变更但无换装记录
        for prev, nxt in zip(seqs, seqs[1:]):
            a, b = bill.segments[prev], bill.segments[nxt]
            if a.mode != b.mode and prev not in bill.transshipments:
                self._err(bn, "段 %s->%s" % (prev, nxt),
                          "运输方式变更(%s->%s)但无换装记录" % (a.mode, b.mode))

        # 规则 2/3：换装拦截与异常级联
        # 换装异常 -> 其后所有段级联挂起（自定规则：挂起-待改配，事件判无效）
        abnormal_at = None
        for seq in seqs:
            if bill.transshipments.get(seq) == "异常":
                abnormal_at = seq
                break
        if abnormal_at is not None:
            self._err(bn, "换装(段 %s 后)" % abnormal_at,
                      "换装异常，级联调整: 后续段全部挂起-待改配")
            for seq in seqs:
                if seq > abnormal_at:
                    seg = bill.segments[seq]
                    seg.suspended = True
                    if seg.departed or seg.arrived:
                        self._err(bn, "段 %s" % seq,
                                  "换装异常后仍发运/到达，事件判无效并拦截")

        # 换装未完成（异常）时下一段发运 -> 拦截
        for prev, nxt in zip(seqs, seqs[1:]):
            ts = bill.transshipments.get(prev)
            nxt_seg = bill.segments[nxt]
            if ts == "异常" and nxt_seg.departed:
                self._err(bn, "段 %s" % nxt,
                          "拦截: 换装(段 %s 后)未完成，下一段不得发运" % prev)

        # 规则 5：延误级联顺延（跨流状态延续：延误流作用于段流状态）
        bill.eta_shift = {}          # seq -> 累计顺延天数
        cumulative = 0
        for seq in seqs:
            cumulative += bill.segments[seq].delay_days
            bill.eta_shift[seq] = cumulative

    # ---------- 联运状态 ----------
    def status_of(self, bill):
        seqs = sorted(bill.segments)
        if not seqs:
            return "无段信息"
        if any(s.suspended for s in bill.segments.values()):
            return "换装异常挂起-待改配"
        if bill.errors:
            return "异常(见错误清单)"
        last = bill.segments[seqs[-1]]
        if last.arrived and last.dest == bill.destination:
            return "已送达"
        if last.arrived and last.dest != bill.destination:
            return "到达但讫点与目的地不符"
        for seq in seqs:
            seg = bill.segments[seq]
            if seg.departed and not seg.arrived:
                return "在途(段 %s %s %s->%s)" % (seq, seg.mode, seg.origin, seg.dest)
        if any(s.arrived for s in bill.segments.values()):
            return "换装中/待续运"
        return "待发运"

    # ---------- 输出 ----------
    def report(self):
        lines = []
        lines.append("=" * 60)
        lines.append("联运状态")
        lines.append("=" * 60)
        for bill_no in sorted(self.bills):
            bill = self.bills[bill_no]
            lines.append("提单 %s: %s -> %s | 状态: %s"
                         % (bill_no, bill.origin, bill.destination,
                            self.status_of(bill)))
            for seq in sorted(bill.segments):
                seg = bill.segments[seq]
                flags = []
                flags.append("发运" if seg.departed else "未发运")
                flags.append("到达" if seg.arrived else "未到达")
                if seg.suspended:
                    flags.append("挂起")
                ts = bill.transshipments.get(seq)
                ts_txt = (" | 换装:%s" % ts) if ts else ""
                shift = bill.eta_shift.get(seq, 0)
                eta_txt = (" | 预计到达顺延 %d 天" % shift) if shift else ""
                lines.append("  段 %s [%s] %s -> %s | %s%s%s"
                             % (seq, seg.mode, seg.origin, seg.dest,
                                "/".join(flags), ts_txt, eta_txt))
        lines.append("=" * 60)
        lines.append("错误清单（共 %d 条）" % len(self.errors))
        lines.append("=" * 60)
        if self.errors:
            for i, e in enumerate(self.errors, 1):
                lines.append("%2d. %s" % (i, e))
        else:
            lines.append("无错误")
        return "\n".join(lines)


def run(cargo, segments, transshipments, delays):
    eng = Engine()
    eng.load_cargo(cargo)
    eng.load_segments(segments)
    eng.load_transshipments(transshipments)
    eng.load_delays(delays)
    eng.evaluate()
    return eng.report()


def demo():
    # ---- 换装衔接样例（正常票 BL001：公路->铁路->海运，两次换装完成）----
    cargo = [
        ("BL001", "成都", "汉堡"),
        ("BL002", "重庆", "上海"),   # 换装异常 -> 级联挂起
        ("BL003", "西安", "青岛"),   # 未发运先到达 + 衔接断裂
        ("BL004", "昆明", "广州"),   # 延误级联 + 重复到达
    ]
    segments = [
        # BL001：三段衔接，方式 公路->铁路->海运
        ("BL001", 1, "公路", "成都", "重庆", "发运"),
        ("BL001", 1, "公路", "成都", "重庆", "到达"),
        ("BL001", 2, "铁路", "重庆", "上海", "发运"),
        ("BL001", 2, "铁路", "重庆", "上海", "到达"),
        ("BL001", 3, "海运", "上海", "汉堡", "发运"),
        # BL002：段1 到达后换装异常，段2 仍发运（应被拦截并级联挂起）
        ("BL002", 1, "公路", "重庆", "武汉", "发运"),
        ("BL002", 1, "公路", "重庆", "武汉", "到达"),
        ("BL002", 2, "铁路", "武汉", "上海", "发运"),
        # BL003：段2 未发运先到达；段1讫点 != 段2起点
        ("BL003", 1, "铁路", "西安", "郑州", "发运"),
        ("BL003", 1, "铁路", "西安", "郑州", "到达"),
        ("BL003", 2, "公路", "洛阳", "青岛", "到达"),
        # BL004：延误 + 重复到达
        ("BL004", 1, "公路", "昆明", "贵阳", "发运"),
        ("BL004", 1, "公路", "昆明", "贵阳", "到达"),
        ("BL004", 1, "公路", "昆明", "贵阳", "到达"),   # 重复到达
        ("BL004", 2, "铁路", "贵阳", "广州", "发运"),
        # 引用不存在的提单
        ("BL999", 1, "公路", "甲地", "乙地", "发运"),
    ]
    transshipments = [
        ("BL001", 1, "完成"),   # 重庆：公路->铁路
        ("BL001", 2, "完成"),   # 上海：铁路->海运
        ("BL002", 1, "异常"),   # 武汉换装异常
        ("BL004", 1, "完成"),
        ("BL999", 1, "完成"),   # 引用不存在的提单
        ("BL004", 9, "完成"),   # 引用不存在的段
    ]
    delays = [
        ("BL004", 1, 2),        # 段1 延误 2 天 -> 段1/段2 均顺延 2 天
        ("BL004", 2, 3),        # 段2 再延误 3 天 -> 段2 累计顺延 5 天
        ("BL999", 1, 1),        # 引用不存在的提单
    ]
    print(run(cargo, segments, transshipments, delays))


if __name__ == "__main__":
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
        print(run(data.get("cargo", []), data.get("segments", []),
                  data.get("transshipments", []), data.get("delays", [])))
    else:
        demo()
