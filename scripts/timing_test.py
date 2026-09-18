"""运价数据的**择时**评估（正确方法）

## 为什么不能用截面 IC

运价指数（BDTI/BDI/BCTI…）是**全行业共同指标** —— 同一天所有标的值相同。
实测：2016-01-05 六只标的的 BDTI 都是 869.0，**截面取值个数 = 1**。
→ 截面 IC 恒为 nan。

**正确的问题不是「买哪只」，而是「现在该不该持有」。**
对 601872 单标的择时，评估方法是**分层回测**。

用法::

    .\\py.cmd scripts\\timing_test.py
    .\\py.cmd scripts\\timing_test.py --horizon 20 --groups 5
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
from do_modle.evaluation.timing import quantile_timing  # noqa: E402
from do_modle.objects import AdjustFlag  # noqa: E402
from do_modle.strategy.panel import attach_shipping  # noqa: E402


def _series_for(bars, field: str) -> list[float | None]:
    return [getattr(b, field, None) for b in bars]


def _mom(values: list[float | None], lookback: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(lookback, len(values)):
        a, b = values[i - lookback], values[i]
        if a and b is not None and a != 0:
            out[i] = b / a - 1.0
    return out


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--horizon", type=int, default=20, help="前向收益天数")
    parser.add_argument("--groups", type=int, default=5)
    parser.add_argument("--store", default=cfg.data.store)
    args = parser.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    symbol = cfg.stock.symbol

    provider = CachedProvider(Path(args.store) / "market.sqlite")
    try:
        inst = provider.get_instrument(symbol)
        bars = provider.get_bars(symbol, "1d", start, end, adjust=AdjustFlag.NONE)
        indices = provider.get_shipping_indices()
    finally:
        provider.close()

    if not bars:
        print("[FAIL] 无行情数据")
        return 1
    bars = attach_shipping({symbol: bars}, indices, pit=PITGuard())[symbol]

    # 待测信号（全部是**行业共同指标**，只能用择时方法）
    bdt = _series_for(bars, "ship_bdt")
    bdi = _series_for(bars, "ship_bdi")
    bct = _series_for(bars, "ship_bct")
    close = [b.close for b in bars]

    signals: dict[str, list[float | None]] = {
        "BDTI 水平": bdt,
        "BDTI 20日变化": _mom(bdt, 20),
        "BDTI 60日变化": _mom(bdt, 60),
        "BDI 20日变化": _mom(bdi, 20),
        "BCTI 20日变化": _mom(bct, 20),
        "BDTI/BDI 比值": [
            (a / b if a is not None and b not in (None, 0) else None)
            for a, b in zip(bdt, bdi)
        ],
        "运价-股价背离(60日)": [
            (m - p if m is not None else None)
            for m, p in zip(_mom(bdt, 60), _mom(close, 60))
        ],
        "股价 20日动量（对照）": _mom(close, 20),
    }

    print("=" * 92)
    print("运价数据 · 择时评估（分层回测）")
    print("=" * 92)
    print(f"标的 {symbol} {inst.name}   区间 {start} ~ {end}   Bar {len(bars)} 根")
    print(f"前向收益 {args.horizon} 日   分 {args.groups} 组   "
          f"**不重叠采样**（每 {args.horizon} 日取 1 个观测）")
    print()
    print("⚠️ 运价是全行业共同指标 → 截面 IC 恒为 nan → 只能用择时方法评估")
    print()

    z = bonferroni_z(len(signals))
    print("=" * 92)
    print(f"{'信号':<24}{'观测':>6}{'第1组':>10}{'第2组':>10}{'第3组':>10}"
          f"{'第4组':>10}{'第5组':>10}{'顶底差':>10}{'t值':>8}")
    print("-" * 92)

    results: list[tuple[str, float, float, bool]] = []
    for name, sig in signals.items():
        res = quantile_timing(
            bars, sig, horizon=args.horizon, n_groups=args.groups
        )
        if res is None:
            print(f"{name:<24}  （数据不足）")
            continue
        gr = "".join(f"{r:>+10.2%}" for r in res.group_returns)
        print(
            f"{name:<24}{res.n_obs:>6}{gr}"
            f"{res.spread:>+10.2%}{res.spread_t:>+8.2f}"
        )
        results.append((name, res.spread, res.spread_t, res.monotonic))

    print("-" * 92)
    print(f"Bonferroni 阈值 z = {z:.3f}（候选 {len(signals)} 个）")
    print()

    print("=" * 92)
    print("结论")
    print("=" * 92)
    passed = [
        (n, sp, t, mono)
        for n, sp, t, mono in results
        if t == t and abs(t) >= z and mono
    ]
    print(f"通过门槛（|t|≥{z:.2f} **且** 分组单调）：{len(passed)} 个")
    if passed:
        for n, sp, t, _ in passed:
            print(f"  ✅ {n}: 顶底差 {sp:+.2%}  t={t:+.2f}")
    else:
        print("  ❌ 无")
    print()
    print("判读要点：")
    print("  · **单调性比 t 值更重要** —— 择时信号若无单调性，顶底差再大也是噪声")
    print("  · 不重叠采样已消除重叠窗口的自相关；若仍用重叠采样，t 值会虚高")
    print("  · 单标的择时的天然基准是**买入持有**，不是零收益")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
