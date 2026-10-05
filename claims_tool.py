#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""理赔事故归并与分摊工具（纯 Python 标准库，单文件）。

输入流（文本，每行一条指令，# 开头为注释，空行忽略）：
  POLICY  <保单号> <保额> <险种>
  REPORT  <事故号> <保单号> <损失额> [责任比例]   # 比例可省，省略时按损失额占比分摊
  ASSESS  <事故号> <定损额>
  PAYOUT  <事故号> <保单号> <赔付额>
  CLOSE   <事故号>

用法：
  python3 claims_tool.py input.txt
  cat input.txt | python3 claims_tool.py

规则：
  1. 同一事故被多家保单报案 -> 归并为一条事故并记录归并信息；
  2. 事故定损按所有报案保单的责任比例分摊；比例缺失时按各保单报案损失额占比分摊；
     显式给出的比例之和不为 1 -> 报告错误并回退为按损失占比分摊；
  3. 单保单累计赔付（跨事故）超其保额 -> 拦截并定位保单号；
  4. 赔付引用未归并的报案（事故/保单无对应报案记录）-> 拦截；
  5. 定损额超事故总报案损失 -> 报告；
  6. 事故结案后新增报案 -> 拦截；
  7. 全部指令单遍顺序处理，跨流状态延续；
  8. 输出事故分摊表、保单状态与错误清单。
"""

import sys
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
ONE = Decimal("1")
RATIO_TOL = Decimal("0.000001")


def to_money(text):
    return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(amount):
    return f"{amount:,.2f}"


class Policy:
    __slots__ = ("pid", "limit", "kind", "paid")

    def __init__(self, pid, limit, kind):
        self.pid = pid
        self.limit = limit
        self.kind = kind
        self.paid = Decimal("0.00")


class Report:
    __slots__ = ("pid", "loss", "ratio")

    def __init__(self, pid, loss, ratio):
        self.pid = pid
        self.loss = loss
        self.ratio = ratio  # Decimal 或 None


class Accident:
    __slots__ = ("aid", "reports", "assessed", "closed", "over_loss", "ratio_warned")

    def __init__(self, aid):
        self.aid = aid
        self.reports = {}      # pid -> Report（保留报案顺序）
        self.assessed = None
        self.closed = False
        self.over_loss = False
        self.ratio_warned = False

    @property
    def total_loss(self):
        total = Decimal("0.00")
        for rep in self.reports.values():
            total += rep.loss
        return total


class Engine:
    def __init__(self):
        self.policies = {}
        self.accidents = {}
        self.errors = []   # (行号, 级别, 消息)
        self.merges = []   # (行号, 消息)

    # ---- 错误/信息记录 ----
    def error(self, lineno, msg):
        self.errors.append((lineno, "拦截", msg))

    def warn(self, lineno, msg):
        self.errors.append((lineno, "报告", msg))

    def check_ratios(self, lineno, acc):
        if acc.ratio_warned or not acc.reports:
            return
        ratios = [r.ratio for r in acc.reports.values()]
        given = [r for r in ratios if r is not None]
        if given and len(given) != len(ratios):
            self.warn(lineno, f"事故 {acc.aid}: 部分报案缺失责任比例，将回退为按损失占比分摊")
            acc.ratio_warned = True
        elif given:
            total = sum(given)
            if abs(total - ONE) > RATIO_TOL:
                self.warn(
                    lineno,
                    f"事故 {acc.aid}: 责任比例之和为 {total}（不为 1），将回退为按损失占比分摊",
                )
                acc.ratio_warned = True

    # ---- 指令处理 ----
    def do_policy(self, lineno, args):
        if len(args) != 3:
            self.error(lineno, "POLICY 格式错误，应为: POLICY <保单号> <保额> <险种>")
            return
        pid, limit_s, kind = args
        try:
            limit = to_money(limit_s)
        except InvalidOperation:
            self.error(lineno, f"POLICY {pid}: 保额 '{limit_s}' 不是合法数字")
            return
        if limit <= 0:
            self.error(lineno, f"POLICY {pid}: 保额必须为正数")
            return
        if pid in self.policies:
            self.error(lineno, f"POLICY {pid}: 保单重复定义")
            return
        self.policies[pid] = Policy(pid, limit, kind)

    def do_report(self, lineno, args):
        if len(args) not in (3, 4):
            self.error(lineno, "REPORT 格式错误，应为: REPORT <事故号> <保单号> <损失额> [责任比例]")
            return
        aid, pid, loss_s = args[0], args[1], args[2]
        ratio = None
        if len(args) == 4:
            try:
                ratio = Decimal(args[3])
            except InvalidOperation:
                self.error(lineno, f"REPORT {aid}/{pid}: 责任比例 '{args[3]}' 不是合法数字")
                return
            if ratio < 0 or ratio > ONE:
                self.error(lineno, f"REPORT {aid}/{pid}: 责任比例 {ratio} 超出 [0,1] 区间")
                return
        try:
            loss = to_money(loss_s)
        except InvalidOperation:
            self.error(lineno, f"REPORT {aid}/{pid}: 损失额 '{loss_s}' 不是合法数字")
            return
        if loss < 0:
            self.error(lineno, f"REPORT {aid}/{pid}: 损失额不能为负")
            return
        if pid not in self.policies:
            self.error(lineno, f"REPORT {aid}/{pid}: 保单 {pid} 未定义，报案拦截")
            return
        acc = self.accidents.get(aid)
        if acc is not None and acc.closed:
            self.error(lineno, f"REPORT {aid}/{pid}: 事故 {aid} 已结案，新增报案拦截")
            return
        if acc is None:
            acc = Accident(aid)
            self.accidents[aid] = acc
        if pid in acc.reports:
            self.error(lineno, f"REPORT {aid}/{pid}: 该保单在此事故下已报案，重复报案拦截")
            return
        if acc.reports:
            merged = "、".join(acc.reports)
            self.merges.append(
                (lineno, f"事故 {aid}: 保单 {pid} 的报案与已报案保单（{merged}）归并为同一事故")
            )
        acc.reports[pid] = Report(pid, loss, ratio)
        if acc.assessed is not None:
            self.check_ratios(lineno, acc)

    def do_assess(self, lineno, args):
        if len(args) != 2:
            self.error(lineno, "ASSESS 格式错误，应为: ASSESS <事故号> <定损额>")
            return
        aid, amount_s = args
        try:
            amount = to_money(amount_s)
        except InvalidOperation:
            self.error(lineno, f"ASSESS {aid}: 定损额 '{amount_s}' 不是合法数字")
            return
        if amount < 0:
            self.error(lineno, f"ASSESS {aid}: 定损额不能为负")
            return
        acc = self.accidents.get(aid)
        if acc is None:
            self.error(lineno, f"ASSESS {aid}: 事故无任何已归并报案，定损拦截")
            return
        if acc.closed:
            self.error(lineno, f"ASSESS {aid}: 事故已结案，定损拦截")
            return
        if acc.assessed is not None:
            self.error(lineno, f"ASSESS {aid}: 事故已定损（{fmt(acc.assessed)}），重复定损拦截")
            return
        acc.assessed = amount
        self.check_ratios(lineno, acc)
        total_loss = acc.total_loss
        if amount > total_loss:
            acc.over_loss = True
            self.warn(
                lineno,
                f"ASSESS {aid}: 定损额 {fmt(amount)} 超过事故总报案损失 {fmt(total_loss)}"
                f"（超出 {fmt(amount - total_loss)}）",
            )

    def do_payout(self, lineno, args):
        if len(args) != 3:
            self.error(lineno, "PAYOUT 格式错误，应为: PAYOUT <事故号> <保单号> <赔付额>")
            return
        aid, pid, amount_s = args
        try:
            amount = to_money(amount_s)
        except InvalidOperation:
            self.error(lineno, f"PAYOUT {aid}/{pid}: 赔付额 '{amount_s}' 不是合法数字")
            return
        if amount <= 0:
            self.error(lineno, f"PAYOUT {aid}/{pid}: 赔付额必须为正数")
            return
        if pid not in self.policies:
            self.error(lineno, f"PAYOUT {aid}/{pid}: 保单 {pid} 未定义，赔付拦截")
            return
        acc = self.accidents.get(aid)
        if acc is None or pid not in acc.reports:
            self.error(
                lineno,
                f"PAYOUT {aid}/{pid}: 未找到该保单在事故 {aid} 下已归并的报案，赔付拦截",
            )
            return
        policy = self.policies[pid]
        if policy.paid + amount > policy.limit:
            self.error(
                lineno,
                f"PAYOUT {aid}/{pid}: 赔付 {fmt(amount)} 将使保单 {pid} 累计赔付 "
                f"{fmt(policy.paid + amount)} 超过保额 {fmt(policy.limit)}"
                f"（已赔 {fmt(policy.paid)}，剩余 {fmt(policy.limit - policy.paid)}），赔付拦截",
            )
            return
        policy.paid += amount

    def do_close(self, lineno, args):
        if len(args) != 1:
            self.error(lineno, "CLOSE 格式错误，应为: CLOSE <事故号>")
            return
        aid = args[0]
        acc = self.accidents.get(aid)
        if acc is None:
            self.error(lineno, f"CLOSE {aid}: 事故不存在，结案拦截")
            return
        if acc.closed:
            self.error(lineno, f"CLOSE {aid}: 事故已结案，重复结案拦截")
            return
        acc.closed = True

    # ---- 分摊计算 ----
    def resolve_ratios(self, acc):
        """返回 (ratios: {pid: Decimal}, note: str|None)。"""
        pids = list(acc.reports)
        with_ratio = [p for p in pids if acc.reports[p].ratio is not None]
        note = None
        if with_ratio and len(with_ratio) != len(pids):
            note = "部分报案缺失责任比例，回退为按损失占比分摊"
            ratios = self._loss_based_ratios(acc)
        elif with_ratio:
            total = sum(acc.reports[p].ratio for p in pids)
            if abs(total - ONE) > RATIO_TOL:
                note = f"责任比例之和为 {total}（不为 1），回退为按损失占比分摊"
                ratios = self._loss_based_ratios(acc)
            else:
                ratios = {p: acc.reports[p].ratio for p in pids}
        else:
            ratios = self._loss_based_ratios(acc)
        return ratios, note

    @staticmethod
    def _loss_based_ratios(acc):
        pids = list(acc.reports)
        total = acc.total_loss
        if total <= 0:
            share = ONE / Decimal(len(pids))
            return {p: share for p in pids}
        return {p: acc.reports[p].loss / total for p in pids}

    @staticmethod
    def split_amount(total, ratios):
        """按比分摊，末位保单兜底尾差，保证分摊之和 == total。"""
        pids = list(ratios)
        shares = {}
        allocated = Decimal("0.00")
        for pid in pids[:-1]:
            share = (total * ratios[pid]).quantize(CENT, rounding=ROUND_HALF_UP)
            shares[pid] = share
            allocated += share
        shares[pids[-1]] = total - allocated
        return shares

    # ---- 主流程 ----
    def run(self, lines):
        handlers = {
            "POLICY": self.do_policy,
            "REPORT": self.do_report,
            "ASSESS": self.do_assess,
            "PAYOUT": self.do_payout,
            "CLOSE": self.do_close,
        }
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            op = parts[0].upper()
            handler = handlers.get(op)
            if handler is None:
                self.error(lineno, f"未知指令 '{parts[0]}'")
                continue
            handler(lineno, parts[1:])

    # ---- 输出 ----
    def render(self, out):
        w = out.write
        w("=" * 72 + "\n")
        w("事故分摊表\n")
        w("=" * 72 + "\n")
        if not self.accidents:
            w("（无事故）\n")
        for aid, acc in self.accidents.items():
            status = "已结案" if acc.closed else "处理中"
            w(f"\n事故 {aid}  [{status}]  报案保单数: {len(acc.reports)}"
              f"  总报案损失: {fmt(acc.total_loss)}")
            if acc.assessed is None:
                w("  定损额: 未定损\n")
            else:
                flag = "  ⚠ 超总损失" if acc.over_loss else ""
                w(f"  定损额: {fmt(acc.assessed)}{flag}\n")
            header = f"  {'保单号':<10}{'报案损失':>14}{'责任比例':>12}{'分摊定损':>16}"
            w(header + "\n")
            w("  " + "-" * 52 + "\n")
            if acc.assessed is not None and acc.reports:
                ratios, note = self.resolve_ratios(acc)
                if note:
                    w(f"  [注意] {note}\n")
                shares = self.split_amount(acc.assessed, ratios)
                for pid, rep in acc.reports.items():
                    ratio_pct = f"{ratios[pid] * 100:.2f}%"
                    w(f"  {pid:<10}{fmt(rep.loss):>14}{ratio_pct:>12}{fmt(shares[pid]):>16}\n")
                check = sum(shares.values())
                w("  " + "-" * 52 + "\n")
                w(f"  {'合计':<10}{fmt(acc.total_loss):>14}{'100.00%':>12}{fmt(check):>16}\n")
            else:
                for pid, rep in acc.reports.items():
                    w(f"  {pid:<10}{fmt(rep.loss):>14}{'-':>12}{'-':>16}\n")

        w("\n" + "=" * 72 + "\n")
        w("保单状态\n")
        w("=" * 72 + "\n")
        if not self.policies:
            w("（无保单）\n")
        else:
            w(f"  {'保单号':<10}{'险种':<8}{'保额':>14}{'累计已赔':>14}{'剩余额度':>14}\n")
            w("  " + "-" * 60 + "\n")
            for p in self.policies.values():
                w(f"  {p.pid:<10}{p.kind:<8}{fmt(p.limit):>14}{fmt(p.paid):>14}"
                  f"{fmt(p.limit - p.paid):>14}\n")

        w("\n" + "=" * 72 + "\n")
        w(f"归并记录（{len(self.merges)} 条）\n")
        w("=" * 72 + "\n")
        if not self.merges:
            w("（无）\n")
        for lineno, msg in self.merges:
            w(f"  [行 {lineno}] {msg}\n")

        w("\n" + "=" * 72 + "\n")
        w(f"错误清单（{len(self.errors)} 条）\n")
        w("=" * 72 + "\n")
        if not self.errors:
            w("（无）\n")
        for lineno, level, msg in self.errors:
            w(f"  [行 {lineno}] [{level}] {msg}\n")


def main(argv):
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in ("-h", "--help")):
        sys.stderr.write(__doc__)
        return 0 if len(argv) == 2 else 2
    if len(argv) == 2:
        with open(argv[1], "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    else:
        lines = sys.stdin.readlines()
    engine = Engine()
    engine.run(lines)
    engine.render(sys.stdout)
    return 1 if any(level == "拦截" for _, level, _ in engine.errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
