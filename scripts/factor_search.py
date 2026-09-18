"""P5 · 因子搜索 —— **带多重检验纪律的系统化搜索**。

## 为什么不能"挑最好的那个"

设候选因子数 ``k``。若从中挑 IC_IR 最大的：
* 纯噪声下，k 个独立检验里至少一个 p<0.05 的概率 = ``1 − 0.95^k``
* k=47 时 = **90%** —— 不管数据里有没有信号，"挑最好"一定能挑出一个"显著"的

所以本脚本**不做挑选**，只做**过滤**，且校正基数 = **实际尝试的候选数**：

    ① 全空间 Bonferroni：``z = Φ⁻¹(1 − 0.025/k)``，k = 候选数
    ② Walk-Forward 稳定性：每个滚动窗口都必须过门槛，且符号一致
    ③ 经济意义：``|IC_IR| ≥ 0.3``

**三道闸之后若无因子存活，结论就是"无"** —— 那是可信的结论，不是失败。

用法::

    .\\py.cmd scripts\\factor_search.py
    .\\py.cmd scripts\\factor_search.py --horizon 10 --window-days 730
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from do_modle.config import load_config  # noqa: E402
from do_modle.data.adapters.local_cache import CachedProvider  # noqa: E402
from do_modle.data.pit import PITGuard  # noqa: E402
from do_modle.evaluation.pitfalls import bonferroni_z  # noqa: E402
from do_modle.evaluation.xsec import (  # noqa: E402
    build_cross_sections,
    compute_ic_series,
    evaluate_stability,
    rolling_windows,
)
from do_modle.objects import AdjustFlag  # noqa: E402
from do_modle.strategy.factors import FACTORS, factor_names  # noqa: E402
from do_modle.strategy.panel import (
    attach_forecasts,
    attach_shipping,
    build_factor_panel,
    build_return_panel,
)  # noqa: E402
from scripts.sync_data import load_universe  # noqa: E402

IC_IR_FLOOR = 0.3  # 全期经济意义门槛
MIN_WINDOW_IC_IR = 0.10  # 每个滚动窗口的最低 |IC_IR|（排除"某些窗口完全失效"）
MIN_SIGN_CONSISTENCY = 0.8

def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="shipping")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--store", default=cfg.data.store)
    parser.add_argument("--min-symbols", type=int, default=5)
    parser.add_argument("--window-days", type=int, default=730)
    parser.add_argument("--step-days", type=int, default=365)
    args = parser.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    symbols = load_universe(args.universe)

    provider = CachedProvider(Path(args.store) / "market.sqlite")
    try:
        available = [s for s in symbols if provider.cache.read("instrument", symbol=s)]
        if len(available) < args.min_symbols:
            print(f"[FAIL] 可用标的 {len(available)} < {args.min_symbols}")
            return 1
        bars_hfq = {s: provider.get_bars(s, "1d", start, end, adjust=AdjustFlag.HFQ)
                    for s in available}
        bars_none = {s: provider.get_bars(s, "1d", start, end, adjust=AdjustFlag.NONE)
                     for s in available}
        forecasts = {s: provider.get_forecasts(s, start, end) for s in available}
        indices = provider.get_shipping_indices()
        bars_hfq = {s: b for s, b in bars_hfq.items() if b}
        bars_none = {s: b for s, b in bars_none.items() if b}
    finally:
        provider.close()

    names = factor_names()
    k = len(names)
    z = bonferroni_z(k)
    windows = rolling_windows(start, end, window_days=args.window_days,
                              step_days=args.step_days)

    print("=" * 100)
    print("因子搜索（带多重检验纪律）")
    print("=" * 100)
    print(f"研究池 {args.universe}（{len(available)} 只）  区间 {start} ~ {end}  "
          f"前向 {args.horizon} 日")
    print(f"候选因子 k = {k}    Bonferroni 阈值 z = {z:.3f}")
    print(f"Walk-Forward 窗口 {len(windows)} 个（{args.window_days} 天窗口 / "
          f"{args.step_days} 天步长）")
    print(f"判定门槛：① |t| ≥ {z:.3f}  ② 每窗口都过 ① 且符号一致率 ≥ "
          f"{MIN_SIGN_CONSISTENCY:.0%}  ③ |IC_IR| ≥ {IC_IR_FLOOR}")
    print()
    print(f"[风险提示] 若从中『挑 IC_IR 最大』，k={k} 时纯噪声下至少一个"
          f"「显著」的概率 = 1 − 0.95^{k} = {1 - 0.95 ** k:.0%}")
    print("           故本脚本不做挑选，只做过滤。")
    print()

    print("构建面板…")
    # 把「截至每根 Bar 当日已公告」的最近一次业绩预告附加到 Bar 上（PIT 安全）
    bars_hfq = attach_forecasts(bars_hfq, forecasts, pit=PITGuard())
    bars_hfq = attach_shipping(bars_hfq, indices, pit=PITGuard())
    factor_panels = build_factor_panel(bars_hfq, names, pit=PITGuard())
    return_panel = build_return_panel(bars_none, args.horizon)
    print("完成。")
    print()

    # ---- 逐因子评估 ----
    print("=" * 100)
    print(f"{'因子':<20}{'类别':<14}{'平均N':>6}{'mean_IC':>10}{'IC_IR':>9}"
          f"{'朴素t':>8}{'修正t':>8}{'膨胀':>6}{'符号一致':>9}{'最差窗':>9}  判定")
    print("-" * 100)

    survivors: list[str] = []
    rows: list[dict] = []

    for name in names:
        sections = build_cross_sections(
            factor_panels[name], return_panel, min_symbols=args.min_symbols
        )
        if not sections:
            continue
        s = compute_ic_series(sections).summary()
        stab = evaluate_stability(sections, windows)

        gate1 = abs(s["t_stat"]) >= z
        gate2 = (
            stab.n_windows >= 3
            and stab.sign_consistency == stab.sign_consistency
            and stab.sign_consistency >= MIN_SIGN_CONSISTENCY
            and stab.min_abs_ic_ir == stab.min_abs_ic_ir
            and abs(stab.min_abs_ic_ir) >= MIN_WINDOW_IC_IR
        )
        gate3 = abs(s["ic_ir"]) >= IC_IR_FLOOR if s["ic_ir"] == s["ic_ir"] else False

        passed = gate1 and gate2 and gate3
        if passed:
            survivors.append(name)
        if not gate1:
            mark = "✗①t值"
        elif not gate2:
            mark = "✗②不稳定"
        elif not gate3:
            mark = "✗③IC_IR"
        else:
            mark = "✅ 存活"

        print(
            f"{name:<20}{FACTORS[name].category:<14}{s['mean_n']:>6.1f}"
            f"{s['mean_ic']:>+10.4f}{s['ic_ir']:>+9.4f}"
            f"{s['t_stat_naive']:>+8.2f}{s['t_stat']:>+8.2f}"
            f"{s['t_stat_naive'] / s['t_stat'] if s['t_stat'] else float('nan'):>6.2f}"
            f"{stab.sign_consistency:>9.0%}{stab.min_abs_ic_ir:>9.4f}  {mark}"
        )
        rows.append(
            {
                "name": name,
                "category": FACTORS[name].category,
                "summary": s,
                "stability": stab,
                "gates": (gate1, gate2, gate3),
            }
        )

    print("-" * 100)

    # ---- 汇总 ----
    n1 = sum(1 for r in rows if r["gates"][0])
    n2 = sum(1 for r in rows if r["gates"][1])
    n3 = sum(1 for r in rows if r["gates"][2])
    print()
    print(f"① **修正 t** 过 Bonferroni(z={z:.3f})                 : {n1}/{len(rows)}")
    print(f"② Walk-Forward 稳定（每窗 |IC_IR|≥{MIN_WINDOW_IC_IR} 且符号一致≥"
          f"{MIN_SIGN_CONSISTENCY:.0%}）: {n2}/{len(rows)}")
    print(f"③ 全期 |IC_IR| ≥ {IC_IR_FLOOR}                                 : {n3}/{len(rows)}")
    print()

    # ---- 诚实排序：按「最差窗口」而非「全期最好」 ----
    print("=" * 100)
    print("按【最差窗口 |IC_IR|】排序（maximin，比『挑全期最大』诚实）")
    print("=" * 100)
    print(f"{'因子':<20}{'全期 IC_IR':>12}{'最差窗 |IC_IR|':>16}"
          f"{'最差窗口':>26}{'符号一致':>10}")
    print("-" * 100)
    ranked = sorted(
        rows,
        key=lambda r: (
            -(abs(r["stability"].min_abs_ic_ir)
              if r["stability"].min_abs_ic_ir == r["stability"].min_abs_ic_ir
              else -1)
        ),
    )
    for r in ranked[:12]:
        s, st = r["summary"], r["stability"]
        print(
            f"{r['name']:<20}{s['ic_ir']:>+12.4f}{st.min_abs_ic_ir:>16.4f}"
            f"{st.worst_window:>26}{st.sign_consistency:>10.0%}"
        )
    print("-" * 100)
    print("↑ 全期 IC_IR 大不代表可用：多数因子在最差窗口塌到接近 0")
    print()

    # ---- 分类别最好 ----
    print("各类别内 |IC_IR| 最大的因子（**仅供参考，不代表可用**）：")
    by_cat: dict[str, dict] = {}
    for r in rows:
        cur = by_cat.get(r["category"])
        v = abs(r["summary"]["ic_ir"]) if r["summary"]["ic_ir"] == r["summary"]["ic_ir"] else -1
        if cur is None or v > cur["v"]:
            by_cat[r["category"]] = {"v": v, "row": r}
    for cat, item in sorted(by_cat.items()):
        r = item["row"]
        s = r["summary"]
        print(
            f"  {cat:<16}{r['name']:<20} IC_IR={s['ic_ir']:+.4f}  "
            f"t={s['t_stat']:+.2f}  符号一致={r['stability'].sign_consistency:.0%}"
        )

    print()
    print("=" * 100)
    if survivors:
        print(f"[PASS] 三道闸后存活 {len(survivors)} 个：{survivors}")
    else:
        print("三道闸后**无因子存活**。")
        print()
        print("这是可信的否定结论，不是失败：")
        print(f"  · 候选数 k={k}，Bonferroni 校正后阈值 z={z:.3f}（单因子只需 1.96）")
        print(f"  · 要求**每个** Walk-Forward 窗口的 |IC_IR| 都 ≥ {MIN_WINDOW_IC_IR}，"
              f"且符号一致 ≥ {MIN_SIGN_CONSISTENCY:.0%}")
        print(f"  · 要求全期 |IC_IR| ≥ {IC_IR_FLOOR}")
        print()
        print("关键读数（maximin 排序）：")
        if ranked:
            top = ranked[0]
            print(f"  · 表现最『稳』的是 {top['name']}：全期 IC_IR={top['summary']['ic_ir']:+.4f}，"
                  f"但**最差窗口仅 {top['stability'].min_abs_ic_ir:.4f}**")
            print("  · 即『全期看起来有效』的因子，在某个 2 年窗口里几乎完全失效")
            best_full = max(
                rows,
                key=lambda r: abs(r["summary"]["ic_ir"])
                if r["summary"]["ic_ir"] == r["summary"]["ic_ir"]
                else -1,
            )
            print(f"  · 全期 |IC_IR| 最高的 {best_full['name']} "
                  f"({best_full['summary']['ic_ir']:+.4f}) "
                  f"在最差窗口同样塌到 {best_full['stability'].min_abs_ic_ir:.4f}")
        print()
        print("下一步方向：找与价格/量能**正交**的新信息源（运价、估值、事件），")
        print("而不是继续在价格/量能上做参数搜索——后者已被证明方向对但强度不足。")
    print("=" * 100)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
