"""P4 · 对账脚本 —— 按修正后的范围与 akquant 逐场景对账。

用法::

    .\\py.cmd scripts\\reconcile_akquant.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from do_modle.config import load_config  # noqa: E402
from do_modle.evaluation.reconcile import (  # noqa: E402
    NON_RECONCILE_SCOPE,
    RECONCILE_SCOPE,
    ReconcileEngine,
    ReconcileReport,
    akquant_available,
)
from do_modle.objects import Side  # noqa: E402
from do_modle.rules.cost import AShareCostModel, CostConfig  # noqa: E402
from tests._helpers import MAIN_INSTRUMENT, SYMBOL, make_bar  # noqa: E402

D0, D1, D2 = date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)


def _cost() -> AShareCostModel:
    cfg = load_config()
    return AShareCostModel(
        CostConfig.from_mapping(
            {
                "commission_rate": cfg.cost.commission_rate,
                "min_commission": cfg.cost.min_commission,
                "stamp_tax_rate": cfg.cost.stamp_tax_rate,
                "transfer_rate": cfg.cost.transfer_rate,
                "slippage": cfg.cost.slippage,
            }
        )
    )


def _normal_series(volume: float = 1_000_000):
    return [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=volume),
        make_bar(d=D1, open_=20.0, close=20.1, prev_close=20.0, volume=volume),
        make_bar(d=D2, open_=20.1, close=20.2, prev_close=20.1, volume=volume),
    ]


def main() -> int:
    print("=" * 74)
    print("P4 · akquant 对账")
    print("=" * 74)
    print(f"akquant 可用: {akquant_available()}")
    print()
    print("对账范围（7 项）：")
    for i, item in enumerate(RECONCILE_SCOPE, 1):
        print(f"  {i}. {item}")
    print()
    print("不对账范围（3 项）：")
    for i, item in enumerate(NON_RECONCILE_SCOPE, 1):
        print(f"  {i}. {item}")
    print()

    engine = ReconcileEngine(_cost())
    report = ReconcileReport()

    # --- 可对账场景 ---
    print("=" * 74)
    print("可对账场景")
    print("=" * 74)

    series = _normal_series()
    report.add(
        engine.reconcile_order(
            name="正常买单 1000 股",
            bar=series[1],
            side=Side.BUY,
            quantity=1000,
            instrument=MAIN_INSTRUMENT,
            series=series,
            signal_date=D0,
        )
    )

    small = [
        make_bar(d=D0, open_=6.08, close=6.08, prev_close=6.08, volume=1_000_000),
        make_bar(d=D1, open_=6.08, close=6.10, prev_close=6.08, volume=1_000_000),
        make_bar(d=D2, open_=6.10, close=6.12, prev_close=6.10, volume=1_000_000),
    ]
    report.add(
        engine.reconcile_order(
            name="低价股买单 1000 股",
            bar=small[1],
            side=Side.BUY,
            quantity=1000,
            instrument=MAIN_INSTRUMENT,
            series=small,
            signal_date=D0,
        )
    )

    thin = [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=1_000_000),
        make_bar(d=D1, open_=20.0, close=20.1, prev_close=20.0, volume=10_000),
        make_bar(d=D2, open_=20.1, close=20.2, prev_close=20.1, volume=1_000_000),
    ]
    report.add(
        engine.reconcile_order(
            name="超额订单 5000 股（先裁剪）",
            bar=thin[1],
            side=Side.BUY,
            quantity=5000,
            instrument=MAIN_INSTRUMENT,
            series=thin,
            signal_date=D0,
        )
    )

    # --- 不可对账场景（应 SKIP） ---
    print()
    print("=" * 74)
    print("不可对账场景（自建先裁决 → 应 SKIP）")
    print("=" * 74)

    limit_up = [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=1_000_000),
        make_bar(d=D1, open_=22.0, high=22.0, low=22.0, prev_close=20.0, volume=100_000),
    ]
    report.add(
        engine.reconcile_order(
            name="涨停买单",
            bar=limit_up[1],
            side=Side.BUY,
            quantity=100,
            instrument=MAIN_INSTRUMENT,
            series=limit_up,
            signal_date=D0,
        )
    )

    suspended = [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=1_000_000),
        make_bar(d=D1, open_=20.0, prev_close=20.0, suspended=True),
    ]
    report.add(
        engine.reconcile_order(
            name="停牌买单",
            bar=suspended[1],
            side=Side.BUY,
            quantity=100,
            instrument=MAIN_INSTRUMENT,
            series=suspended,
            signal_date=D0,
        )
    )

    # --- 汇总 ---
    print()
    print("=" * 74)
    print("对账结果")
    print("=" * 74)
    print(report.format_text())
    print()

    for case in report.cases:
        print(f"\n【{case.name}】{case.status} —— {case.detail}")
        if case.ours:
            print(f"  自建  : {case.ours}")
        if case.theirs:
            print(f"  akquant: {case.theirs}")

    print()
    print("=" * 74)
    if report.ok:
        print("[PASS] 无不一致项 → 自建规则层与 akquant 在可对账范围内一致")
    else:
        print(f"[FAIL] {len(report.mismatched)} 项不一致 → 需排查")
    print("=" * 74)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
