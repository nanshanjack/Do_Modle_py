"""数据同步：baostock → 本地 SQLite 缓存。

支持**单标的**与**多标的（研究池）**两种模式。

用法::

    .\\py.cmd scripts\\sync_data.py                              # 单标的（config 里的交易标的）
    .\\py.cmd scripts\\sync_data.py --universe shipping          # 航运板块 12 只研究池
    .\\py.cmd scripts\\sync_data.py --symbols sh.601872,sh.600026
    .\\py.cmd scripts\\sync_data.py --universe shipping --start 2016-01-01

落库内容：

* ``daily_bar_none``  不复权（成交/涨跌停/账户口径）
* ``daily_bar_hfq``   后复权（因子/收益口径，历史不变，无前视）
* ``calendar``        交易日历
* ``instrument``      标的元数据
* ``dividend``        分红送转（送转比例 + 每股税前现金）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from do_modle.config import load_config  # noqa: E402
from do_modle.data.adapters.baostock_src import BaostockProvider  # noqa: E402
from do_modle.data.cache import DataCache  # noqa: E402
from do_modle.objects import AdjustFlag  # noqa: E402


_MAX_ATTEMPTS = 3


def _reconnect(provider: BaostockProvider) -> None:
    """断线重连：baostock 长连接在批量查询中会失效。"""
    try:
        provider.disconnect()
    except Exception:  # noqa: BLE001
        pass
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"    重连失败：{type(exc).__name__}: {exc}")


def _bar_rows(bars) -> list[dict]:
    return [
        {
            "symbol": b.symbol,
            "date": b.dt.date().isoformat(),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "prev_close": b.prev_close,
            "volume": b.volume,
            "amount": b.amount,
            "is_suspended": b.is_suspended,
            "is_st": b.is_st,
            # 估值（日频，PIT 安全）
            "pe_ttm": b.pe_ttm,
            "pb_mrq": b.pb_mrq,
            "ps_ttm": b.ps_ttm,
            "pcf_ncf_ttm": b.pcf_ncf_ttm,
            "turnover": b.turnover,
        }
        for b in bars
    ]


def load_universe(name: str) -> list[str]:
    """从 ``config/universe_<name>.yaml`` 读标的列表。"""
    path = ROOT / "config" / f"universe_{name}.yaml"
    if not path.exists():
        raise SystemExit(f"未找到研究池配置: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = (data.get("universe") or {}).get("symbols") or []
    symbols = [str(e["symbol"]) for e in entries if e.get("symbol")]
    if not symbols:
        raise SystemExit(f"{path} 中未解析出任何 symbol")
    return symbols


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=cfg.data.start)
    parser.add_argument("--end", default=cfg.backtest.end)
    parser.add_argument("--symbols", default=None, help="逗号分隔的代码列表")
    parser.add_argument("--universe", default=None, help="研究池名称，如 shipping")
    parser.add_argument("--store", default=cfg.data.store)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.universe:
        symbols = load_universe(args.universe)
    else:
        symbols = [cfg.stock.symbol]

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print("=" * 74)
    print(f"数据同步  {len(symbols)} 个标的  {start} ~ {end}")
    print(f"缓存文件: {Path(args.store) / 'market.sqlite'}")
    print("=" * 74)

    all_none, all_hfq, all_div, all_fc, instruments = [], [], [], [], []
    calendar: list[date] = []
    failures: list[tuple[str, str]] = []

    provider = BaostockProvider()
    try:
        provider.connect()
        for i, symbol in enumerate(symbols, 1):
            # baostock 长连接在批量查询中会断（WinError 10053 / 10002007），
            # 故每个标的带**重连重试**：失败 → disconnect + connect → 重试
            last_err = ""
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    inst = provider.get_instrument(symbol)
                    bars_none = provider.get_bars(
                        symbol, "1d", start, end, adjust=AdjustFlag.NONE
                    )
                    bars_hfq = provider.get_bars(
                        symbol, "1d", start, end, adjust=AdjustFlag.HFQ
                    )
                    dividends = provider.get_dividends(symbol, start, end)
                    forecasts = provider.get_forecasts(symbol, start, end)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}: {exc}"
                    if attempt < _MAX_ATTEMPTS:
                        print(
                            f"[{i:>2}/{len(symbols)}] {symbol} "
                            f"第 {attempt} 次失败，重连后重试…（{last_err[:60]}）"
                        )
                        _reconnect(provider)
                    else:
                        print(f"[{i:>2}/{len(symbols)}] {symbol} ❌ {last_err}")
                        failures.append((symbol, last_err))
            else:
                continue

            all_none.extend(bars_none)
            all_hfq.extend(bars_hfq)
            all_div.extend(dividends)
            all_fc.extend(forecasts)
            instruments.append(inst)
            if not calendar:
                calendar = provider.get_calendar(start, end)
            print(
                f"[{i:>2}/{len(symbols)}] {symbol} {inst.name:<8s} "
                f"bar={len(bars_none):>5d}  分红={len(dividends):>2d}  "
                f"预告={len(forecasts):>2d}  "
                f"首={bars_none[0].dt.date() if bars_none else '—'}"
            )
    finally:
        provider.disconnect()

    store_dir = Path(args.store)
    store_dir.mkdir(parents=True, exist_ok=True)
    db_path = store_dir / "market.sqlite"

    with DataCache(db_path) as cache:
        n1 = cache.upsert("daily_bar_none", _bar_rows(all_none), ("symbol", "date"))
        n2 = cache.upsert("daily_bar_hfq", _bar_rows(all_hfq), ("symbol", "date"))
        n3 = cache.upsert(
            "calendar", [{"date": d.isoformat()} for d in calendar], ("date",)
        )
        n4 = cache.upsert(
            "instrument",
            [
                {
                    "symbol": s.symbol,
                    "name": s.name,
                    "board": s.board.value,
                    "list_date": s.list_date.isoformat(),
                }
                for s in instruments
            ],
            ("symbol",),
        )
        n6 = (
            cache.upsert(
                "forecast",
                [
                    {
                        "symbol": f.symbol,
                        "pub_date": f.pub_date.isoformat(),
                        "stat_date": f.stat_date.isoformat(),
                        "source": f.source,
                        "kind": f.kind,
                        "chg_pct_mid": f.chg_pct_mid,
                        "chg_pct_up": f.chg_pct_up,
                        "chg_pct_down": f.chg_pct_down,
                    }
                    for f in all_fc
                ],
                ("symbol", "pub_date", "source"),
            )
            if all_fc
            else 0
        )
        n5 = (
            cache.upsert(
                "dividend",
                [
                    {
                        "symbol": c.symbol,
                        "ex_date": c.ex_date.isoformat(),
                        "share_ratio": c.share_ratio,
                        "cash_per_share": c.cash_per_share,
                    }
                    for c in all_div
                ],
                ("symbol", "ex_date"),
            )
            if all_div
            else 0
        )

    print()
    print("落库完成：")
    for table, n in (
        ("daily_bar_none", n1),
        ("daily_bar_hfq", n2),
        ("calendar", n3),
        ("instrument", n4),
        ("dividend", n5),
        ("forecast", n6),
    ):
        print(f"  {table:16s} {n:>7d} 行")

    print()
    print("数据质量校验：")
    if all_none:
        susp = sum(1 for b in all_none if b.is_suspended)
        st = sum(1 for b in all_none if b.is_st)
        bad = [b for b in all_none if b.prev_close <= 0]
        print(f"  停牌日: {susp}   ST 日: {st}   prev_close 非法行: {len(bad)}（应为 0）")
        print(f"  价格区间: {min(b.close for b in all_none)} ~ {max(b.close for b in all_none)}")
        print(f"  成功标的: {len(instruments)}/{len(symbols)}")
    print(f"缓存路径: {db_path}")
    return 0 if len(instruments) == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(main())
