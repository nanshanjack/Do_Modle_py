"""运价**事件研究** —— 检验「供应扰动 → 运价飙升 → 股价上涨」的假设

## 假设（用户提出）

**霍尔木兹海峡通航骤减对油运公司是利好**：供应扰动 → 有效运力减少 / 绕行 →
**运价飙升** → 油运公司盈利改善 → 股价上涨。

**这是正确的供给侧逻辑**。历史案例：
- 2019 年霍尔木兹油轮遇袭 → VLCC 运价暴涨
- 2024 年红海危机 → 绕行 → 运价涨 → 航运股大涨

## 数据约束与解决方案

**约束**：霍尔木兹通航量的公开历史**只有 ≤1 年**，无法回测。

**解决**：用 **BDTI 单日暴涨**作为「供应扰动已发生」的**代理变量**。
运价暴涨本身就说明供应侧发生了冲击（无论起因是地缘、天气还是运力事故）。

**BDTI 有 25 年历史（2001 年起）** → 完全可回测。

## 方法

事件研究，基准 = **全样本同 horizon 的平均收益**（剥离「这只股票本来就涨」）：

    超额 = 事件后收益 − 无条件基准

用法::

    .\\py.cmd scripts\\event_study.py
    .\\py.cmd scripts\\event_study.py --threshold 0.05 --horizon 20
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
from do_modle.evaluation.timing import event_study  # noqa: E402
from do_modle.objects import AdjustFlag  # noqa: E402
from do_modle.strategy.panel import attach_shipping  # noqa: E402


def _jump_dates(bars, field: str, threshold: float) -> list[date]:
    """找出该字段**单日涨幅 ≥ threshold** 的日期（信号日）。"""
    out: list[date] = []
    prev = None
    for b in bars:
        v = getattr(b, field, None)
        if v is not None and prev is not None and prev > 0:
            if v / prev - 1.0 >= threshold:
                out.append(b.dt.date())
        prev = v
    return out


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--horizon", type=int, default=20, help="事件后持有天数")
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

    print("=" * 96)
    print("运价事件研究 —— 检验「供应扰动 → 运价飙升 → 股价上涨」")
    print("=" * 96)
    print(f"标的 {symbol} {inst.name}   区间 {start} ~ {end}   Bar {len(bars)} 根")
    print(f"事件后持有 {args.horizon} 日   基准 = 全样本同 horizon 平均收益")
    print()
    print("假设（供给侧逻辑）：运价暴涨 = 供应扰动已发生 → 油运公司受益 → 后续股价上涨")
    print()

    # 事件定义：BDTI / BDI / BCTI 单日涨幅阈值
    specs: list[tuple[str, str, float]] = [
        ("BDTI 单日 +3%", "ship_bdt", 0.03),
        ("BDTI 单日 +5%", "ship_bdt", 0.05),
        ("BDTI 单日 +8%", "ship_bdt", 0.08),
        ("BDTI 单日 +10%", "ship_bdt", 0.10),
        ("BDI 单日 +5%", "ship_bdi", 0.05),
        ("BCTI 单日 +5%", "ship_bct", 0.05),
    ]

    z = bonferroni_z(len(specs))
    print("=" * 96)
    print(f"{'事件定义':<18}{'次数':>6}{'事件后':>11}{'基准':>11}"
          f"{'超额':>11}{'t值':>8}{'事件胜率':>10}{'基准胜率':>10}")
    print("-" * 96)

    results: list[tuple[str, int, float, float]] = []
    for name, field, thr in specs:
        ev = _jump_dates(bars, field, thr)
        res = event_study(bars, ev, horizon=args.horizon)
        if res is None:
            print(f"{name:<18}{len(ev):>6}   （事件不足，跳过）")
            continue
        print(
            f"{name:<18}{res.n_events:>6}"
            f"{res.mean_event_return:>+11.2%}{res.mean_baseline_return:>+11.2%}"
            f"{res.abnormal_return:>+11.2%}{res.t_stat:>+8.2f}"
            f"{res.hit_rate:>10.1%}{res.baseline_hit_rate:>10.1%}"
        )
        results.append((name, res.n_events, res.abnormal_return, res.t_stat))

    print("-" * 96)
    print(f"Bonferroni 阈值 z = {z:.3f}（候选 {len(specs)} 个事件定义）")
    print()

    print("=" * 96)
    print("结论")
    print("=" * 96)
    passed = [r for r in results if r[3] == r[3] and abs(r[3]) >= z]
    print(f"通过门槛（|t| ≥ {z:.2f}）：{len(passed)} 个")
    if passed:
        for name, n, ab, t in passed:
            direction = "利好（运价涨→股价涨）" if ab > 0 else "利空（运价涨→股价跌）"
            print(f"  ✅ {name}: 超额 {ab:+.2%}  t={t:+.2f}  n={n}  → {direction}")
    else:
        print("  ❌ 无")
    print()
    print("判读要点：")
    print("  · 基准是**全样本平均**，不是零 —— 已剥离「这只股票本来就涨」")
    print("  · 事件后收益从**次日开盘**起算（与撮合口径一致）")
    print("  · 霍尔木兹通航量的公开历史只有 ≤1 年，故用**运价暴涨**作为供应扰动的代理")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
