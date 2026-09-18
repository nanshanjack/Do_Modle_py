"""全区间 B&H 独立校验 + 绩效报告 + 陷阱自检。

**独立性的关键**：分红现金与送转股数由本脚本**自行重算**，不读取引擎的输出。
若两边不一致，说明引擎的公司行为处理有 bug。

用法::

    .\\py.cmd scripts\\validate_full.py
    .\\py.cmd scripts\\validate_full.py --start 2016-01-01 --end 2026-09-01
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
from do_modle.core.backtest import BacktestEngine  # noqa: E402
from do_modle.data.adapters.local_cache import CachedProvider  # noqa: E402
from do_modle.evaluation.perf import PerfReport  # noqa: E402
from do_modle.evaluation.pitfalls import run_pitfall_checks  # noqa: E402
from do_modle.numeric import floor_to_lot  # noqa: E402
from do_modle.objects import Side  # noqa: E402
from do_modle.rules.cost import AShareCostModel, CostConfig  # noqa: E402


def _cost_config(cfg) -> CostConfig:
    return CostConfig.from_mapping(
        {
            "commission_rate": cfg.cost.commission_rate,
            "min_commission": cfg.cost.min_commission,
            "stamp_tax_rate": cfg.cost.stamp_tax_rate,
            "transfer_rate": cfg.cost.transfer_rate,
            "slippage": cfg.cost.slippage,
        }
    )


def _independent_dividends(divs, qty: int, entry_date: date):
    """**独立重算**除权后的股数与累计分红现金。

    除权日当天：先按**登记日持仓**派息，再送转。
    """
    shares = qty
    cash = 0.0
    applied = []
    for ca in sorted(divs, key=lambda c: c.ex_date):
        if ca.ex_date <= entry_date:
            continue  # 除权日当天买入不享有
        cash += shares * ca.cash_per_share
        shares = int(round(shares * (1 + ca.share_ratio)))
        applied.append(ca)
    return shares, cash, applied


def _ideal_nav_path(
    *, nav, bars, divs, qty: int, cash: float, entry_date: date, entry_price: float
) -> list[tuple[date, float]]:
    """逐日重算"零成本理想持有"净值路径（无滑点、无费用）。

    用作绩效基准——B&H 策略的超额收益应完全来自成本节省（即应为负）。
    """
    price_by_date = {b.dt.date(): b.close for b in bars}
    divs_by_date: dict[date, list] = {}
    for ca in divs:
        divs_by_date.setdefault(ca.ex_date, []).append(ca)

    out: list[tuple[date, float]] = []
    shares = qty
    div_acc = 0.0
    invested = False
    for d, _ in nav:
        if not invested:
            out.append((d, cash))
            if d == entry_date:
                invested = True
            continue
        for ca in divs_by_date.get(d, ()):
            div_acc += shares * ca.cash_per_share  # 按送转前持仓派息
            shares = int(round(shares * (1 + ca.share_ratio)))
        price = price_by_date.get(d)
        if price is None:
            price = out[-1][1] and 0.0 or 0.0
        out.append((d, cash - qty * entry_price + shares * price + div_acc))
    return out


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--cash", type=float, default=cfg.backtest.initial_cash)
    parser.add_argument(
        "--qty",
        type=int,
        default=0,
        help="买入股数；0 = 按可用资金满仓买入（默认，才是真正的 B&H）",
    )
    parser.add_argument("--store", default=cfg.data.store)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    symbol = cfg.stock.symbol

    provider = CachedProvider(Path(args.store) / "market.sqlite")
    try:
        instrument = provider.get_instrument(symbol)
        bars = provider.get_bars(symbol, "1d", start, end)
        divs = provider.get_dividends(symbol, start, end)
        if not bars:
            print("[FAIL] 区间内无数据")
            return 1

        state = {"done": False}

        def buy_and_hold(ctx):
            if state["done"] or ctx.bar is None:
                return None
            state["done"] = True
            if args.qty > 0:
                qty = args.qty
            else:
                # 满仓买入：留一点余量给费用与滑点
                qty = floor_to_lot(ctx.available_cash / (ctx.bar.open * 1.001))
            if qty <= 0:
                return None
            return [ctx.order(Side.BUY, qty)]

        cost_cfg = _cost_config(cfg)
        engine = BacktestEngine(
            provider=provider,
            instrument=instrument,
            symbol=symbol,
            handler=buy_and_hold,
            start=start,
            end=end,
            initial_cash=args.cash,
            cost=AShareCostModel(cost_cfg),
        )
        result = engine.run()
    finally:
        provider.close()

    if not result.trades:
        print("[FAIL] 未产生任何成交")
        print(f"       拒单统计: {result.reject_reasons}")
        return 1

    trade = result.trades[0]
    qty = trade.quantity  # 实际成交股数（满仓时由引擎决定）
    entry_price, exec_price = trade.raw_price, trade.price
    entry_date = trade.dt.date()
    exit_price = bars[-1].close

    # ---- 独立重算 ----
    shares, div_cash, applied = _independent_dividends(divs, qty, entry_date)

    print("=" * 74)
    print("全区间 B&H 独立校验")
    print("=" * 74)
    print(f"标的       {symbol} {instrument.name}")
    print(f"区间       {start} ~ {end}   Bar {len(bars)} 根   分红 {len(divs)} 条")
    print(f"买入       {entry_date} 开盘 {entry_price} → 成交 {exec_price}")
    print(f"期末       {bars[-1].dt.date()} 收盘 {exit_price}")
    print("-" * 74)
    print(f"独立重算   除权后股数 {shares}（买入 {qty}，送转 +{shares - qty}）")
    print(f"           累计分红现金 {div_cash:,.4f}（{len(applied)} 次除权）")
    print(f"引擎输出   除权后股数 {result.records[-1]['position']}，"
          f"分红现金 {result.dividend_cash:,.4f}")

    shares_match = shares == result.records[-1]["position"]
    cash_match = abs(div_cash - result.dividend_cash) < 1e-6
    print(f"一致性     股数 {'✅' if shares_match else '❌'}   "
          f"分红 {'✅' if cash_match else '❌'}")

    # ---- 成本校验 ----
    fees = (
        qty * exec_price * cost_cfg.commission_rate
        + qty * exec_price * cost_cfg.transfer_rate
    )
    slippage = qty * (exec_price - entry_price)
    ideal_final = args.cash - qty * entry_price + shares * exit_price + div_cash
    actual_gap = ideal_final - result.final_nav
    residual = actual_gap - (fees + slippage)

    print("-" * 74)
    print(f"初始资金        {args.cash:>16,.4f}")
    print(f"期末净值(回测)  {result.final_nav:>16,.4f}")
    print(f"期末净值(独立)  {ideal_final:>16,.4f}")
    print(f"差额            {actual_gap:>16,.4f}")
    print(f"  滑点          {slippage:>16,.4f}")
    print(f"  费用          {fees:>16,.4f}")
    print(f"  预期合计      {fees + slippage:>16,.4f}")
    print(f"  **残差**      {residual:>16,.4f}")
    print(f"拒单统计        {result.reject_reasons or '无'}")

    # ---- 绩效报告（基准 = 逐日重算的"零成本理想持有"路径） ----
    print()
    print("=" * 74)
    print("绩效报告（基准 = 零成本理想持有）")
    print("=" * 74)
    ideal_nav = _ideal_nav_path(
        nav=result.nav,
        bars=bars,
        divs=divs,
        qty=qty,
        cash=args.cash,
        entry_date=entry_date,
        entry_price=entry_price,
    )
    perf = PerfReport(result.nav, benchmark=ideal_nav)
    print(perf.format_text())

    # ---- 陷阱自检 ----
    print()
    print("=" * 74)
    print("10 项陷阱自检")
    print("=" * 74)
    rep = run_pitfall_checks(
        result,
        bars=bars,
        end=end,
        in_sample_end=cfg.backtest.in_sample_end,
        cost_config=cost_cfg,
    )
    print(rep.format_text())

    print()
    print("=" * 74)
    ok = abs(residual) < 1e-6 and shares_match and cash_match
    if ok:
        print("[PASS] 残差 < 1e-6，且独立重算的股数与分红完全一致")
        print("       规则层 / 撮合器 / 公司行为处理全部可信。")
    else:
        print(f"[FAIL] 残差 {residual:.6f} 或独立重算不一致 → 有 bug")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
