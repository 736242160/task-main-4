# -*- coding: utf-8 -*-
"""
转会费摊销与注册联动工具（纯 Python 标准库，单文件）

自定规则：
1. 摊销：直线法，年摊销额 = 转会费 / 合同年限；年度摊销入账后剩余额 = 转会费 - 累计已摊销。
2. 冻结：球员存在未摊销完余额且未解约时，再次签约（转会）被拦截（转会冻结）。
3. 解约：释放注册名额；未摊销余额一次性级联调整为当期"解约减值"入账，剩余额归零。
4. 注册：占用球员当前所属队 1 个名额；名额满时报缺几个；已注册到他队再注册他队被拦截。
5. 校验：摊销累计不得超过转会费总额；引用不存在的球员/球队要报告。
6. 跨流状态延续：转会流与摊销流合并为统一事件序列，按顺序推进同一状态机。
"""

from decimal import Decimal, ROUND_HALF_UP


def D(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Engine:
    def __init__(self, teams):
        # teams: {队名: 注册名额}
        self.slots = dict(teams)
        self.players = {}          # 球员名 -> 状态 dict
        self.rosters = {t: [] for t in teams}  # 队名 -> 已注册球员列表
        self.ledger = []           # 摊销/调整入账流水 (年度, 球员, 金额, 类型)
        self.errors = []           # 拦截/报告信息

    # ---------- 内部 ----------
    def _err(self, msg):
        self.errors.append(msg)
        print("  [拦截/报告] " + msg)

    def _remaining(self, p):
        return D(p["fee"] - p["amortized"])

    # ---------- 转会流 ----------
    def sign(self, player, from_team, to_team, fee, years):
        """状态=签约：建立摊销计划；未摊销完的老合同触发转会冻结。"""
        if to_team not in self.slots:
            return self._err(f"签约失败：球队不存在 -> {to_team}")
        if from_team and from_team != "-" and from_team not in self.slots:
            return self._err(f"签约失败：原球队不存在 -> {from_team}")
        old = self.players.get(player)
        if old and not old["released"]:
            rem = self._remaining(old)
            if rem > 0:
                return self._err(
                    f"转会冻结：{player} 尚有未摊销余额 {rem}（{old['team']}），禁止再签约")
            return self._err(f"签约失败：{player} 与 {old['team']} 合同仍在，未解约")
        self.players[player] = {
            "team": to_team, "from": from_team or "-",
            "fee": D(fee), "years": int(years),
            "amortized": D(0), "registered": False, "released": False,
        }
        print(f"  签约：{player} {from_team or '-'} -> {to_team}，费 {D(fee)}，{years} 年，"
              f"年摊销 {D(D(fee) / int(years))}")

    def register(self, player, team=None):
        """状态=注册：占新队名额；重复/跨队注册拦截；名额满报缺口。"""
        p = self.players.get(player)
        if not p:
            return self._err(f"注册失败：球员不存在 -> {player}")
        if p["released"]:
            return self._err(f"注册失败：{player} 已解约")
        team = team or p["team"]
        if team not in self.slots:
            return self._err(f"注册失败：球队不存在 -> {team}")
        if team != p["team"]:
            return self._err(f"注册失败：{player} 属 {p['team']}，不能注册到 {team}")
        if p["registered"]:
            return self._err(f"重复注册拦截：{player} 已注册在 {p['team']}，不能同时注册两队")
        used = len(self.rosters[team])
        cap = self.slots[team]
        if used >= cap:
            return self._err(f"名额不足：{team} 名额 {used}/{cap}，注册 {player} 还缺 {used + 1 - cap} 个名额")
        p["registered"] = True
        self.rosters[team].append(player)
        print(f"  注册：{player} -> {team}（名额 {len(self.rosters[team])}/{cap}）")

    def release(self, player):
        """状态=解除：释放名额；未摊销余额级联一次性调整入账。"""
        p = self.players.get(player)
        if not p:
            return self._err(f"解约失败：球员不存在 -> {player}")
        if p["released"]:
            return self._err(f"解约失败：{player} 此前已解约")
        p["released"] = True
        if p["registered"]:
            p["registered"] = False
            self.rosters[p["team"]].remove(player)
            print(f"  解除注册：{player} 离开 {p['team']} 名单，名额释放")
        rem = self._remaining(p)
        if rem > 0:
            self.ledger.append(("解约当期", player, rem, "解约减值级联调整"))
            p["amortized"] = p["fee"]
            print(f"  解约级联调整：{player} 未摊销 {rem} 一次性计入当期损益，剩余摊销额归零")
        else:
            print(f"  解约：{player} 已摊销完毕，无需调整")

    # ---------- 摊销流 ----------
    def amortize_year(self, year):
        """年度摊销：对所有在约球员按直线法入账，重算剩余额；超额报告。"""
        print(f"  -- {year} 年度摊销 --")
        for name, p in self.players.items():
            if p["released"]:
                continue
            rem = self._remaining(p)
            if rem <= 0:
                continue
            annual = D(p["fee"] / p["years"])
            if p["amortized"] + annual > p["fee"]:
                self._err(f"摊销超额：{name} 本年应摊 {annual} 会使累计超过转会费 {p['fee']}，"
                          f"按剩余 {rem} 封顶入账")
                annual = rem
            p["amortized"] = D(p["amortized"] + annual)
            self.ledger.append((year, name, annual, "年度摊销"))
            print(f"    {name}: 入账 {annual}，累计 {p['amortized']}/{p['fee']}，"
                  f"剩余 {self._remaining(p)}")

    # ---------- 事件分发（跨流状态延续） ----------
    def apply(self, ev):
        kind = ev[0]
        print(f"> 事件: {ev}")
        if kind == "sign":
            self.sign(*ev[1:])
        elif kind == "register":
            self.register(*ev[1:])
        elif kind == "release":
            self.release(*ev[1:])
        elif kind == "amortize":
            self.amortize_year(ev[1])
        else:
            self._err(f"未知事件类型: {kind}")

    # ---------- 输出 ----------
    def report(self):
        print("\n===== 注册状态清单 =====")
        for team, cap in self.slots.items():
            names = self.rosters[team]
            print(f"  {team}: {len(names)}/{cap} -> {', '.join(names) if names else '(空)'}")
        print("\n===== 摊销状态清单 =====")
        print(f"  {'球员':<8}{'球队':<8}{'转会费':>10}{'年限':>4}{'已摊销':>10}{'剩余':>10}  状态")
        for name, p in self.players.items():
            st = "已解约" if p["released"] else ("已摊完" if self._remaining(p) == 0 else "摊销中")
            print(f"  {name:<8}{p['team']:<8}{p['fee']:>10}{p['years']:>6}"
                  f"{p['amortized']:>10}{self._remaining(p):>10}  {st}")
        print("\n===== 摊销/调整入账流水 =====")
        for year, name, amt, kind in self.ledger:
            print(f"  {year}  {name:<8}{kind:<12}{amt:>10}")
        print("\n===== 拦截/报告汇总 =====")
        print("\n".join("  - " + e for e in self.errors) if self.errors else "  无")


if __name__ == "__main__":
    # 球队定义：注册名额
    teams = {"皇马": 2, "巴萨": 1, "拜仁": 2}

    # 统一事件序列：转会流(签约/注册/解除) + 摊销流(年度摊销)，按序推进
    events = [
        ("sign", "甲", "-", "皇马", 1200, 4),     # 年摊销 300
        ("sign", "乙", "-", "皇马", 600, 3),      # 年摊销 200
        ("register", "甲"),
        ("register", "乙"),
        ("amortize", "2023"),                     # 甲 300、乙 200 入账，剩余重算
        ("sign", "甲", "皇马", "巴萨", 2000, 5),  # 甲未摊销完 -> 转会冻结拦截
        ("amortize", "2024"),
        ("release", "甲"),                        # 解约：释放名额 + 剩余 600 级联调整
        ("sign", "甲", "皇马", "巴萨", 2000, 5),  # 解约后可再签约
        ("register", "甲"),                       # 占巴萨名额
        ("sign", "丙", "-", "巴萨", 300, 3),
        ("register", "丙"),                       # 巴萨名额满 -> 报告缺几个
        ("register", "甲"),                       # 重复注册 -> 拦截同时注册两队
        ("sign", "丁", "-", "拜仁", 100, 1),
        ("amortize", "2025"),                     # 丁摊完；乙 2025 摊完后剩余 0
        ("amortize", "2026"),                     # 乙/丁已摊完不再入账（防超额）
        ("register", "戊"),                       # 引用不存在球员 -> 报告
        ("sign", "己", "-", "巴黎", 100, 2),      # 引用不存在球队 -> 报告
    ]

    eng = Engine(teams)
    for ev in events:
        eng.apply(ev)
    eng.report()
