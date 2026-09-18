"""P2 实机验证脚本：验证 baostock 连通性 + 核对真实字段名。

用法：
    python scripts/probe_baostock.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from do_modle.data.adapters.baostock_src import (  # noqa: E402
    BaostockProvider,
    parse_dividend_row,
)
from do_modle.objects import AdjustFlag  # noqa: E402

SYMBOL = "sh.601872"
START = date(2024, 1, 2)
END = date(2024, 3, 1)


def main() -> int:
    print("=" * 70)
    print("P2 实机验证：baostock 连通性与字段核对")
    print("=" * 70)

    provider = BaostockProvider()
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 登录失败: {type(exc).__name__}: {exc}")
        return 2
    print("[OK] 登录成功")

    try:
        # 1. 标的元数据
        inst = provider.get_instrument(SYMBOL)
        print(f"[OK] get_instrument -> {inst.symbol} {inst.name} "
              f"board={inst.board.value} list_date={inst.list_date}")

        # 2. 交易日历
        cal = provider.get_calendar(START, END)
        print(f"[OK] get_calendar -> {len(cal)} 个交易日，"
              f"首={cal[0] if cal else None} 尾={cal[-1] if cal else None}")

        # 3. 日线（不复权）
        bars = provider.get_bars(SYMBOL, "1d", START, END, adjust=AdjustFlag.NONE)
        print(f"[OK] get_bars(none) -> {len(bars)} 根")
        if bars:
            b = bars[0]
            print(f"     首根: {b.dt} O={b.open} H={b.high} L={b.low} C={b.close} "
                  f"prev_close={b.prev_close} vol={b.volume} "
                  f"suspended={b.is_suspended} st={b.is_st} adj={b.adjust_flag.value}")

        # 4. 日线（后复权）
        hfq = provider.get_bars(SYMBOL, "1d", START, END, adjust=AdjustFlag.HFQ)
        print(f"[OK] get_bars(hfq)  -> {len(hfq)} 根")
        if bars and hfq:
            print(f"     hfq 首根 close={hfq[0].close} vs none 首根 close={bars[0].close} "
                  f"(应不同)")

        # 5. 停牌 / ST 名单
        try:
            sus = provider.get_suspended(END)
            print(f"[OK] get_suspended({END}) -> {len(sus)} 只")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] get_suspended 失败: {type(exc).__name__}: {exc}")
        try:
            risk = provider.get_risk_warning(END)
            print(f"[OK] get_risk_warning({END}) -> {len(risk)} 只")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] get_risk_warning 失败: {type(exc).__name__}: {exc}")

        # 6. 分红：先探测真实字段名
        print("-" * 70)
        print("分红字段探测（用于校验 _FIELD_CANDIDATES）")
        try:
            fields = provider.probe_dividend_fields(SYMBOL, "2023")
            print(f"[OK] 真实字段: {fields}")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 分红字段探测失败: {type(exc).__name__}: {exc}")
            fields = []

        # 7. 分红解析
        try:
            divs = provider.get_dividends(SYMBOL, date(2016, 1, 1), date(2026, 9, 1))
            print(f"[OK] get_dividends -> {len(divs)} 条")
            for ca in divs[:5]:
                print(f"     {ca.ex_date} 送转={ca.share_ratio} 现金={ca.cash_per_share}")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] get_dividends 失败: {type(exc).__name__}: {exc}")

        # 8. 端到端：Bar -> TradabilityGate 涨跌停判定
        print("-" * 70)
        if bars:
            from do_modle.rules.tradability import LimitRuleTable, TradabilityGate
            from do_modle.objects import Side

            gate = TradabilityGate(LimitRuleTable())
            b = bars[0]
            up, down = gate.resolve_limits(b, inst)
            print(f"[OK] 首根 Bar 涨跌停: up={up} down={down} "
                  f"(由 prev_close={b.prev_close} 计算)")
            verdict = gate.check(b, Side.BUY, 100, instrument=inst)
            print(f"[OK] 买单判定: passed={verdict.passed} reason={verdict.reason}")

        print("=" * 70)
        print("结论：连通性与字段核对完成")
        return 0
    finally:
        provider.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
