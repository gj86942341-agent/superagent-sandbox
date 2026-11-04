#!/usr/bin/env python3
"""诚实的"优化"评估：walk-forward 回测 + 显著性检验 + 样本外验证。

它回答一个问题，而且只用数据回答：
    「把参数（窗口、热度/回补权重）调一调，真的能比随机更好吗？」

方法
----
1. **walk-forward**：对历史上每一期 t，只用 t 之前的数据算分、选出 6+2 复式，
   再用 t 期的真实开奖对奖。全程不使用未来信息。
2. **显著性检验**：把每注的前区命中数均值与理论值 5×5/35 = 0.7143 比，
   算 z 值；|z| < 2 说明差异在随机波动内。
3. **样本外验证**：拿前 70% 的数据挑"表现最好"的参数，再看它在后 30% 还能不能行。
   如果样本外塌了，说明"挑最好参数"就是过拟合。

用法
----
    python optimize.py            跑全部参数组合并输出评估表
    python optimize.py --quick    只跑 3 个参数组合（约 1/3 时间）

诚实预期：所有参数组合都会落在随机波动内。本脚本的价值不是"找到更好的参数"，
而是**每跑一次都能证明"没有找到"**——这比拍脑袋调参可靠。
"""
from __future__ import annotations

import itertools
import json
import math
import sys
import lottery as lt  # noqa: E402

FRONT_N, BACK_N = lt.TICKET_FRONT_N, lt.TICKET_BACK_N
ZONES = ((1, 12), (13, 24), (25, 35))
# 现行 7 奖级从 26014 期（2026-02-02）开始；更早的期数奖级表不同，ROI 只在现行期数上算。
CURRENT_RULES_FROM = "26014"
ADD = True


def window_stats(rows):
    """rows: 按时间倒序（最新在前）。返回频次、间隔、上期号码。"""
    front_freq = {n: 0 for n in range(lt.FRONT_MIN, lt.FRONT_MAX + 1)}
    back_freq = {n: 0 for n in range(lt.BACK_MIN, lt.BACK_MAX + 1)}
    front_gap = {n: len(rows) for n in front_freq}
    back_gap = {n: len(rows) for n in back_freq}
    for index, (front, back) in enumerate(rows):
        for number in front:
            front_freq[number] += 1
            if front_gap[number] == len(rows):
                front_gap[number] = index
        for number in back:
            back_freq[number] += 1
            if back_gap[number] == len(rows):
                back_gap[number] = index
    return front_freq, back_freq, front_gap, back_gap, (set(rows[0][0]) if rows else set())


def pick(front_freq, back_freq, front_gap, back_gap, last_front, weights):
    def score(freq, gap, last):
        top = max(freq.values()) or 1
        longest = max(gap.values()) or 1
        result = {}
        for number in freq:
            value = weights["heat"] * freq[number] / top + weights["gap"] * gap[number] / longest
            result[number] = round(value * (0.6 if number in last else 1.0), 6)
        return result

    front_scores = score(front_freq, front_gap, last_front)
    back_scores = score(back_freq, back_gap, set())
    base, extra = divmod(FRONT_N, len(ZONES))
    quota = [base + (1 if i < extra else 0) for i in range(len(ZONES))]
    chosen: list[int] = []
    for (low, high), need in zip(ZONES, quota):
        ranked = sorted((n for n in range(low, high + 1)), key=lambda n: (-front_scores[n], n))
        taken = 0
        for number in ranked:
            if taken >= need:
                break
            if any(abs(number - other) == 1 for other in chosen):
                continue
            chosen.append(number)
            taken += 1
        if taken < need:
            for number in ranked:
                if taken >= need:
                    break
                if number in chosen:
                    continue
                chosen.append(number)
                taken += 1
    ranked = sorted(back_scores, key=lambda n: (-back_scores[n], n))
    back = sorted({ranked[0], ranked[-1]})[:BACK_N]
    return sorted(chosen), back


def prize_map(conn) -> dict[str, dict[str, float]]:
    mapping: dict[str, dict[str, float]] = {}
    for row in conn.execute("SELECT draw_no, level, amount FROM prizes"):
        if row["amount"] is not None:
            mapping.setdefault(row["draw_no"], {})[row["level"]] = float(row["amount"])
    return mapping


def settle(conn_prizes, draw_no, front_pool, back_pool, draw_front, draw_back, add):
    """复式逐注判奖，返回 (前区命中总数, 注数, 总奖金)。

    注意：必须统计**每一注**的命中再取平均。只取"6 注里的最大命中"会系统性虚高——
    那是 max 而不是单注期望，跟理论值 0.7143 不可比。
    """
    total = 0.0
    front_sum = 0
    bets = 0
    levels = conn_prizes.get(draw_no, {})
    for front in itertools.combinations(front_pool, lt.FRONT_COUNT):
        for back in itertools.combinations(back_pool, lt.BACK_COUNT):
            front_hit, back_hit = lt.hit(list(front), list(back), draw_front, draw_back)
            front_sum += front_hit
            bets += 1
            level = lt.judge(front_hit, back_hit)
            if not level:
                continue
            total += levels.get(level, 0.0)
            if add and level in lt.FLOATING:
                total += levels.get(f"{level}(追加)", levels.get(level, 0.0) * 0.8)
    return front_sum, bets, total


def evaluate(conn, draws, prizes, window, weights, start=200, end=None):
    end = len(draws) if end is None else end
    front_sum = 0
    back_sum = 0
    bets_total = 0
    spend = 0.0
    won = 0.0
    periods = 0
    period_means: list[float] = []
    for index in range(start, end):
        rows = [([int(x) for x in draws[j]["front"].split()], [int(x) for x in draws[j]["back"].split()])
                for j in range(index - 1, max(-1, index - 1 - window), -1)]
        if len(rows) < window:
            continue
        front_freq, back_freq, front_gap, back_gap, last_front = window_stats(rows)
        front, back = pick(front_freq, back_freq, front_gap, back_gap, last_front, weights)
        draw = draws[index]
        draw_front = [int(x) for x in draw["front"].split()]
        draw_back = [int(x) for x in draw["back"].split()]
        front_hits, bets, amount = settle(prizes, draw["draw_no"], front, back, draw_front, draw_back, ADD)
        front_sum += front_hits
        bets_total += bets
        period_means.append(front_hits / bets)
        # 后区：该复式后区池只有 2 个号，每一注的后区命中都相同，按注计一次即可
        back_sum += len(set(back) & set(draw_back)) * bets
        periods += 1
        if draw["draw_no"] >= CURRENT_RULES_FROM:
            spend += lt.compound_plan(FRONT_N, BACK_N, ADD)["cost"]
            won += amount
    return {"periods": periods, "bets": bets_total,
            "avg_front_hit": front_sum / bets_total if bets_total else 0,
            "avg_back_hit": back_sum / bets_total if bets_total else 0,
            "per_period": period_means,
            "spend": spend, "won": won, "roi": (won / spend) if spend else 0}


def z_cluster(per_period, expected=5 * 5 / 35):
    """以"期"为聚类单位计算标准误。

    同一期的 6 注共享同一个号码池，彼此高度相关，把它们当成独立样本会把标准误算小、
    把 z 值算大（第一版就踩了这个坑）。正确做法是先算每期的均值，再对期做 t 检验。
    """
    n = len(per_period)
    if n < 2:
        return 0.0, 0.0
    mean = sum(per_period) / n
    variance = sum((value - mean) ** 2 for value in per_period) / (n - 1)
    se = math.sqrt(variance / n)
    return mean, ((mean - expected) / se if se else 0.0)


GRID = [
    {"window": window, "weights": {"heat": heat, "gap": round(1 - heat, 2)}}
    for window in (20, 30, 50)
    for heat in (0.3, 0.55, 0.7)
]


def main(argv) -> int:
    conn = lt.connect()
    draws = list(conn.execute("SELECT draw_no, front, back FROM draws ORDER BY draw_no").fetchall())
    prizes = prize_map(conn)
    conn.close()
    if len(draws) < 500:
        print("数据太少，先运行 fetch")
        return 1

    grid = GRID[:3] if "--quick" in argv else GRID
    results = []
    print(f"回测区间：第 {draws[200]['draw_no']} 期 – 第 {draws[-1]['draw_no']} 期"
          f"（共 {len(draws) - 200} 种参数组合的 walk-forward）")
    print(f"{'窗口':>4} {'热度':>5} {'回补':>5} {'期数':>5} {'前区均中':>9} {'z':>7}"
          f" {'后区均中':>9} {'ROI':>7}")
    for params in grid:
        result = evaluate(conn=None, draws=draws, prizes=prizes,
                          window=params["window"], weights=params["weights"])
        _, z = z_cluster(result["per_period"])
        result.update(params | {"z": z})
        results.append(result)
        print(f"{params['window']:>4} {params['weights']['heat']:>5} {params['weights']['gap']:>5}"
              f" {result['periods']:>5} {result['avg_front_hit']:>9.4f} {z:>7.2f}"
              f" {result['avg_back_hit']:>9.4f} {result['roi']:>7.1%}")

    best = max(results, key=lambda r: r["avg_front_hit"])
    # 多重比较：9 个组合各做一次检验，|z|>2 出现一两次是正常的假阳性。
    # 按 Bonferroni（5% / 9 ≈ 0.56%）校正后，判定阈值约 |z| > 2.87。
    threshold = 2.87 if len(grid) > 1 else 2.0
    print(f"\n样本内最好的组合：窗口 {best['window']}、热度 {best['weights']['heat']}"
          f" → 前区均中 {best['avg_front_hit']:.4f}（理论随机 0.7143，z={best['z']:.2f}）")
    print(f"多重比较校正后的阈值：|z| > {threshold:.2f}（9 次检验，Bonferroni 5%）")
    significant = [r for r in results if abs(r["z"]) >= threshold]
    if significant:
        print("⚠️ 校正后仍有组合偏离随机；先怀疑样本/口径问题，不要直接当成'策略有效'。")
    else:
        print("结论：没有任何参数组合显著优于随机。调参不会提高中奖概率——与数学推导一致。")

    # ---- 样本外验证：用前 70% 的数据挑"最好"，看它在后 30% 还能不能行 ----
    split = 200 + int((len(draws) - 200) * 0.7)
    print(f"\n样本外验证：前 70%（到第 {draws[split - 1]['draw_no']} 期）挑参数，"
          f"后 30%（第 {draws[split]['draw_no']} 期起）检验")
    in_sample = []
    for params in grid:
        result = evaluate(None, draws, prizes, params["window"], params["weights"], start=200, end=split)
        result.update(params)
        in_sample.append(result)
    champion = max(in_sample, key=lambda r: r["avg_front_hit"])
    oos = evaluate(None, draws, prizes, champion["window"], champion["weights"], start=split, end=len(draws))
    oos_mean, oos_z = z_cluster(oos["per_period"])
    print(f"  样本内冠军：窗口 {champion['window']}、热度 {champion['weights']['heat']}"
          f"（样本内均中 {champion['avg_front_hit']:.4f}）")
    print(f"  样本外表现：均中 {oos['avg_front_hit']:.4f}，z={oos_z:.2f}，"
          f"期数 {oos['periods']}")
    if abs(oos_z) < 2:
        print("  → 样本外落在随机范围内：样本内的领先没有延续，属过拟合。")
    else:
        print("  → 样本外仍偏离随机；请核对数据与口径，不要急于下结论。")

    # 只存聚合结果：per_period 是每期的原始均值序列（几千个数），存进去会让 JSON 膨胀到几百 KB。
    slim = [{key: value for key, value in row.items() if key != "per_period"} for row in results]
    out = {"at": lt.now().strftime("%Y-%m-%d %H:%M:%S"), "results": slim,
           "best_in_sample": {"window": best["window"], **best["weights"]},
           "threshold": threshold, "significant": bool(significant),
           "out_of_sample": {"window": champion["window"], **champion["weights"],
                             "avg_front_hit": oos["avg_front_hit"], "z": oos_z,
                             "periods": oos["periods"]}}
    with open(r"D:\deepseek_space\lottery\optimize-report.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("结果已写入 lottery/optimize-report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
