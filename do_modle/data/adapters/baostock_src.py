"""baostock 数据源适配器。

**已实机核验的接口与字段**（下载 ``baostock-0.9.3`` wheel 解包读源码与 demo）：

* ``query_history_k_data_plus(code, fields, start_date, end_date, frequency, adjustflag)``
* 日线字段：``date, code, open, high, low, close, preclose, volume, amount,
  adjustflag, turn, tradestatus, pctChg, peTTM, pbMRQ, psTTM, pcfNcfTTM, isST``
* ``adjustflag``：``"1"`` 后复权 / ``"2"`` 前复权 / ``"3"`` 不复权
* ``query_trade_dates`` / ``query_stock_basic`` / ``query_dividend_data`` /
  ``query_adjust_factor`` / ``query_suspended_stocks`` / ``query_stocks_in_risk``

**字段 → 规则层映射（地基）**：

===================  ====================  ================================
baostock 字段        Bar 字段               用途
===================  ====================  ================================
``preclose``         ``prev_close``        **涨跌停价的唯一正确基准**
``tradestatus``      ``is_suspended``      ``"0"`` → 停牌
``isST``             ``is_st``             ``"1"`` → 涨跌幅 5%（主板）
``adjustflag``       ``adjust_flag``       标记该行复权口径，**不可混用**
===================  ====================  ================================

纯解析函数（``infer_board`` / ``row_to_bar`` / …）在模块级，**不依赖 baostock
即可单测**；``baostock`` 只在 :meth:`connect` 时惰性导入。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Mapping, Sequence

from do_modle.data.provider import DataProvider, ProviderError, register
from do_modle.objects import (
    AdjustFlag,
    Bar,
    Board,
    CorporateAction,
    Forecast,
    Instrument,
)

__all__ = [
    "BaostockProvider",
    "DAILY_FIELDS",
    "infer_board",
    "parse_adjust_flag",
    "adjust_flag_to_baostock",
    "row_to_bar",
    "parse_trade_dates",
    "parse_stock_basic",
    "parse_dividend_row",
    "parse_forecast_row",
    "parse_express_row",
    "parse_symbol_set",
    "resolve_baostock_func",
]

DAILY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)

_MARKET_CLOSE = time(15, 0)

_ADJUST_FROM_BAOSTOCK: dict[str, AdjustFlag] = {
    "1": AdjustFlag.HFQ,
    "2": AdjustFlag.QFQ,
    "3": AdjustFlag.NONE,
}
_ADJUST_TO_BAOSTOCK: dict[AdjustFlag, str] = {
    v: k for k, v in _ADJUST_FROM_BAOSTOCK.items()
}

# 代码前缀 → 板块
_BOARD_PREFIXES: tuple[tuple[str, Board], ...] = (
    ("sh.688", Board.STAR),
    ("sh.689", Board.STAR),
    ("sz.300", Board.GEM),
    ("sz.301", Board.GEM),
    ("bj.", Board.BSE),
    ("sh.600", Board.MAIN),
    ("sh.601", Board.MAIN),
    ("sh.603", Board.MAIN),
    ("sh.605", Board.MAIN),
    ("sz.000", Board.MAIN),
    ("sz.001", Board.MAIN),
    ("sz.002", Board.MAIN),
    ("sz.003", Board.MAIN),
)


# ==================== 纯解析函数（可脱离 baostock 单测） ====================


def infer_board(symbol: str) -> Board:
    """由代码前缀推断板块。

    >>> infer_board("sh.601872")
    <Board.MAIN: 'MAIN'>
    """
    code = symbol.strip().lower()
    for prefix, board in _BOARD_PREFIXES:
        if code.startswith(prefix):
            return board
    raise ProviderError(f"无法推断板块（未知代码前缀）: {symbol!r}")


def parse_adjust_flag(value: str | int | None) -> AdjustFlag:
    """baostock ``adjustflag`` → :class:`AdjustFlag`。"""
    key = str(value).strip() if value is not None else "3"
    if key not in _ADJUST_FROM_BAOSTOCK:
        raise ProviderError(f"未知 adjustflag: {value!r}（应为 1/2/3）")
    return _ADJUST_FROM_BAOSTOCK[key]


def adjust_flag_to_baostock(flag: AdjustFlag) -> str:
    """:class:`AdjustFlag` → baostock ``adjustflag``。"""
    if flag not in _ADJUST_TO_BAOSTOCK:
        raise ProviderError(f"不支持的复权口径: {flag!r}")
    return _ADJUST_TO_BAOSTOCK[flag]


def _num(value: Any) -> float | None:
    """把 baostock 的字符串数值转 float；空串/None → None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _flag(value: Any) -> bool:
    return str(value).strip() in {"1", "True", "true"}


# 分红字段的候选名（服务端定义，客户端包内不可查 → 逐项匹配 + 实机探测校验）
_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "ex_date": ("dividOperateDate", "operateDate", "dividExDate"),
    "stock_ratio": ("dividStocksPs", "dividStockPs", "dividStocksPs"),
    "reserve_ratio": ("dividReserveToStockPs", "dividReserveToStockPs"),
    "cash_per_share": ("dividCashPsBeforeTax", "dividCashPsAfterTax"),
}


def _first_present(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


# 名单类接口的代码字段候选名（服务端定义，同样未经验证）
_SYMBOL_KEYS: tuple[str, ...] = ("code", "symbol", "code_name", "stock_code")


def parse_symbol_set(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    """把名单类查询结果转成代码集合。

    ⚠️ 字段名未知（服务端定义）→ 用候选名逐个尝试。全部未命中时返回空集，
    但调用方可用 :meth:`BaostockProvider.probe_list_fields` 打印真实字段。
    """
    out: set[str] = set()
    for row in rows:
        value = _first_present(row, _SYMBOL_KEYS)
        if value is not None:
            out.add(str(value).strip())
    return out


def resolve_baostock_func(name: str):
    """解析 baostock 函数。

    ``query_suspended_stocks`` / ``query_stocks_in_risk`` 等**未在
    ``baostock/__init__.py`` 导出**（实测 0.9.3 会 ``AttributeError``），
    但定义在 ``baostock.security.sectorinfo`` 里。故先试顶层，再试子模块。
    """
    try:
        import baostock as bs  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ProviderError("未安装 baostock；请执行 pip install baostock pandas") from exc

    func = getattr(bs, name, None)
    if func is not None:
        return func

    try:
        from baostock.security import sectorinfo  # noqa: PLC0415

        func = getattr(sectorinfo, name, None)
    except ImportError:  # pragma: no cover
        func = None

    if func is None:
        raise ProviderError(
            f"baostock 未提供 {name}（顶层与 security.sectorinfo 均未找到）；"
            "单标的场景可直接用 Bar.is_suspended / Bar.is_st，无需该接口"
        )
    return func


def row_to_bar(row: Mapping[str, Any], freq: str = "1d") -> Bar | None:
    """把一行 baostock 日线转成 :class:`Bar`。

    返回 ``None`` 的情形（该日无可用行情，调用方应跳过）：

    * ``open/high/low/close`` 任一为空 —— 停牌或无数据日
    * ``preclose`` 为空或 ≤ 0 —— **涨跌停价无法计算**。此时宁可跳过该日，
      也不要用 ``prev_close=0`` 静默关闭涨跌停检查（那会让回测虚高）。
    """
    o = _num(row.get("open"))
    h = _num(row.get("high"))
    lo = _num(row.get("low"))
    c = _num(row.get("close"))
    prev = _num(row.get("preclose"))
    if o is None or h is None or lo is None or c is None:
        return None
    if prev is None or prev <= 0:
        return None

    raw_date = row.get("date")
    if raw_date is None:
        return None
    if isinstance(raw_date, datetime):
        d = raw_date.date()
    elif isinstance(raw_date, date):
        d = raw_date
    else:
        d = date.fromisoformat(str(raw_date).strip()[:10])

    return Bar(
        symbol=str(row.get("code", "")).strip(),
        dt=datetime.combine(d, _MARKET_CLOSE),
        open=o,
        high=h,
        low=lo,
        close=c,
        prev_close=prev,
        volume=_num(row.get("volume")) or 0.0,
        amount=_num(row.get("amount")) or 0.0,
        freq=freq,
        adjust_flag=parse_adjust_flag(row.get("adjustflag")),
        is_suspended=str(row.get("tradestatus", "1")).strip() == "0",
        is_st=_flag(row.get("isST")),
        pe_ttm=_num(row.get("peTTM")),
        pb_mrq=_num(row.get("pbMRQ")),
        ps_ttm=_num(row.get("psTTM")),
        pcf_ncf_ttm=_num(row.get("pcfNcfTTM")),
        turnover=_num(row.get("turn")),
    )


def parse_trade_dates(rows: Sequence[Mapping[str, Any]]) -> list[date]:
    """``query_trade_dates`` 结果 → 交易日列表（仅 ``is_trading_day == "1"``）。"""
    out: list[date] = []
    for row in rows:
        if str(row.get("is_trading_day", "0")).strip() != "1":
            continue
        raw = row.get("calendar_date")
        if raw is None:
            continue
        out.append(date.fromisoformat(str(raw).strip()[:10]))
    return sorted(set(out))


def parse_stock_basic(row: Mapping[str, Any]) -> Instrument:
    """``query_stock_basic`` 单行 → :class:`Instrument`。"""
    symbol = str(row.get("code", "")).strip()
    if not symbol:
        raise ProviderError(f"query_stock_basic 行缺少 code: {row!r}")
    raw_ipo = row.get("ipoDate")
    try:
        list_date = date.fromisoformat(str(raw_ipo).strip()[:10]) if raw_ipo else date(1990, 1, 1)
    except ValueError:
        list_date = date(1990, 1, 1)
    return Instrument(
        symbol=symbol,
        name=str(row.get("code_name", "")).strip() or symbol,
        board=infer_board(symbol),
        list_date=list_date,
    )



def _parse_date(value: Any) -> date | None:
    if value is None or str(value).strip() in ("", "None"):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def parse_forecast_row(row: Mapping[str, Any], symbol: str = "") -> Forecast | None:
    """解析 ``query_forecast_report`` 的一行。

    变动幅度：给上下限时取**中点**，仅给单边时取该边，都没有则 ``None``。
    """
    pub = _parse_date(row.get("profitForcastExpPubDate"))
    stat = _parse_date(row.get("profitForcastExpStatDate"))
    if pub is None or stat is None:
        return None
    up = _num(row.get("profitForcastChgPctUp"))
    down = _num(row.get("profitForcastChgPctDwn"))
    if up is not None and down is not None:
        mid = (up + down) / 2.0
    else:
        mid = up if up is not None else down
    return Forecast(
        symbol=symbol or str(row.get("code", "")).strip(),
        pub_date=pub,
        stat_date=stat,
        source="forecast",
        kind=str(row.get("profitForcastType", "")).strip(),
        chg_pct_mid=mid,
        chg_pct_up=up,
        chg_pct_down=down,
    )


def parse_express_row(row: Mapping[str, Any], symbol: str = "") -> Forecast | None:
    """解析 ``query_performance_express_report`` 的一行。

    快报无"类型"，用 ``performanceExpressEPSChgPct``（EPS 同比变动，小数）作为幅度。
    """
    pub = _parse_date(row.get("performanceExpPubDate"))
    stat = _parse_date(row.get("performanceExpStatDate"))
    if pub is None or stat is None:
        return None
    chg = _num(row.get("performanceExpressEPSChgPct"))
    pct = chg * 100.0 if chg is not None else None
    return Forecast(
        symbol=symbol or str(row.get("code", "")).strip(),
        pub_date=pub,
        stat_date=stat,
        source="express",
        kind="快报",
        chg_pct_mid=pct,
        chg_pct_up=pct,
        chg_pct_down=pct,
    )


def parse_dividend_row(row: Mapping[str, Any], symbol: str = "") -> CorporateAction | None:
    """``query_dividend_data`` 单行 → :class:`CorporateAction`。

    以**除权除息日**为 ``ex_date``；``share_ratio`` = 每股送股 + 每股转增；
    ``cash_per_share`` = 每股税前现金。

    ⚠️ **字段名未经实机验证**。baostock 的字段由服务端返回，客户端包内查不到
    定义；唯一被官方 demo 证实的是 ``dividOperateDate``（见
    ``demo_daily_adjust_factor_data.py``）。因此本函数用**候选名列表**逐项匹配，
    并可用 :meth:`BaostockProvider.probe_dividend_fields` 打印真实字段名做校验。
    """
    ex_raw = _first_present(row, _FIELD_CANDIDATES["ex_date"])
    if not ex_raw:
        return None
    try:
        ex_date = date.fromisoformat(str(ex_raw).strip()[:10])
    except ValueError:
        return None

    stock = _num(_first_present(row, _FIELD_CANDIDATES["stock_ratio"])) or 0.0
    reserve = _num(_first_present(row, _FIELD_CANDIDATES["reserve_ratio"])) or 0.0
    cash = _num(_first_present(row, _FIELD_CANDIDATES["cash_per_share"])) or 0.0
    if stock == 0.0 and reserve == 0.0 and cash == 0.0:
        return None
    return CorporateAction(
        symbol=str(row.get("code", symbol)).strip() or symbol,
        ex_date=ex_date,
        share_ratio=stock + reserve,
        cash_per_share=cash,
    )


# ==================== 适配器 ====================


@register("baostock")
class BaostockProvider(DataProvider):
    """baostock 数据源。需要 ``login()`` 会话，用上下文管理器最省心。

    >>> with BaostockProvider() as p:            # doctest: +SKIP
    ...     bars = p.get_bars("sh.601872", "1d", date(2024, 1, 1), date(2024, 3, 1))
    """

    name = "baostock"

    def __init__(self, *, autologin: bool = False, verbose: bool = False) -> None:
        self._logged_in = False
        self.verbose = verbose
        if autologin:
            self.connect()

    # ---- 会话 ----

    @staticmethod
    def _bs():
        try:
            import baostock as bs  # noqa: PLC0415  (惰性导入，便于无网络单测)
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "未安装 baostock；请执行 pip install baostock pandas"
            ) from exc
        return bs

    def connect(self) -> None:
        if self._logged_in:
            return
        lg = self._bs().login()
        if getattr(lg, "error_code", "1") != "0":
            raise ProviderError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
        self._logged_in = True

    def disconnect(self) -> None:
        if not self._logged_in:
            return
        self._bs().logout()
        self._logged_in = False

    # ---- 内部：把 ResultSet 拉成 dict 列表 ----

    @staticmethod
    def _drain(rs: Any) -> list[dict[str, Any]]:
        if getattr(rs, "error_code", "1") != "0":
            raise ProviderError(f"baostock 查询失败: {rs.error_code} {rs.error_msg}")
        rows: list[dict[str, Any]] = []
        while rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        return rows

    # ---- DataProvider ----

    def get_bars(
        self,
        symbol: str,
        freq: str = "1d",
        start: date | None = None,
        end: date | None = None,
        adjust: AdjustFlag = AdjustFlag.NONE,
    ) -> list[Bar]:
        self.connect()
        rs = self._bs().query_history_k_data_plus(
            symbol,
            DAILY_FIELDS,
            start_date=start.isoformat() if start else None,
            end_date=end.isoformat() if end else None,
            frequency="d",
            adjustflag=adjust_flag_to_baostock(adjust),
        )
        bars: list[Bar] = []
        for row in self._drain(rs):
            bar = row_to_bar(row, freq)
            if bar is not None:
                bars.append(bar)
        return bars

    def get_calendar(self, start: date, end: date) -> list[date]:
        self.connect()
        rs = self._bs().query_trade_dates(
            start_date=start.isoformat(), end_date=end.isoformat()
        )
        return parse_trade_dates(self._drain(rs))

    def get_instrument(self, symbol: str) -> Instrument:
        self.connect()
        rs = self._bs().query_stock_basic(code=symbol)
        rows = self._drain(rs)
        if not rows:
            raise ProviderError(f"未找到标的: {symbol}")
        return parse_stock_basic(rows[0])

    def get_suspended(self, d: date) -> set[str]:
        """暂停上市股票列表。

        ⚠️ ``query_suspended_stocks`` **未在 baostock 顶层导出**（0.9.3 实测），
        经 :func:`resolve_baostock_func` 从子模块解析。字段名未知，用候选名匹配。
        **单标的场景不需要此接口**——``Bar.is_suspended`` 已由日线自带。
        """
        self.connect()
        rs = resolve_baostock_func("query_suspended_stocks")(date=d.isoformat())
        return parse_symbol_set(self._drain(rs))

    def get_risk_warning(self, d: date) -> set[str]:
        """风险警示板（ST）列表。

        ⚠️ **服务端实测不支持**：``query_stocks_in_risk`` 虽然定义在
        ``baostock.security.sectorinfo``，但调用返回
        ``10004020 错误的消息类型``（2026-09-17 实测）。

        **正确做法：单标的场景用 ``Bar.is_st``**——日线自带 ``isST`` 列，
        已由 :func:`row_to_bar` 映射，无需额外查询（也避免了 2500 天 × 1 次查询）。
        此接口保留仅为多标的场景的兼容占位。
        """
        self.connect()
        try:
            rs = resolve_baostock_func("query_stocks_in_risk")(date=d.isoformat())
            return parse_symbol_set(self._drain(rs))
        except ProviderError as exc:
            raise ProviderError(
                f"baostock 服务端不支持 query_stocks_in_risk（{exc}）；"
                "ST 判定请改用 Bar.is_st（日线自带 isST 列）"
            ) from exc

    def probe_list_fields(self, which: str, d: date) -> list[str]:
        """**实机探测**名单类接口的真实字段名。

        ``which`` 取 ``"suspended"`` 或 ``"risk"``。
        """
        self.connect()
        name = (
            "query_suspended_stocks" if which == "suspended" else "query_stocks_in_risk"
        )
        rs = resolve_baostock_func(name)(date=d.isoformat())
        fields = list(rs.fields)
        print(f"[probe] {name} fields = {fields}")
        return fields

    def get_dividends(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateAction]:
        """``query_dividend_data`` 按年查询，故按年份循环再过滤区间。

        ``yearType="operate"`` = **除权除息年份**（不是预案公告年份）——因为我们
        按 ``ex_date`` 过滤区间，用 ``"report"`` 会漏掉跨年的除权事件。
        """
        self.connect()
        out: list[CorporateAction] = []
        for year in range(start.year, end.year + 1):
            rs = self._bs().query_dividend_data(
                code=symbol, year=str(year), yearType="operate"
            )
            for row in self._drain(rs):
                ca = parse_dividend_row(row, symbol)
                if ca is not None and start <= ca.ex_date <= end:
                    out.append(ca)
        out.sort(key=lambda c: c.ex_date)
        return out

    def get_forecasts(
        self, symbol: str, start: date, end: date
    ) -> list[Forecast]:
        """业绩预告 + 业绩快报。

        **按 ``pub_date``（公告日）过滤区间**，不是 ``stat_date``（报告期）——
        后者会导致前视（提前知道未公布的业绩）。

        两个接口字段不同，统一成 :class:`Forecast`：

        * ``query_forecast_report``：``profitForcastExpPubDate`` / ``...StatDate`` /
          ``profitForcastType`` / ``profitForcastChgPctUp`` / ``...Dwn``
        * ``query_performance_express_report``：``performanceExpPubDate`` /
          ``...StatDate`` / ``performanceExpressEPSChgPct`` / ``...ROEWa`` /
          ``...GRYOY`` / ``...OPYOY``
        """
        self.connect()
        out: list[Forecast] = []

        rs = self._bs().query_forecast_report(
            code=symbol, start_date=start.isoformat(), end_date=end.isoformat()
        )
        for row in self._drain(rs):
            f = parse_forecast_row(row, symbol)
            if f is not None and start <= f.pub_date <= end:
                out.append(f)

        rs2 = self._bs().query_performance_express_report(
            code=symbol, start_date=start.isoformat(), end_date=end.isoformat()
        )
        for row in self._drain(rs2):
            f = parse_express_row(row, symbol)
            if f is not None and start <= f.pub_date <= end:
                out.append(f)

        out.sort(key=lambda f: f.pub_date)
        return out

    def probe_dividend_fields(self, symbol: str, year: str) -> list[str]:
        """**实机探测**：打印 ``query_dividend_data`` 的真实字段名，用于校验
        :data:`_FIELD_CANDIDATES` 的候选名是否与实际一致。

        >>> with BaostockProvider() as p:            # doctest: +SKIP
        ...     print(p.probe_dividend_fields("sh.601872", "2023"))
        """
        self.connect()
        rs = self._bs().query_dividend_data(
            code=symbol, year=str(year), yearType="operate"
        )
        fields = list(rs.fields)
        print(f"[probe] query_dividend_data fields = {fields}")
        return fields
