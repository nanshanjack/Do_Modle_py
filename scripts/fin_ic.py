"""P6-d · 财务质量因子评估 —— **按报告期采样**（避免日频采样的自相关）

## 为什么必须按报告期采样

财报因子值在两次公告之间**完全不变**。若按日频算截面 IC：

* 同一份财报被重复计入约 **60 次**
* IC 序列 lag-1 自相关可达 **0.95+**
* 朴素 t 值虚高 **10 倍以上**

本脚本**同时输出两种口径**，让膨胀倍数可见：

    日频采样   T ≈ 2500   ← t 严重虚高，**不可用**
    报告期采样 T ≈ 30     ← 每个观测对应一次独立信息事件，**这才是判据**

用法::

    .\\py.cmd scripts\\fin_ic.py
    .\\py.cmd scripts\\fin_ic.py --horizon 60
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
    build_report_sections,
    compute_ic_series,
    report_observations,
)
from do_modle.objects import AdjustFlag  # noqa: E402
from do_modle.strategy.factors import FACTORS, factor_categories  # noqa: E402
from do_modle.strategy.panel import (  # noqa: E402
    attach_financials,
    attach_forecasts,
    build_factor_panel,
    build_return_panel,
)
from scripts.sync_data import load_universe  # noqa: E402

IC_IR_FLOOR = 0.3


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="shipping")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--horizon", type=int, default=60, help="报告期采样的前向天数")
    parser.add_argument("--store", default=cfg.data.store)
    parser.add_argument("--min-symbols", type=int, default=5)
    args = parser.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    symbols = load_universe(args.universe)

    provider = CachedProvider(Path(args.store) / "market.sqlite")
    try:
        available = [s for s in symbols if provider.cache.read("instrument", symbol=s)]
        bars_hfq = {s: provider.get_bars(s, "1d", start, end, adjust=AdjustFlag.HFQ)
                    for s in available}
        bars_none = {s: provider.get_bars(s, "1d", start, end, adjust=AdjustFlag.NONE)
                     for s in available}
        forecasts = {s: provider.get_forecasts(s, start, end) for s in available}
        financials = {s: provider.get_financials(s) for s in available}
    finally:
        provider.close()

    bars_hfq = {s: b for s, b in bars_hfq.items() if b}
    bars_none = {s: b for s, b in bars_none.items() if b}

    # PIT：预告（公告日次日）+ 财报（公告日次日）
    bars_hfq = attach_forecasts(bars_hfq, forecasts, pit=PITGuard())
    bars_hfq = attach_financials(bars_hfq, financials, pit=PITGuard())

    n_reports = sum(len(v) for v in financials.values())
    print("=" * 96)
    print("财务质量因子评估（按报告期采样）")
    print("=" * 96)
    print(f"研究池 {args.universe}（{len(available)} 只）  区间 {start} ~ {end}")
    print(f"财务报告 {n_reports} 条   报告期采样前向 {args.horizon} 日")
    print()

    quality = sorted(factor_categories().get("quality", []))
    ref = ["mom_60", "turnover_mean_20", "vol_60", "fc_score"]
    names = quality + [n for n in ref if n in FACTORS]

    print("构建面板…")
    factor_panels = build_factor_panel(bars_hfq, names, pit=PITGuard())
    return_panel = build_return_panel(bars_none, 5)  # 日频口径用 5 日
    report_return_panel = build_return_panel(bars_none, args.horizon)  # 报告期口径

    # **每个因子用它自己的信息更新事件日作为观测点**
    #   quality   → 财报变化日（fin_roe 变化）
    #   forecast  → 预告公告日（fc_score 变化）
    #   其它      → 财报变化日（作为统一的事件网格，便于横向对比）
    OBS_FIELD: dict[str, str] = {
        "fin_roe": "fin_roe",
        "fin_gp_margin": "fin_roe",
        "fin_np_margin": "fin_roe",
        "fin_asset_turn": "fin_roe",
        "fin_liab_to_asset": "fin_roe",
        "fin_yoy_ni": "fin_roe",
        "fin_cfo_to_or": "fin_roe",
        "fin_dupont_roe": "fin_roe",
        "fc_score": "fc_score",
        "fc_chg_pct": "fc_score",
        "fc_score_decay": "fc_score",
        "fc_chg_decay": "fc_score",
        "fc_age": "fc_score",
    }
    obs_cache: dict[str, dict] = {}

    def obs_for(field: str) -> dict:
        if field not in obs_cache:
            obs_cache[field] = {
                s: report_observations(b, field) for s, b in bars_hfq.items()
            }
        return obs_cache[field]

    n_fin = sum(len(v) for v in obs_for("fin_roe").values())
    n_fc = sum(len(v) for v in obs_for("fc_score").values())
    print(f"事件观测点：财报 {n_fin} 个 / 预告 {n_fc} 个")
    print()

    # ---- 双口径对比 ----
    print("=" * 96)
    print("日频采样 vs 报告期采样（**后者才是判据**）")
    print("=" * 96)
    print(
        f"{'因子':<20}{'类别':<11}"
        f"{'日频T':>6}{'日频t':>8}{'日频膨胀':>9}"
        f"{'报告T':>6}{'报告IC_IR':>11}{'报告t':>8}"
    )
    print("-" * 96)

    z_daily = bonferroni_z(len(names))
    z_report = bonferroni_z(len(quality))
    rows: list[tuple[str, float, float]] = []

    for n in names:
        # 日频
        secs_d = build_cross_sections(
            factor_panels[n], return_panel, min_symbols=args.min_symbols
        )
        sd = compute_ic_series(secs_d).summary() if secs_d else {}
        # 报告期（用该因子自己的事件日）
        obs = obs_for(OBS_FIELD.get(n, "fin_roe"))
        secs_r = build_report_sections(
            factor_panels[n], report_return_panel, obs, min_symbols=args.min_symbols
        )
        sr = compute_ic_series(secs_r).summary() if secs_r else {}

        cat = FACTORS[n].category
        if not sd or not sr:
            print(f"{n:<20}{cat:<11}  （截面不足，跳过）")
            continue
        print(
            f"{n:<20}{cat:<11}"
            f"{int(sd['n_periods']):>6}{sd['t_stat']:>+8.2f}"
            f"{sd['t_stat_naive'] / sd['t_stat'] if sd['t_stat'] else float('nan'):>9.2f}"
            f"{int(sr['n_periods']):>6}{sr['ic_ir']:>+11.4f}{sr['t_stat']:>+8.2f}"
        )
        rows.append((n, sr["ic_ir"], sr["t_stat"]))

    print("-" * 96)
    print(f"报告期采样 Bonferroni 阈值 z = {z_report:.3f}（候选 {len(quality)} 个财务因子）")
    print()

    # ---- 结论 ----
    print("=" * 96)
    print("结论")
    print("=" * 96)
    q_rows = [r for r in rows if FACTORS[r[0]].category in ("quality", "forecast")]
    if not q_rows:
        print("无有效的财务因子截面。")
        return 0

    best = max(q_rows, key=lambda r: abs(r[1]) if r[1] == r[1] else -1)
    passed = [r for r in q_rows if abs(r[2]) >= z_report and abs(r[1]) >= IC_IR_FLOOR]
    print(f"财务/预告因子 {len(q_rows)} 个，|IC_IR| 最大：{best[0]} = {best[1]:+.4f}（t={best[2]:+.2f}）")
    print(f"通过门槛（|t|≥{z_report:.2f} 且 |IC_IR|≥{IC_IR_FLOOR}）：{len(passed)} 个")
    if passed:
        for n, ir, t in passed:
            print(f"  ✅ {n}: IC_IR={ir:+.4f}  t={t:+.2f}")
    else:
        print("  ❌ 无")
    print()
    print("判读提醒：")
    print(f"  · 报告期采样 T 仅 {int(rows[0][1] and 0) or '数十'} 量级 → 统计功效天然低")
    print("  · 财务因子要真正可用，IC_IR 需显著高于噪声下界（1/√N）")
    print("  · 若日频 t 远大于报告期 t，说明日频口径不可信（自相关虚高）")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
