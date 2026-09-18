"""``PITGuard`` —— 时点守卫，防前视偏差的核心组件。

**这是本项目最容易被低估、代价最高的组件。** 前六轮验证的失败很可能部分源于
此，而非因子本身。

设计：为每类数据定义「可见时点」（``data_date -> datetime``），任何数据读取
必须声明 ``as_of``，晚于 ``as_of`` 的数据不可见。

默认滞后规则（保守）：

===========================  ==============================  ==============
数据类型                      可见时点                         依据
===========================  ==============================  ==============
``daily_bar``                 ``T 15:00``                     收盘价确定时刻
``calendar``                  立即                             交易所提前公布
``board_membership``          ``T 09:00``                     盘前公告
``suspended``                 ``T 09:00``                     盘前公告
``risk_warning``              ``T 09:00``                     盘前公告
``dividend``                  ``T 09:00``                     除权日盘前
``adjust_factor``             ``T 09:00``                     除权日盘前
``express_report``            公告日 ``+1 天 09:00``            保守 +1 天
``macro``                     统计期 ``+20 天 09:00``          统计局发布节奏
``shipping_index``            发布日 ``+2 天 09:00``           二手转载，保守
``financial_report``          报告期末 + 法定滞后              见下表
===========================  ==============================  ==============

财务报告法定滞后（取上限，保守）：一季报/三季报 ``+30`` 天，半年报 ``+60`` 天，
年报 ``+120`` 天。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from do_modle.objects import Bar

__all__ = [
    "PITGuard",
    "LookaheadError",
    "DEFAULT_LAG_RULES",
    "FINANCIAL_LAG_DAYS",
    "KINDS",
]

# ---- 数据类型常量（用常量避免拼写错误静默通过） ----

KIND_CALENDAR = "calendar"
KIND_DAILY_BAR = "daily_bar"
KIND_BOARD_MEMBERSHIP = "board_membership"
KIND_SUSPENDED = "suspended"
KIND_RISK_WARNING = "risk_warning"
KIND_DIVIDEND = "dividend"
KIND_ADJUST_FACTOR = "adjust_factor"
KIND_EXPRESS_REPORT = "express_report"
KIND_MACRO = "macro"
KIND_SHIPPING_INDEX = "shipping_index"
KIND_FINANCIAL_REPORT = "financial_report"

KINDS: frozenset[str] = frozenset(
    {
        KIND_CALENDAR,
        KIND_DAILY_BAR,
        KIND_BOARD_MEMBERSHIP,
        KIND_SUSPENDED,
        KIND_RISK_WARNING,
        KIND_DIVIDEND,
        KIND_ADJUST_FACTOR,
        KIND_EXPRESS_REPORT,
        KIND_MACRO,
        KIND_SHIPPING_INDEX,
        KIND_FINANCIAL_REPORT,
    }
)

FINANCIAL_LAG_DAYS: dict[str, int] = {"q1": 30, "q3": 30, "h1": 60, "annual": 120}

_DAY_START = time(0, 0)
_MARKET_CLOSE = time(15, 0)
_PRE_OPEN = time(9, 0)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


DEFAULT_LAG_RULES: dict[str, Callable[[date], datetime]] = {
    KIND_CALENDAR: lambda d: _at(d, _DAY_START),
    KIND_DAILY_BAR: lambda d: _at(d, _MARKET_CLOSE),
    KIND_BOARD_MEMBERSHIP: lambda d: _at(d, _PRE_OPEN),
    KIND_SUSPENDED: lambda d: _at(d, _PRE_OPEN),
    KIND_RISK_WARNING: lambda d: _at(d, _PRE_OPEN),
    KIND_DIVIDEND: lambda d: _at(d, _PRE_OPEN),
    KIND_ADJUST_FACTOR: lambda d: _at(d, _PRE_OPEN),
    KIND_EXPRESS_REPORT: lambda d: _at(d + timedelta(days=1), _PRE_OPEN),
    KIND_MACRO: lambda d: _at(d + timedelta(days=20), _PRE_OPEN),
    KIND_SHIPPING_INDEX: lambda d: _at(d + timedelta(days=2), _PRE_OPEN),
}


class LookaheadError(AssertionError):
    """发现未来数据。开发期直接抛异常，而不是静默过滤。"""


def _coerce_date(value: Any) -> date:
    """把 ``date`` / ``datetime`` / ``"YYYY-MM-DD"`` 统一成 ``date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"无法解析为日期: {value!r}")


class PITGuard:
    """时点守卫。

    ``strict=True``（默认）时 :meth:`assert_no_lookahead` 会在发现未来数据时
    抛 :class:`LookaheadError`；:meth:`filter` 始终静默丢弃未来数据。
    """

    def __init__(
        self,
        lag_rules: Mapping[str, Callable[[date], datetime]] | None = None,
        *,
        financial_lag_days: Mapping[str, int] | None = None,
        strict: bool = True,
    ) -> None:
        rules = dict(DEFAULT_LAG_RULES)
        if lag_rules:
            rules.update(lag_rules)
        self.lag_rules = rules
        self.financial_lag_days = dict(financial_lag_days or FINANCIAL_LAG_DAYS)
        self.strict = strict

    # ---- 可见时点 ----

    def visible_from(self, data_date: date | datetime | str, kind: str) -> datetime:
        """返回某条数据最早可见的时刻。"""
        if kind == KIND_FINANCIAL_REPORT:
            raise ValueError(
                "financial_report 需要报告类型：请用 financial_visible_from(period_end, report_type)"
            )
        if kind not in self.lag_rules:
            raise KeyError(f"未知数据类型: {kind!r}；已登记: {sorted(self.lag_rules)}")
        return self.lag_rules[kind](_coerce_date(data_date))

    def visible(
        self, data_date: date | datetime | str, as_of: datetime, kind: str
    ) -> bool:
        """该数据在 ``as_of`` 时刻是否可见。"""
        return self.visible_from(data_date, kind) <= as_of

    def financial_visible_from(self, period_end: date | str, report_type: str) -> datetime:
        """财务报告可见时点 = 报告期末 + 法定滞后。"""
        if report_type not in self.financial_lag_days:
            raise KeyError(
                f"未知报告类型: {report_type!r}；已登记: {sorted(self.financial_lag_days)}"
            )
        end = _coerce_date(period_end)
        return _at(end + timedelta(days=self.financial_lag_days[report_type]), _PRE_OPEN)

    def financial_visible(
        self, period_end: date | str, report_type: str, as_of: datetime
    ) -> bool:
        return self.financial_visible_from(period_end, report_type) <= as_of

    # ---- 行级过滤 / 断言 ----

    def filter(
        self,
        rows: Iterable[Mapping[str, Any]],
        as_of: datetime,
        kind: str,
        *,
        date_key: str = "date",
    ) -> list[Mapping[str, Any]]:
        """丢弃 ``as_of`` 之后才可见的行。"""
        out: list[Mapping[str, Any]] = []
        for row in rows:
            if date_key not in row:
                raise KeyError(f"行缺少日期字段 {date_key!r}: {row!r}")
            if self.visible(row[date_key], as_of, kind):
                out.append(row)
        return out

    def assert_no_lookahead(
        self,
        rows: Iterable[Mapping[str, Any]],
        as_of: datetime,
        kind: str,
        *,
        date_key: str = "date",
    ) -> None:
        """发现未来数据即抛 :class:`LookaheadError`（而非静默过滤）。"""
        violations: list[Any] = []
        for row in rows:
            if date_key not in row:
                raise KeyError(f"行缺少日期字段 {date_key!r}: {row!r}")
            if not self.visible(row[date_key], as_of, kind):
                violations.append(row[date_key])
        if violations:
            raise LookaheadError(
                f"发现 {len(violations)} 条未来数据（kind={kind}, as_of={as_of}）："
                f"最早 {min(violations)}，最晚 {max(violations)}"
            )

    # ---- Bar 专用 ----

    def visible_bars(self, bars: Sequence[Bar], as_of: datetime) -> list[Bar]:
        """按日线可见时点过滤 Bar。"""
        return [b for b in bars if self.visible(b.dt.date(), as_of, KIND_DAILY_BAR)]

    def assert_no_lookahead_bars(self, bars: Sequence[Bar], as_of: datetime) -> None:
        """Bar 版无前视断言。"""
        bad = [b.dt for b in bars if not self.visible(b.dt.date(), as_of, KIND_DAILY_BAR)]
        if bad:
            raise LookaheadError(
                f"发现 {len(bad)} 根未来 Bar（as_of={as_of}）：最早 {min(bad)}，最晚 {max(bad)}"
            )
