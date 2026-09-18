"""``data/pit.py`` 单测 —— 防前视的核心组件。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from do_modle.data.pit import (
    DEFAULT_LAG_RULES,
    FINANCIAL_LAG_DAYS,
    KIND_DAILY_BAR,
    KIND_DIVIDEND,
    KIND_FINANCIAL_REPORT,
    KIND_SHIPPING_INDEX,
    LookaheadError,
    PITGuard,
)
from tests._helpers import D1, D2, make_bar

GUARD = PITGuard()


# ==================== 可见时点 ====================

def test_daily_bar_visible_at_market_close() -> None:
    assert GUARD.visible_from(D1, KIND_DAILY_BAR) == datetime(2026, 9, 15, 15, 0)


def test_daily_bar_not_visible_before_close() -> None:
    """当日 14:00 时，当日收盘价还不存在。"""
    assert not GUARD.visible(D1, datetime(2026, 9, 15, 14, 0), KIND_DAILY_BAR)
    assert GUARD.visible(D1, datetime(2026, 9, 15, 15, 0), KIND_DAILY_BAR)


def test_dividend_visible_from_pre_open() -> None:
    assert GUARD.visible_from(D1, KIND_DIVIDEND) == datetime(2026, 9, 15, 9, 0)


def test_shipping_index_has_two_day_lag() -> None:
    assert GUARD.visible_from(D1, KIND_SHIPPING_INDEX) == datetime(2026, 9, 17, 9, 0)


def test_calendar_immediately_visible() -> None:
    assert GUARD.visible_from(D1, "calendar") == datetime(2026, 9, 15, 0, 0)


@pytest.mark.parametrize(
    ("report_type", "period_end", "expected"),
    [
        ("q1", date(2025, 3, 31), datetime(2025, 4, 30, 9, 0)),
        ("q3", date(2025, 9, 30), datetime(2025, 10, 30, 9, 0)),
        ("h1", date(2025, 6, 30), datetime(2025, 8, 29, 9, 0)),
        ("annual", date(2025, 12, 31), datetime(2026, 4, 30, 9, 0)),
    ],
)
def test_financial_report_lag(
    report_type: str, period_end: date, expected: datetime
) -> None:
    """报告期末 + 法定滞后（取上限，保守）。"""
    assert GUARD.financial_visible_from(period_end, report_type) == expected


def test_annual_report_not_visible_90_days_after_period_end() -> None:
    """年报法定滞后 4 个月——90 天时仍不可见。"""
    period_end = date(2025, 12, 31)
    assert not GUARD.financial_visible(period_end, "annual", datetime(2026, 3, 31, 9, 0))
    assert GUARD.financial_visible(period_end, "annual", datetime(2026, 5, 1, 9, 0))


def test_financial_report_kind_requires_report_type() -> None:
    with pytest.raises(ValueError, match="report_type"):
        GUARD.visible_from(D1, KIND_FINANCIAL_REPORT)


def test_unknown_report_type_raises() -> None:
    with pytest.raises(KeyError):
        GUARD.financial_visible_from(date(2025, 12, 31), "q2")


# ==================== 错误处理 ====================

def test_unknown_kind_raises_keyerror() -> None:
    """拼错的数据类型必须报错，不能静默放过。"""
    with pytest.raises(KeyError, match="未知数据类型"):
        GUARD.visible_from(D1, "daily_ba")


def test_coerce_date_accepts_str_date_datetime() -> None:
    expected = datetime(2026, 9, 15, 15, 0)
    assert GUARD.visible_from("2026-09-15", KIND_DAILY_BAR) == expected
    assert GUARD.visible_from(date(2026, 9, 15), KIND_DAILY_BAR) == expected
    assert GUARD.visible_from(datetime(2026, 9, 15, 8, 0), KIND_DAILY_BAR) == expected


def test_coerce_date_rejects_garbage() -> None:
    with pytest.raises(TypeError):
        GUARD.visible_from(12345, KIND_DAILY_BAR)  # type: ignore[arg-type]


# ==================== 行级过滤 ====================

def test_filter_drops_future_rows() -> None:
    rows = [{"date": "2026-09-14"}, {"date": "2026-09-15"}, {"date": "2026-09-16"}]
    kept = GUARD.filter(rows, datetime(2026, 9, 15, 15, 0), KIND_DAILY_BAR)
    assert [r["date"] for r in kept] == ["2026-09-14", "2026-09-15"]


def test_filter_missing_date_key_raises() -> None:
    with pytest.raises(KeyError, match="date"):
        GUARD.filter([{"close": 1.0}], datetime(2026, 9, 15), KIND_DAILY_BAR)


def test_filter_custom_date_key() -> None:
    rows = [{"trade_date": "2026-09-16"}]
    kept = GUARD.filter(
        rows, datetime(2026, 9, 15, 15, 0), KIND_DAILY_BAR, date_key="trade_date"
    )
    assert kept == []


# ==================== 无前视断言 ====================

def test_assert_no_lookahead_raises_on_future_data() -> None:
    """开发期必须抛异常，而不是静默过滤。"""
    rows = [{"date": "2026-09-16"}]
    with pytest.raises(LookaheadError, match="未来数据"):
        GUARD.assert_no_lookahead(rows, datetime(2026, 9, 15, 15, 0), KIND_DAILY_BAR)


def test_assert_no_lookahead_passes_for_past_data() -> None:
    rows = [{"date": "2026-09-14"}, {"date": "2026-09-15"}]
    GUARD.assert_no_lookahead(rows, datetime(2026, 9, 15, 15, 0), KIND_DAILY_BAR)


def test_lookahead_error_is_assertion_error() -> None:
    assert issubclass(LookaheadError, AssertionError)


# ==================== Bar 专用 ====================

def test_visible_bars_filters_by_close_time() -> None:
    bars = [make_bar(d=D1), make_bar(d=D2)]
    assert len(GUARD.visible_bars(bars, datetime(2026, 9, 15, 15, 0))) == 1
    assert len(GUARD.visible_bars(bars, datetime(2026, 9, 16, 15, 0))) == 2


def test_assert_no_lookahead_bars_raises() -> None:
    bars = [make_bar(d=D1), make_bar(d=D2)]
    with pytest.raises(LookaheadError, match="未来 Bar"):
        GUARD.assert_no_lookahead_bars(bars, datetime(2026, 9, 15, 15, 0))


def test_assert_no_lookahead_bars_passes_at_close() -> None:
    GUARD.assert_no_lookahead_bars([make_bar(d=D1)], datetime(2026, 9, 15, 15, 0))


# ==================== 可配置性 ====================

def test_custom_lag_rule_overrides_default() -> None:
    guard = PITGuard({KIND_DAILY_BAR: lambda d: datetime.combine(d, datetime.min.time())})
    assert guard.visible_from(D1, KIND_DAILY_BAR) == datetime(2026, 9, 15, 0, 0)


def test_custom_financial_lag() -> None:
    guard = PITGuard(financial_lag_days={"annual": 1})
    assert guard.financial_visible(date(2025, 12, 31), "annual", datetime(2026, 1, 2, 9, 0))


def test_default_lag_rules_cover_expected_kinds() -> None:
    expected = {
        "calendar",
        "daily_bar",
        "board_membership",
        "suspended",
        "risk_warning",
        "dividend",
        "adjust_factor",
        "express_report",
        "macro",
        "shipping_index",
    }
    assert expected <= set(DEFAULT_LAG_RULES)


def test_strict_flag_defaults_true() -> None:
    assert PITGuard().strict is True
    assert PITGuard(strict=False).strict is False
