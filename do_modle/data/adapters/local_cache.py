"""本地缓存数据源 —— 同步完成后，回测从这里读，不再联网。

这是**回测的默认数据源**：``sync_data.py`` 落库一次，之后所有回测都从
SQLite 读，保证结果可复现且不受网络影响。
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from do_modle.data.cache import DataCache
from do_modle.data.provider import DataProvider, ProviderError, register
from do_modle.objects import AdjustFlag, Bar, Board, CorporateAction, Forecast, Instrument

__all__ = ["CachedProvider", "FIN_TABLES", "TABLE_BY_ADJUST"]

FIN_TABLES: tuple[str, ...] = (
    "fin_profit",
    "fin_operation",
    "fin_growth",
    "fin_balance",
    "fin_cashflow",
    "fin_dupont",
)

TABLE_BY_ADJUST: dict[AdjustFlag, str] = {
    AdjustFlag.NONE: "daily_bar_none",
    AdjustFlag.HFQ: "daily_bar_hfq",
}

_MARKET_CLOSE = time(15, 0)


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt(value: Any) -> float | None:
    """可空数值：``None``/空串/NaN 统一返回 ``None``（估值字段可能缺失）。"""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


@register("cache")
class CachedProvider(DataProvider):
    """从 :class:`DataCache` 读取。"""

    name = "cache"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if not Path(self.db_path).exists():
            raise ProviderError(
                f"缓存不存在: {self.db_path}；请先执行 scripts/sync_data.py"
            )
        self.cache = DataCache(self.db_path)

    def close(self) -> None:
        self.cache.close()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- DataProvider ----

    def get_bars(
        self,
        symbol: str,
        freq: str = "1d",
        start: date | None = None,
        end: date | None = None,
        adjust: AdjustFlag = AdjustFlag.NONE,
    ) -> list[Bar]:
        table = TABLE_BY_ADJUST.get(adjust)
        if table is None:
            raise ProviderError(
                f"缓存只存了不复权与后复权；不支持 {adjust.value}（前复权会引入前视）"
            )
        rows = self.cache.read(table, symbol=symbol, order_by="date")
        bars: list[Bar] = []
        for row in rows:
            d = date.fromisoformat(str(row["date"])[:10])
            if start and d < start:
                continue
            if end and d > end:
                continue
            bars.append(
                Bar(
                    symbol=str(row["symbol"]),
                    dt=datetime.combine(d, _MARKET_CLOSE),
                    open=_to_float(row["open"]),
                    high=_to_float(row["high"]),
                    low=_to_float(row["low"]),
                    close=_to_float(row["close"]),
                    prev_close=_to_float(row["prev_close"]),
                    volume=_to_float(row["volume"]),
                    amount=_to_float(row["amount"]),
                    freq=freq,
                    adjust_flag=adjust,
                    is_suspended=bool(row["is_suspended"]),
                    is_st=bool(row["is_st"]),
                    pe_ttm=_opt(row.get("pe_ttm")),
                    pb_mrq=_opt(row.get("pb_mrq")),
                    ps_ttm=_opt(row.get("ps_ttm")),
                    pcf_ncf_ttm=_opt(row.get("pcf_ncf_ttm")),
                    turnover=_opt(row.get("turnover")),
                )
            )
        return bars

    def get_instrument(self, symbol: str) -> Instrument:
        rows = self.cache.read("instrument", symbol=symbol)
        if not rows:
            raise ProviderError(f"缓存中无标的: {symbol}")
        row = rows[0]
        return Instrument(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            board=Board(str(row["board"])),
            list_date=date.fromisoformat(str(row["list_date"])[:10]),
        )

    def get_calendar(self, start: date, end: date) -> list[date]:
        rows = self.cache.read("calendar", order_by="date")
        return [
            d
            for d in (date.fromisoformat(str(r["date"])[:10]) for r in rows)
            if start <= d <= end
        ]

    def get_dividends(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateAction]:
        rows = self.cache.read("dividend", symbol=symbol, order_by="ex_date")
        out: list[CorporateAction] = []
        for row in rows:
            ex = date.fromisoformat(str(row["ex_date"])[:10])
            if not (start <= ex <= end):
                continue
            out.append(
                CorporateAction(
                    symbol=str(row["symbol"]),
                    ex_date=ex,
                    share_ratio=_to_float(row["share_ratio"]),
                    cash_per_share=_to_float(row["cash_per_share"]),
                )
            )
        return out

    def get_forecasts(self, symbol: str, start: date, end: date) -> list[Forecast]:
        """业绩预告/快报。**按 ``pub_date``（公告日）过滤**，非报告期。"""
        rows = self.cache.read("forecast", symbol=symbol, order_by="pub_date")
        out: list[Forecast] = []
        for row in rows:
            pub = date.fromisoformat(str(row["pub_date"])[:10])
            if not (start <= pub <= end):
                continue
            stat = date.fromisoformat(str(row["stat_date"])[:10])
            out.append(
                Forecast(
                    symbol=str(row["symbol"]),
                    pub_date=pub,
                    stat_date=stat,
                    source=str(row["source"]),
                    kind=str(row["kind"] or ""),
                    chg_pct_mid=_opt(row.get("chg_pct_mid")),
                    chg_pct_up=_opt(row.get("chg_pct_up")),
                    chg_pct_down=_opt(row.get("chg_pct_down")),
                )
            )
        return out

    def get_financials(self, symbol: str) -> dict[date, dict[str, str]]:
        """按**报告期**合并 6 张财务表的原始字段。

        返回 ``{stat_date: {字段名: 原始字符串}}``。不做类型转换——
        调用方（panel.attach_financials）负责取值与 PIT。
        """
        out: dict[date, dict[str, str]] = {}
        for table in FIN_TABLES:
            if table not in self.cache.tables():
                continue
            for row in self.cache.read(table, symbol=symbol):
                raw = row.get("statDate")
                if not raw:
                    continue
                try:
                    stat = date.fromisoformat(str(raw)[:10])
                except ValueError:
                    continue
                rec = out.setdefault(stat, {})
                for k, v in row.items():
                    if k not in ("symbol", "year", "quarter", "code"):
                        rec[k] = v
        return out

    def get_shipping_indices(self) -> dict[str, list[tuple[date, float]]]:
        """全部航运运价指数，返回 ``{指数名: [(日期, 值), ...]}``（按日期升序）。"""
        if "shipping_index" not in self.cache.tables():
            return {}
        out: dict[str, list[tuple[date, float]]] = {}
        for row in self.cache.read("shipping_index", order_by="date"):
            name = str(row["index_name"])
            try:
                d = date.fromisoformat(str(row["date"])[:10])
                v = float(row["value"])
            except (TypeError, ValueError):
                continue
            out.setdefault(name, []).append((d, v))
        return out

    def stats(self) -> dict[str, int]:
        return {t: self.cache.count(t) for t in self.cache.tables()}
