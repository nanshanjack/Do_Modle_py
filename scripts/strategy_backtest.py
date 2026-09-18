"""P6-g · 因子端到端择时回测 —— **因子能不能变成正收益**。

## 为什么必须做这一步

此前 78 个因子的评估**全部停在 IC 层面**（相关性）。IC 显著 ≠ 能赚钱：
IC 不涉及成本、T+1、涨跌停、手数取整，也不涉及"要交易多少次"。
本脚本补上最后一环：

    因子值 → 目标仓位 → 引擎（T+1 / 涨跌停 / 真实成本）→ 净值

## 规则（只有一个参数，压缩拟合空间）

    目标仓位 = 1   if  sign × 因子值(T) > 过去 window 日因子值中位数
             = 0   otherwise

* 中位数只用 **≤ T** 的因子观测 → PIT 安全
* `sign` 由**训练段（前半）时序 IC 符号**决定 —— 不是事后按净值调
* 因子有效观测 < `min_obs` 时目标仓位 = 0（冷启动不交易）

## 两个对照

1. **B&H**：同一引擎、同一成本、同一成交时点 —— 择时策略的真实对手
2. **`--gross`**：关掉费率与滑点 —— 用来区分
   「信号本身无效」还是「成本把有效信号吃掉了」

## 样本外

全期净值按 `--split`（默认 2021-12-31）切片，**后半段才是样本外**。
方向在训练段定，评估在后半段看。

用法::

    .\\py.cmd scripts\\strategy_backtest.py                    # 全因子扫描（含成本）
    .\\py.cmd scripts\\strategy_backtest.py --gross            # 毛收益对照
    .\\py.cmd scripts\\strategy_backtest.py --factor mom_20 --detail
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from do_modle.config import load_config  # noqa: E402
from do_modle.core.backtest import BacktestEngine, EngineContext  # noqa: E402
from do_modle.data.adapters.local_cache import CachedProvider  # noqa: E402
from do_modle.data.pit import PITGuard  # noqa: E402
from do_modle.data.provider import DataProvider  # noqa: E402
from do_modle.evaluation.perf import PerfReport  # noqa: E402
from do_modle.numeric import spearman  # noqa: E402
from do_modle.objects import (  # noqa: E402
    AdjustFlag,
    Bar,
    CorporateAction,
    Instrument,
    Side,
)
from do_modle.rules.cost import AShareCostModel, CostConfig  # noqa: E402
from do_modle.strategy.factors import factor_names  # noqa: E402
from do_modle.strategy.panel import (  # noqa: E402
    attach_financials,
    attach_forecasts,
    attach_shipping,
    build_factor_panel,
    forward_return,
)


class MemoryProvider(DataProvider):
    """把已加载数据直接喂给引擎 —— 避免每个因子都重读 SQLite。"""

    def __init__(
        self,
        *,
        bars: list[Bar],
        calendar: list[date],
        instrument: Instrument,
        dividends: list[CorporateAction],
    ) -> None:
        self._bars = list(bars)
        self._calendar = list(calendar)
        self._instrument = instrument
        self._dividends = list(dividends)

    def get_bars(
        self,
        symbol: str,
        freq: str,
        start: date,
        end: date,
        adjust: AdjustFlag = AdjustFlag.NONE,
    ) -> list[Bar]:
        return [b for b in self._bars if start <= b.dt.date() <= end]

    def get_instrument(self, symbol: str) -> Instrument:
        return self._instrument

    def get_calendar(self, start: date, end: date) -> list[date]:
        return [d for d in self._calendar if start <= d <= end]

    def get_dividends(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return [a for a in self._dividends if start <= a.ex_date <= end]


def rolling_targets(
    series: list[tuple[date, float]], sign: int, window: int, min_obs: int
) -> dict[date, int]:
    """因子序列 → 目标仓位序列。中位数只用 ≤ 当日的观测。"""
    out: dict[date, int] = {}
    obs: list[float] = []
    for d, v in series:
        obs.append(v)
        if len(obs) < min_obs:
            continue
        med = statistics.median(obs[-window:])
        out[d] = 1 if sign * v > med else 0
    return out


def shifted_targets(
    series: list[tuple[date, float]],
    sign: int,
    window: int,
    min_obs: int,
    shift: int,
) -> dict[date, int]:
    """把目标仓位序列**循环移位** ``shift`` 步。

    安慰剂检验的标准做法：移位**完全保留**在场比例、换手次数与持续性
    （即保留了所有"信号结构"），只破坏**信号与收益的时序对齐**。
    若真实信号的超额并不优于自己的移位版本 → 该超额来自运气而非预测力。
    """
    base = rolling_targets(series, sign, window, min_obs)
    dates = sorted(base)
    values = [base[d] for d in dates]
    n = len(values)
    if n < 2:
        return {}
    k = shift % n
    rotated = values[-k:] + values[:-k] if k else values
    return dict(zip(dates, rotated))


def make_timing_handler(targets: dict[date, int]):
    """目标仓位 → 订单。满仓 = 用可用资金买最大手数；空仓 = 卖出全部可卖。"""

    def handler(ctx: EngineContext):
        if ctx.bar is None:
            return None
        target = targets.get(ctx.trading_date)
        if target is None:
            return None
        if target == 1:
            if ctx.position > 0:
                return None
            qty = ctx.account.max_buy_quantity(ctx.bar.close)
            return [ctx.order(Side.BUY, qty)] if qty > 0 else None
        if ctx.sellable > 0:
            return [ctx.order(Side.SELL, ctx.sellable)]
        return None

    return handler


def make_bh_handler():
    """买入持有：首个有 Bar 的日子满仓买入，之后不动。"""
    state = {"done": False}

    def handler(ctx: EngineContext):
        if state["done"] or ctx.bar is None:
            return None
        qty = ctx.account.max_buy_quantity(ctx.bar.close)
        if qty <= 0:
            return None
        state["done"] = True
        return [ctx.order(Side.BUY, qty)]

    return handler


def run_engine(
    *,
    provider: MemoryProvider,
    instrument: Instrument,
    symbol: str,
    handler,
    start: date,
    end: date,
    cash: float,
    cost: AShareCostModel,
):
    engine = BacktestEngine(
        provider=provider,
        instrument=instrument,
        symbol=symbol,
        handler=handler,
        start=start,
        end=end,
        initial_cash=cash,
        cost=cost,
    )
    return engine.run()


def slice_nav(nav: list[tuple[date, float]], start: date) -> list[tuple[date, float]]:
    out = [(d, v) for d, v in nav if d >= start]
    return out if len(out) >= 2 else nav[-2:]


def metrics(nav: list[tuple[date, float]]) -> dict[str, float]:
    return PerfReport(list(nav)).metrics()


def in_market_ratio(result) -> float:
    rows = result.records
    if not rows:
        return 0.0
    held = sum(1 for r in rows if r.get("position", 0) > 0)
    return held / len(rows)


def main() -> int:
    cfg = load_config()
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default=None, help="忽略；本脚本只做单标的交易轨")
    p.add_argument("--start", default="2016-01-04")
    p.add_argument("--end", default="2026-09-01")
    p.add_argument("--split", default="2021-12-31", help="样本内/外切分点")
    p.add_argument("--window", type=int, default=250, help="滚动中位数窗口")
    p.add_argument("--min-obs", type=int, default=120, help="冷启动所需最少观测")
    p.add_argument("--horizon", type=int, default=5, help="训练段时序 IC 的前向天数")
    p.add_argument("--cash", type=float, default=None)
    p.add_argument("--store", default=cfg.data.store)
    p.add_argument("--factor", default=None, help="只跑指定因子，逗号分隔")
    p.add_argument("--gross", action="store_true", help="关掉费率与滑点（毛收益对照）")
    p.add_argument("--top", type=int, default=15, help="表格显示前 N 行")
    p.add_argument(
        "--null",
        type=int,
        default=0,
        metavar="N",
        help="安慰剂检验：对样本外前几名做 N 次随机循环移位，给出 p 值（0=关闭）",
    )
    p.add_argument("--null-top", type=int, default=3, help="对样本外前 K 名做安慰剂检验")
    p.add_argument("--seed", type=int, default=20260918, help="安慰剂检验随机种子")
    args = p.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    split = date.fromisoformat(args.split)
    cash = args.cash if args.cash is not None else cfg.backtest.initial_cash
    symbol = cfg.stock.symbol

    if args.gross:
        cost_cfg = CostConfig(
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            transfer_rate=0.0,
            slippage=0.0,
        )
        cost_label = "毛收益（零费率 / 零滑点）"
    else:
        cost_cfg = CostConfig.from_mapping(
            {
                "commission_rate": cfg.cost.commission_rate,
                "min_commission": cfg.cost.min_commission,
                "stamp_tax_rate": cfg.cost.stamp_tax_rate,
                "transfer_rate": cfg.cost.transfer_rate,
                "slippage": cfg.cost.slippage,
            }
        )
        cost_label = "含成本（万一免五 + 滑点 ≥1 tick）"
    cost = AShareCostModel(cost_cfg)

    provider = CachedProvider(Path(args.store) / "market.sqlite")
    try:
        instrument = provider.get_instrument(symbol)
        bars_none = provider.get_bars(symbol, "1d", start, end, adjust=AdjustFlag.NONE)
        bars_hfq = provider.get_bars(symbol, "1d", start, end, adjust=AdjustFlag.HFQ)
        calendar = provider.get_calendar(start, end)
        dividends = provider.get_dividends(symbol, start, end)
        forecasts = provider.get_forecasts(symbol, start, end)
        financials = provider.get_financials(symbol)
        indices = provider.get_shipping_indices()
    finally:
        provider.close()

    if not bars_none or not bars_hfq:
        print(f"[FAIL] {symbol} 区间 {start}~{end} 无数据")
        return 1

    # 把 PIT 附加数据挂到 Bar 上，使 fc_* / fin_* / ship_* 因子可用
    hfq = {symbol: bars_hfq}
    hfq = attach_forecasts(hfq, {symbol: forecasts}, pit=PITGuard())
    hfq = attach_financials(hfq, {symbol: financials}, pit=PITGuard())
    hfq = attach_shipping(hfq, indices, pit=PITGuard())
    bars_hfq = hfq[symbol]

    names = [s.strip() for s in args.factor.split(",")] if args.factor else factor_names()
    factor_panels = build_factor_panel({symbol: bars_hfq}, names, pit=PITGuard())

    # 训练段时序 IC 的前向收益（T+1 开盘 → T+h 收盘，与撮合口径一致）
    fwd: dict[date, float] = {}
    for i, bar in enumerate(bars_none):
        r = forward_return(bars_none, i, args.horizon)
        if r is not None:
            fwd[bar.dt.date()] = r

    mem = MemoryProvider(
        bars=bars_none, calendar=calendar, instrument=instrument, dividends=dividends
    )

    print("=" * 100)
    print(f"P6-g · 因子端到端择时回测    {symbol} {instrument.name}")
    print("=" * 100)
    print(f"区间 {start} ~ {end}   Bar {len(bars_none)}   初始资金 {cash:,.0f}")
    print(f"成本口径：{cost_label}")
    print(
        f"规则：sign × 因子(T) > 滚动 {args.window} 日中位数 → 满仓，否则空仓"
        f"（最少观测 {args.min_obs}）"
    )
    print(f"方向 sign 由训练段（{start} ~ {split}）时序 IC 符号决定；样本外 = {split} 之后")
    print()

    # ---- 基准：同一引擎下的 B&H ----
    bh = run_engine(
        provider=mem,
        instrument=instrument,
        symbol=symbol,
        handler=make_bh_handler(),
        start=start,
        end=end,
        cash=cash,
        cost=cost,
    )
    bh_m = metrics(bh.nav)
    bh_oos = metrics(slice_nav(bh.nav, split))
    print("-" * 100)
    print(
        f"基准 B&H（同引擎同成本）：全期 {bh_m['total_return']:+.2%}"
        f"  年化 {bh_m['annual_return']:+.2%}  最大回撤 {bh_m['max_drawdown']:.2%}"
        f"  |  后半段 {bh_oos['total_return']:+.2%}"
    )
    print(f"      成交 {len(bh.trades)} 笔   总费用 {bh.total_fee:,.2f}")
    print("-" * 100)
    print()

    rows: list[dict] = []
    for name in names:
        panel = factor_panels.get(name) or {}
        series = sorted((d, v[symbol]) for d, v in panel.items() if symbol in v)
        if len(series) < args.min_obs + 20:
            continue

        # 训练段时序 IC → 方向
        pairs = [(v, fwd[d]) for d, v in series if d <= split and d in fwd]
        if len(pairs) < 60:
            continue
        ic = spearman([a for a, _ in pairs], [b for _, b in pairs])
        if ic != ic or ic == 0.0:
            continue
        sign = 1 if ic > 0 else -1

        for s in (sign, -sign):
            targets = rolling_targets(series, s, args.window, args.min_obs)
            if not targets:
                continue
            res = run_engine(
                provider=mem,
                instrument=instrument,
                symbol=symbol,
                handler=make_timing_handler(targets),
                start=start,
                end=end,
                cash=cash,
                cost=cost,
            )
            m = metrics(res.nav)
            o = metrics(slice_nav(res.nav, split))
            rows.append(
                {
                    "factor": name,
                    "sign": s,
                    "is_oos": s == sign,
                    "ic": ic,
                    "nav": m["end"],
                    "ret": m["total_return"],
                    "ann": m["annual_return"],
                    "mdd": m["max_drawdown"],
                    "sharpe": m["sharpe"],
                    "oos_ret": o["total_return"],
                    "oos_ann": o["annual_return"],
                    "oos_mdd": o["max_drawdown"],
                    "excess_oos": o["total_return"] - bh_oos["total_return"],
                    "trades": len(res.trades),
                    "fee": res.total_fee,
                    "held": in_market_ratio(res),
                    "_series": series,
                }
            )

    if not rows:
        print("[FAIL] 没有任何因子产出有效结果")
        return 1

    # ---- 表 1：样本外（方向由训练段决定） ----
    oos_rows = [r for r in rows if r["is_oos"]]
    oos_rows.sort(key=lambda r: -r["excess_oos"])
    print("=" * 100)
    print(f"表 1 · 样本外（{split} 之后，方向由训练段决定，**唯一无偏的结果**）")
    print("=" * 100)
    hdr = (
        f"{'因子':<20}{'方向':>5}{'训练IC':>9}{'后半收益':>10}{'后半年化':>10}"
        f"{'最大回撤':>10}{'vs B&H':>10}{'成交':>7}{'在场%':>8}{'总费用':>11}"
    )
    print(hdr)
    print("-" * 100)
    print(
        f"{'B&H（基准）':<20}{'—':>5}{'—':>9}{bh_oos['total_return']:>+10.2%}"
        f"{bh_oos['annual_return']:>+10.2%}{bh_oos['max_drawdown']:>10.2%}"
        f"{0.0:>+10.2%}{len(bh.trades):>7}{100.0:>8.1f}{bh.total_fee:>11,.0f}"
    )
    for r in oos_rows[: args.top]:
        print(
            f"{r['factor']:<20}{'+' if r['sign'] > 0 else '−':>5}{r['ic']:>+9.3f}"
            f"{r['oos_ret']:>+10.2%}{r['oos_ann']:>+10.2%}{r['oos_mdd']:>10.2%}"
            f"{r['excess_oos']:>+10.2%}{r['trades']:>7}{r['held'] * 100:>8.1f}"
            f"{r['fee']:>11,.0f}"
        )
    print()

    # ---- 表 2：事后最优方向（上界，需折价） ----
    print("=" * 100)
    print("表 2 · 全期净值（**含事后挑最优方向的上界**，用于判断天花板）")
    print("=" * 100)
    print(
        f"{'因子':<20}{'方向':>5}{'事后最优?':>10}{'期末净值':>14}{'全期收益':>10}"
        f"{'年化':>9}{'Sharpe':>8}{'最大回撤':>10}"
    )
    print("-" * 100)
    print(
        f"{'B&H（基准）':<20}{'—':>5}{'—':>10}{bh_m['end']:>14,.0f}"
        f"{bh_m['total_return']:>+10.2%}{bh_m['annual_return']:>+9.2%}"
        f"{bh_m['sharpe']:>8.2f}{bh_m['max_drawdown']:>10.2%}"
    )
    for r in sorted(rows, key=lambda x: -x["ret"])[: args.top]:
        print(
            f"{r['factor']:<20}{'+' if r['sign'] > 0 else '−':>5}"
            f"{'是' if r['is_oos'] else '否':>10}{r['nav']:>14,.0f}"
            f"{r['ret']:>+10.2%}{r['ann']:>+9.2%}{r['sharpe']:>8.2f}"
            f"{r['mdd']:>10.2%}"
        )
    print()

    # ---- 安慰剂检验（随机循环移位） ----
    placebo: dict[str, tuple[float, float, float]] = {}
    if args.null > 0 and oos_rows:
        import random

        rng = random.Random(args.seed)
        print("=" * 100)
        print(f"安慰剂检验 · 目标仓位序列随机循环移位 ×{args.null}（保结构、断对齐）")
        print("=" * 100)
        print(
            f"{'因子':<20}{'真实超额':>11}{'安慰剂均值':>12}{'安慰剂P95':>12}"
            f"{'优于次数':>10}{'p 值':>9}"
        )
        print("-" * 100)
        for r in oos_rows[: args.null_top]:
            excesses: list[float] = []
            for _ in range(args.null):
                shifted = shifted_targets(
                    r["_series"], r["sign"], args.window, args.min_obs, rng.randrange(1, 10**6)
                )
                if not shifted:
                    continue
                sim = run_engine(
                    provider=mem,
                    instrument=instrument,
                    symbol=symbol,
                    handler=make_timing_handler(shifted),
                    start=start,
                    end=end,
                    cash=cash,
                    cost=cost,
                )
                excesses.append(metrics(slice_nav(sim.nav, split))["total_return"] - bh_oos["total_return"])
            if not excesses:
                continue
            excesses.sort()
            beat = sum(1 for e in excesses if e >= r["excess_oos"])
            p_value = (1 + beat) / (1 + len(excesses))
            p95 = excesses[int(0.95 * (len(excesses) - 1))]
            placebo[r["factor"]] = (p_value, p95, sum(excesses) / len(excesses))
            print(
                f"{r['factor']:<20}{r['excess_oos']:>+11.2%}"
                f"{sum(excesses) / len(excesses):>+12.2%}{p95:>+12.2%}"
                f"{beat:>7}/{len(excesses):<3}{p_value:>9.3f}"
            )
        print()
        print("判读：p 值 = 自己的随机移位版本中不差于真实信号的比例。")
        print("      移位保留了在场比例 / 换手次数 / 持续性，只破坏与收益的时序对齐。")
        print(f"      多重检验：{len(oos_rows)} 个因子，单看 p<0.05 纯噪声下也期望命中 "
              f"{0.05 * len(oos_rows):.1f} 个 → 门槛应取 p < {0.05 / len(oos_rows):.5f}")
        print()

    # ---- 结论 ----
    n_factors = len(oos_rows)
    win = [r for r in oos_rows if r["excess_oos"] > 0]
    win_all = [r for r in rows if r["excess_oos"] > 0]
    pos_abs = [r for r in oos_rows if r["oos_ret"] > 0]
    print("=" * 100)
    print("结论")
    print("=" * 100)
    print(f"可评估因子数：{n_factors}")
    print(f"  样本外绝对正收益：{len(pos_abs)} / {n_factors}")
    print(f"  样本外跑赢 B&H（方向由训练段定）：{len(win)} / {n_factors}")
    print(f"  全期跑赢 B&H（含事后挑最优方向，{len(rows)} 个方向组合）：{len(win_all)} / {len(rows)}")
    if oos_rows:
        best = oos_rows[0]
        print(
            f"  样本外最好：{best['factor']}（{'正' if best['sign'] > 0 else '负'}向）"
            f" 收益 {best['oos_ret']:+.2%}  超额 {best['excess_oos']:+.2%}"
            f"  成交 {best['trades']} 笔"
        )
        print(
            f"  样本外最差：{oos_rows[-1]['factor']} 超额 "
            f"{oos_rows[-1]['excess_oos']:+.2%}"
        )
    avg_held = sum(r["held"] for r in oos_rows) / len(oos_rows) if oos_rows else 0.0
    avg_fee = sum(r["fee"] for r in oos_rows) / len(oos_rows) if oos_rows else 0.0
    print(f"  平均在场比例：{avg_held:.1%}   平均总费用：{avg_fee:,.0f} 元")
    print()
    if args.gross:
        print("[NOTE] 本次为**毛收益**口径。与含成本结果对比即可分离「信号无效」与「成本拖累」。")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
