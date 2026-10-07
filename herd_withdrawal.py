#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
herd_withdrawal.py — 休药期 / 转群 / 出栏 状态追踪工具（纯 Python 标准库，单文件）

用法:
    python3 herd_withdrawal.py 输入文件 [更多输入文件...]

输入格式（文本，每行一条指令，# 之后为注释，空行忽略；多个文件按顺序处理，
状态跨文件延续）:

    DRUG      <兽药名> <休药期天数>          定义兽药
    GROUP     <群号> <初始数量>              建群
    MED       <群号> <兽药名> <YYYY-MM-DD>   用药
    TRANSFER  <源群> <目标群> <数量>         转群/合群
    QUAR      <群号> <PASS|FAIL>             检疫结果登记
    SLAUGHTER <群号> <YYYY-MM-DD> <数量>     出栏
    STATUS                                 立即打印当前群状态

自定规则（全部在此声明，计算均严格按此执行）:

    R1 休药期到期日 = 用药日期 + 休药期天数；到期日当天及之后允许出栏。
       剩余天数 = max(0, 到期日 - 当日)。
    R2 重复用药：每次用药独立计算到期日，群的有效休药期取所有用药记录中
       最晚的到期日（即最长约束，天然实现叠加）。
    R3 转群：目标群继承源群的全部用药记录（与自身记录合并，实际效果即取最长
       剩余期）；目标群不存在时自动建群（混群：多个源并入同一目标，逐条
       TRANSFER 即可，目标群累积继承所有源的最长剩余期）。
       任一来源群检疫不合格，则目标群标记为不合格。
    R4 源群转出后数量归零即解散；对已解散群用药、再转出、出栏一律报错。
       部分转出时源群保留自身用药记录（剩余动物仍在休药期内）。
    R5 出栏拦截：休药期剩余 > 0（报告剩余天数）、检疫不合格、数量不足、
       群不存在或已解散，均拦截并记入错误清单。
    R6 引用不存在的群或兽药，报错并跳过该指令，不影响后续处理。
"""

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta


# ---------------------------------------------------------------- 数据模型

@dataclass
class MedRecord:
    drug: str
    start: date
    end: date          # 到期日 = start + 休药期天数（R1）


@dataclass
class Group:
    name: str
    count: int
    meds: list = field(default_factory=list)   # List[MedRecord]
    quarantine: str = "PASS"                   # PASS / FAIL
    dissolved: bool = False

    def withdrawal_end(self):
        """有效休药期到期日：所有用药记录中最晚的到期日（R2）。"""
        if not self.meds:
            return None
        return max(m.end for m in self.meds)

    def remaining_days(self, on):
        end = self.withdrawal_end()
        if end is None:
            return 0
        return max(0, (end - on).days)


# ---------------------------------------------------------------- 引擎

class Farm:
    def __init__(self):
        self.drugs = {}          # 兽药名 -> 休药期天数
        self.groups = {}         # 群号 -> Group
        self.errors = []         # 错误清单 [(位置, 信息)]
        self.last_date = None    # 已处理事件中的最大日期，用于状态报告

    # -- 工具 ----------------------------------------------------------
    def _err(self, where, msg):
        self.errors.append((where, msg))
        print("  [拦截/错误] %s: %s" % (where, msg))

    def _touch_date(self, d):
        if self.last_date is None or d > self.last_date:
            self.last_date = d

    def _get_group(self, where, name, need_active=True):
        g = self.groups.get(name)
        if g is None:
            self._err(where, "群 '%s' 不存在" % name)
            return None
        if need_active and g.dissolved:
            self._err(where, "群 '%s' 已解散（R4），指令忽略" % name)
            return None
        return g

    # -- 指令 ----------------------------------------------------------
    def do_drug(self, where, name, days):
        if days < 0:
            self._err(where, "兽药 '%s' 休药期天数不能为负" % name)
            return
        self.drugs[name] = days

    def do_group(self, where, name, count):
        if name in self.groups and not self.groups[name].dissolved:
            self._err(where, "群 '%s' 已存在" % name)
            return
        if count <= 0:
            self._err(where, "群 '%s' 初始数量必须为正" % name)
            return
        self.groups[name] = Group(name, count)

    def do_med(self, where, gname, drug, day):
        g = self._get_group(where, gname)
        if g is None:
            return
        if drug not in self.drugs:
            self._err(where, "兽药 '%s' 未定义" % drug)
            return
        end = day + timedelta(days=self.drugs[drug])   # R1
        g.meds.append(MedRecord(drug, day, end))       # R2：独立记录，取最晚
        self._touch_date(day)
        print("  [用药] %s <- %s @%s，到期日 %s" % (gname, drug, day, end))

    def do_transfer(self, where, src_name, dst_name, qty):
        src = self._get_group(where, src_name)
        if src is None:
            return
        if qty <= 0 or qty > src.count:
            self._err(where, "转群数量 %d 非法（源群 '%s' 现存 %d）"
                      % (qty, src_name, src.count))
            return
        dst = self.groups.get(dst_name)
        if dst is None or dst.dissolved:               # R3：自动建群（混群）
            dst = Group(dst_name, 0)
            self.groups[dst_name] = dst
            print("  [建群] 目标群 '%s' 自动创建" % dst_name)
        # R3：目标群继承源群全部用药记录（合并后按最晚到期日生效）
        dst.meds.extend(MedRecord(m.drug, m.start, m.end) for m in src.meds)
        if src.quarantine == "FAIL":
            dst.quarantine = "FAIL"
        src.count -= qty
        dst.count += qty
        end = dst.withdrawal_end()
        print("  [转群] %s -> %s 数量 %d；目标群继承休药期，最晚到期日 %s"
              % (src_name, dst_name, qty, end if end else "无"))
        if src.count == 0:                             # R4：源群解散
            src.dissolved = True
            print("  [解散] 源群 '%s' 数量归零，解散" % src_name)

    def do_quar(self, where, gname, result):
        g = self._get_group(where, gname)
        if g is None:
            return
        g.quarantine = result
        print("  [检疫] %s => %s" % (gname, result))

    def do_slaughter(self, where, gname, day, qty):
        g = self._get_group(where, gname)
        if g is None:
            return
        self._touch_date(day)
        if g.quarantine == "FAIL":                     # R5：检疫拦截
            self._err(where, "群 '%s' 检疫不合格，禁止出栏" % gname)
            return
        rem = g.remaining_days(day)                    # R5：休药期拦截
        if rem > 0:
            self._err(where, "群 '%s' 仍在休药期内，禁止出栏；剩余 %d 天"
                      "（到期日 %s）" % (gname, rem, g.withdrawal_end()))
            return
        if qty <= 0 or qty > g.count:
            self._err(where, "出栏数量 %d 非法（群 '%s' 现存 %d）"
                      % (qty, gname, g.count))
            return
        g.count -= qty
        print("  [出栏] %s @%s 数量 %d，放行；剩余存栏 %d"
              % (gname, day, qty, g.count))
        if g.count == 0:
            g.dissolved = True
            print("  [解散] 群 '%s' 全部出栏，解散" % gname)

    # -- 状态输出 ------------------------------------------------------
    def print_status(self):
        ref = self.last_date or date.today()
        print("\n===== 群状态（基准日 %s）=====" % ref)
        if not self.groups:
            print("  （无群）")
        for name in sorted(self.groups):
            g = self.groups[name]
            state = "已解散" if g.dissolved else "在养"
            print("  群 %-6s 存栏 %-5d 状态 %-4s 检疫 %s"
                  % (name, g.count, state, g.quarantine))
            for m in g.meds:
                print("      用药 %-8s 起始 %s  到期日 %s"
                      % (m.drug, m.start, m.end))
            if not g.dissolved:
                rem = g.remaining_days(ref)
                print("      有效休药期剩余：%d 天" % rem)

    def print_errors(self):
        print("\n===== 错误/拦截清单（共 %d 条）=====" % len(self.errors))
        for where, msg in self.errors:
            print("  %s: %s" % (where, msg))


# ---------------------------------------------------------------- 解析

def parse_date(where, s, farm):
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        farm._err(where, "日期 '%s' 非法，应为 YYYY-MM-DD" % s)
        return None


def run_file(path, farm):
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            where = "%s:%d" % (path, lineno)
            parts = line.split()
            cmd, args = parts[0].upper(), parts[1:]
            try:
                if cmd == "DRUG" and len(args) == 2:
                    farm.do_drug(where, args[0], int(args[1]))
                elif cmd == "GROUP" and len(args) == 2:
                    farm.do_group(where, args[0], int(args[1]))
                elif cmd == "MED" and len(args) == 3:
                    d = parse_date(where, args[2], farm)
                    if d:
                        farm.do_med(where, args[0], args[1], d)
                elif cmd == "TRANSFER" and len(args) == 3:
                    farm.do_transfer(where, args[0], args[1], int(args[2]))
                elif cmd == "QUAR" and len(args) == 2 \
                        and args[1].upper() in ("PASS", "FAIL"):
                    farm.do_quar(where, args[0], args[1].upper())
                elif cmd == "SLAUGHTER" and len(args) == 3:
                    d = parse_date(where, args[1], farm)
                    if d:
                        farm.do_slaughter(where, args[0], d, int(args[2]))
                elif cmd == "STATUS" and not args:
                    farm.print_status()
                else:
                    farm._err(where, "无法解析的指令: %s" % line)
            except ValueError:
                farm._err(where, "数值参数非法: %s" % line)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    farm = Farm()
    for path in argv[1:]:
        print(">>> 处理输入流 %s" % path)
        run_file(path, farm)
    farm.print_status()
    farm.print_errors()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
