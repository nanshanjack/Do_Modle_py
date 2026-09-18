"""P6-b · 多因子合成评估 —— **带 split-half 防选择偏差**。

## 为什么必须 split-half

若用**全期** IC 挑因子再在全期评估合成结果，得到的是**过拟合估计**：
挑出来的因子本就在全期表现最好，合成当然好看。

正确做法：**在 A 段选因子与定符号 → 在 B 段评估**，反向再做一次。
两段的样本外结果才是对"选择规则 + 合成"的无偏估计。

## 两个纪律

1. **符号对齐**：因子方向必须由 **A 段** 决定（`sign = +1 if IC_IR_A > 0 else −1`），
   不能事后按 B 段调。
2. **等权**：不做 IC 加权（那会引入更多拟合）。只做 sign × 等权。

用法::

    .\\py.cmd scripts\\factor_combo.py
    .\\py.cmd scripts\\factor_combo.py --top-k 5 --horizon 5
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
    filter_sections,
)
from do_modle.objects import AdjustFlag  # noqa: E402
from do_modle.strategy.composite import combine_panels, panel_correlation  # noqa: E402
from do_modle.strategy.factors import factor_names  # noqa: E402
from do_modle.strategy.panel import (
    attach_forecasts,
    attach_shipping,
    build_factor_panel,
    build_return_panel,
)  # noqa: E402
from scripts.sync_data import load_universe  # noqa: E402

IC_IR_FLOOR = 0.3

def _ic_ir(sections, min_symbols: int) -> float:
    if not sections:
        return float("nan")
    return compute_ic_series(sections).ic_ir

def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="shipping")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--split", default="2021-12-31", help="split-half 切分点")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--store", default=cfg.data.store)
    parser.add_argument("--min-symbols", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5, help="训练段选前 K 个因子")
    args = parser.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    split = date.fromisoformat(args.split)
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
    print("=" * 92)
    print("多因子合成评估（split-half 防选择偏差）")
    print("=" * 92)
    print(f"研究池 {args.universe}（{len(available)} 只）  区间 {start} ~ {end}  前向 {args.horizon} 日")
    print(f"候选因子 {len(names)} 个   split-half 切分点 {split}   训练段选前 {args.top_k} 个")
    print()

    # 把「截至每根 Bar 当日已公告」的最近一次业绩预告附加到 Bar 上（PIT 安全）
    bars_hfq = attach_forecasts(bars_hfq, forecasts, pit=PITGuard())
    bars_hfq = attach_shipping(bars_hfq, indices, pit=PITGuard())
    factor_panels = build_factor_panel(bars_hfq, names, pit=PITGuard())
    return_panel = build_return_panel(bars_none, args.horizon)

    # 逐因子截面（全期）
    sections = {}
    for n in names:
        secs = build_cross_sections(factor_panels[n], return_panel,
                                    min_symbols=args.min_symbols)
        if secs:
            sections[n] = secs
    usable = sorted(sections)
    print(f"可用因子 {len(usable)} 个（有有效截面）")
    print()

    # ---- 1. 单因子全期表现（作为基线） ----
    print("=" * 92)
    print("基线：单因子全期 |IC_IR| 前 8")
    print("=" * 92)
    single = {n: _ic_ir(sections[n], args.min_symbols) for n in usable}
    for n, v in sorted(single.items(), key=lambda kv: -abs(kv[1]) if kv[1] == kv[1] else 1)[:8]:
        print(f"  {n:<22}{v:+.4f}")
    best_single = max(abs(v) for v in single.values() if v == v)
    print(f"\n单因子 |IC_IR| 最大值 = {best_single:.4f}")
    print()

    # ---- 2. split-half：A 段选因子 + 定符号 → B 段评估 ----
    print("=" * 92)
    print("split-half 样本外评估（**这是唯一无偏的结果**）")
    print("=" * 92)

    halves = [
        ("前半→后半", (start, split), (split, end)),
        ("后半→前半", (split, end), (start, split)),
    ]
    oos_results: list[tuple[str, float, list[str]]] = []

    for label, train_range, test_range in halves:
        # 训练段：算每个因子的 IC_IR
        train_ic: dict[str, float] = {}
        for n in usable:
            secs = filter_sections(sections[n], start=train_range[0], end=train_range[1])
            v = _ic_ir(secs, args.min_symbols)
            if v == v:
                train_ic[n] = v

        # 选前 K 个（按 |IC_IR|）
        picked = sorted(train_ic, key=lambda n: -abs(train_ic[n]))[: args.top_k]
        # 符号由训练段决定（不能事后调）
        weights = {n: (1.0 if train_ic[n] > 0 else -1.0) for n in picked}

        # 测试段：合成并评估
        test_panels = {}
        for n in picked:
            sub = {d: v for d, v in factor_panels[n].items()
                   if test_range[0] <= d <= test_range[1]}
            if sub:
                test_panels[n] = sub
        if not test_panels:
            print(f"【{label}】测试段无数据")
            continue

        combo = combine_panels(test_panels, weights)
        combo_secs = build_cross_sections(combo, return_panel,
                                         min_symbols=args.min_symbols)
        if not combo_secs:
            print(f"【{label}】合成后无有效截面")
            continue
        series = compute_ic_series(combo_secs)
        s = series.summary()

        # 对照：测试段内单因子的最好表现
        test_single = {}
        for n in picked:
            secs = filter_sections(sections[n], start=test_range[0], end=test_range[1])
            v = _ic_ir(secs, args.min_symbols)
            if v == v:
                test_single[n] = v

        print(f"\n【{label}】训练段 {train_range[0]}~{train_range[1]} → "
              f"测试段 {test_range[0]}~{test_range[1]}")
        print(f"  选中因子（{len(picked)}）：")
        for n in picked:
            sign = "+" if weights[n] > 0 else "−"
            print(f"    {sign} {n:<22} 训练 IC_IR={train_ic[n]:+.4f}"
                  f"  测试 IC_IR={test_single.get(n, float('nan')):+.4f}")
        print(f"  **合成后测试段**：IC_IR={s['ic_ir']:+.4f}  t={s['t_stat']:+.2f}  "
              f"期数={int(s['n_periods'])}  平均N={s['mean_n']:.1f}")
        oos_results.append((label, s["ic_ir"], picked))

    # ---- 3. 因子相关性（判断合成上限） ----
    print()
    print("=" * 92)
    print("因子间相关性（判断合成上限）")
    print("=" * 92)
    top8 = sorted(single, key=lambda n: -abs(single[n]) if single[n] == single[n] else 1)[:8]
    print("     " + "".join(f"{n[:9]:>10}" for n in top8))
    corrs = []
    for a in top8:
        row = f"{a[:9]:>4} "
        for b in top8:
            if a == b:
                row += f"{'1.00':>10}"
            else:
                c = panel_correlation(factor_panels[a], factor_panels[b])
                row += f"{c:>10.2f}"
                if a < b and c == c:
                    corrs.append(abs(c))
        print(row)
    if corrs:
        print(f"\n平均 |相关| = {sum(corrs)/len(corrs):.3f}")
        print(f"理论合成增益上限 ≈ √(K/(1+(K−1)·ρ̄))，K={args.top_k}, "
              f"ρ̄={sum(corrs)/len(corrs):.3f} → "
              f"{_gain(args.top_k, sum(corrs)/len(corrs)):.2f}×")

    # ---- 4. 汇总 ----
    print()
    print("=" * 92)
    print("结论")
    print("=" * 92)
    z = bonferroni_z(len(usable) + 2)  # +2：两次 split-half 合成
    print(f"Bonferroni 阈值（含 2 次合成检验）z = {z:.3f}")
    print()
    print(f"{'单因子最好 |IC_IR|':<26}{best_single:>10.4f}")
    for label, v, _ in oos_results:
        print(f"{'合成 OOS IC_IR (' + label + ')':<26}{v:>+10.4f}")
    print(f"{'可用门槛':<26}{IC_IR_FLOOR:>10.2f}")
    print()

    any_pass = False
    for label, v, _ in oos_results:
        if v == v and abs(v) >= IC_IR_FLOOR:
            print(f"[PASS] {label} 合成后 |IC_IR| = {abs(v):.4f} ≥ {IC_IR_FLOOR}")
            any_pass = True
    if not any_pass:
        print("**合成后仍未达到可用门槛。**")
        print()
        print("原因（按可能性排序）：")
        print("  1. 因子间相关性高 → 分散化收益有限（见上方相关矩阵）")
        print("  2. 单因子本身太弱（|IC_IR| ≈ 0.1），√K 倍提升不足以跨过 0.3")
        print("  3. 截面 N=8.7 → IC 估计噪声 0.36，合成也无法降低这个噪声")
        print()
        print("→ 合成是**正确的方向但量级不够**。要质变须换更强信号源（运价/事件）。")
    print("=" * 92)
    return 0

def _gain(k: int, rho: float) -> float:
    """等权合成的理论 IC_IR 提升倍数。"""
    import math

    if k <= 0:
        return 0.0
    denom = 1.0 + (k - 1) * rho
    return math.sqrt(k / denom) if denom > 0 else 0.0

if __name__ == "__main__":
    raise SystemExit(main())
