"""真实数据上的 B&H 成本校验 —— **回测可信度的第一道实证关卡**。

逻辑：买入持有有**解析解**。回测净值与"手工持有（零成本）"的差额，
必须能被滑点 + 费用**完全解释**。对不上就说明规则层或撮合器有 bug。

为避免公司行为干扰，默认选**区间内无分红**的一段做严格校验。

用法::

    PY=".../python.exe"
    "$PY" scripts/validate_bh.py                    # 默认区间
    "$PY" scripts/validate_bh.py --start 2024-01-02 --end 2024-03-01
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
from do_modle.objects import Side  # noqa: E402
from do_modle.rules.cost import AShareCostModel, CostConfig  # noqa: E402


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2024-03-01")
    parser.add_argument("--cash", type=float, default=cfg.backtest.initial_cash)
    parser.add_argument("--qty", type=int, default=1000)
    parser.add_argument("--store", default=cfg.data.store)
    parser.add_argument(
        "--no-corporate-actions",
        action="store_true",
        help="关闭公司行为处理（用于对照）",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    symbol = cfg.stock.symbol
    qty = args.qty

    db = Path(args.store) / "market.sqlite"
    provider = CachedProvider(db)
    try:
        instrument = provider.get_instrument(symbol)
        bars = provider.get_bars(symbol, "1d", start, end)
        divs = provider.get_dividends(symbol, start, end)

        print("=" * 72)
        print("B&H 成本校验（真实数据）")
        print("=" * 72)
        print(f"标的: {symbol} {instrument.name}")
        print(f"区间: {start} ~ {end}   Bar 数: {len(bars)}   分红: {len(divs)} 条")
        if not bars:
            print("[FAIL] 区间内无数据")
            return 1
        for ca in divs:
            print(f"      分红 {ca.ex_date}  送转={ca.share_ratio}  现金={ca.cash_per_share}")

        state = {"done": False}

        def buy_and_hold(ctx):
            if state["done"] or ctx.bar is None:
                return None
            state["done"] = True
            return [ctx.order(Side.BUY, qty)]

        engine = BacktestEngine(
            provider=provider,
            instrument=instrument,
            symbol=symbol,
            handler=buy_and_hold,
            start=start,
            end=end,
            initial_cash=args.cash,
            cost=AShareCostModel(CostConfig.from_mapping({
                "commission_rate": cfg.cost.commission_rate,
                "min_commission": cfg.cost.min_commission,
                "stamp_tax_rate": cfg.cost.stamp_tax_rate,
                "transfer_rate": cfg.cost.transfer_rate,
                "slippage": cfg.cost.slippage,
            })),
            apply_corporate_actions=not args.no_corporate_actions,
        )
        result = engine.run()

        if not result.trades:
            print("[FAIL] 未产生任何成交")
            print(f"       拒单统计: {result.reject_reasons}")
            return 1

        trade = result.trades[0]
        entry_price = trade.raw_price
        exec_price = trade.price
        exit_price = bars[-1].close

        # 期末持仓 = 初始股数 × (1 + 送转比例)；现金分红按**期末持仓**计
        held = result.records[-1]["position"]
        share_ratio = sum(ca.share_ratio for ca in result.corporate_actions)
        bonus_shares = held - qty
        dividend_cash = result.dividend_cash

        # 理想净值 = 现金 - 买入成本 + 期末市值 + 分红现金
        # 其中期末市值按「送转后股数」计
        ideal_final = (
            args.cash
            - qty * entry_price
            + held * exit_price
            + dividend_cash
        )
        fees = (
            qty * exec_price * cfg.cost.commission_rate
            + qty * exec_price * cfg.cost.transfer_rate
        )
        slippage = qty * (exec_price - entry_price)
        expected_gap = fees + slippage
        actual_gap = ideal_final - result.final_nav
        residual = actual_gap - expected_gap

        print()
        print("-" * 72)
        print(f"信号日:   {result.records[0]['date']}（首个有 Bar 的交易日）")
        print(f"成交日:   {trade.dt.date()}   成交价 {exec_price}（开盘 {entry_price}）")
        print(f"期末收盘: {bars[-1].dt.date()}  {exit_price}")
        print(f"期末持仓: {held} 股（送转 +{bonus_shares}，累计比例 {share_ratio:+.4f}）")
        print("-" * 72)
        print(f"初始资金:        {args.cash:>15,.4f}")
        print(f"期末净值(回测):  {result.final_nav:>15,.4f}")
        print(f"期末净值(理想):  {ideal_final:>15,.4f}")
        print(f"  含 分红现金:   {dividend_cash:>15,.4f}")
        print(f"差额:            {actual_gap:>15,.4f}")
        print(f"  其中 滑点:     {slippage:>15,.4f}")
        print(f"  其中 费用:     {fees:>15,.4f}")
        print(f"  预期差额合计:  {expected_gap:>15,.4f}")
        print(f"  **残差**:      {residual:>15,.4f}")
        print("-" * 72)
        print(f"拒单: {result.reject_reasons or '无'}")
        print(f"总费用(逐笔):    {result.total_fee:>15,.4f}")
        if slippage == 0.0:
            print()
            print("[NOTE] 滑点为 0 —— 该价位下 0.05% 滑点小于半个 tick(0.005 元)，")
            print("       被 round_to_tick 取整吃掉。详见 P2-P3 说明 §5 问题 2。")

        ok = abs(residual) < 1e-6
        print()
        print("=" * 72)
        if ok:
            print("[PASS] 残差 < 1e-6 → 回测净值与手工持有的差异**完全由成本解释**")
            print("       规则层与撮合器可信。")
            return 0
        print(f"[FAIL] 残差 {residual:.6f} 无法被成本解释 → 规则层或撮合器有 bug")
        return 1
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
