"""``data/adapters/baostock_src.py`` 单测 —— 全部为**纯解析函数**，不需要网络。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from do_modle.data.adapters.baostock_src import (
    DAILY_FIELDS,
    BaostockProvider,
    adjust_flag_to_baostock,
    infer_board,
    parse_adjust_flag,
    parse_dividend_row,
    parse_stock_basic,
    parse_symbol_set,
    parse_trade_dates,
    resolve_baostock_func,
    row_to_bar,
)
from do_modle.data.provider import PROVIDERS, ProviderError
from do_modle.objects import AdjustFlag, Board

GOOD_ROW = {
    "date": "2026-09-15",
    "code": "sh.601872",
    "open": "20.00",
    "high": "20.40",
    "low": "19.80",
    "close": "20.20",
    "preclose": "20.00",
    "volume": "12345678",
    "amount": "250000000",
    "adjustflag": "3",
    "turn": "0.51",
    "tradestatus": "1",
    "pctChg": "1.0",
    "peTTM": "12.3",
    "pbMRQ": "1.1",
    "psTTM": "1.0",
    "pcfNcfTTM": "8.0",
    "isST": "0",
}


# ==================== 板块推断 ====================

@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("sh.601872", Board.MAIN),  # 招商轮船
        ("sh.600000", Board.MAIN),
        ("sh.603288", Board.MAIN),
        ("sh.605499", Board.MAIN),
        ("sz.000001", Board.MAIN),
        ("sz.001979", Board.MAIN),
        ("sz.002594", Board.MAIN),
        ("sz.003816", Board.MAIN),
        ("sz.300750", Board.GEM),
        ("sz.301029", Board.GEM),
        ("sh.688981", Board.STAR),
        ("bj.832000", Board.BSE),
    ],
)
def test_infer_board(symbol: str, expected: Board) -> None:
    assert infer_board(symbol) is expected


def test_infer_board_is_case_insensitive() -> None:
    assert infer_board("SH.601872") is Board.MAIN


def test_infer_board_unknown_raises() -> None:
    with pytest.raises(ProviderError, match="无法推断板块"):
        infer_board("xx.999999")


# ==================== 复权口径 ====================

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", AdjustFlag.HFQ), ("2", AdjustFlag.QFQ), ("3", AdjustFlag.NONE)],
)
def test_parse_adjust_flag(raw: str, expected: AdjustFlag) -> None:
    assert parse_adjust_flag(raw) is expected


def test_parse_adjust_flag_defaults_to_none() -> None:
    assert parse_adjust_flag(None) is AdjustFlag.NONE


def test_parse_adjust_flag_invalid_raises() -> None:
    with pytest.raises(ProviderError, match="未知 adjustflag"):
        parse_adjust_flag("9")


def test_adjust_flag_roundtrip() -> None:
    for flag in (AdjustFlag.HFQ, AdjustFlag.QFQ, AdjustFlag.NONE):
        assert parse_adjust_flag(adjust_flag_to_baostock(flag)) is flag


def test_adjust_flag_to_baostock_invalid_raises() -> None:
    with pytest.raises(ProviderError):
        adjust_flag_to_baostock("bogus")  # type: ignore[arg-type]


# ==================== row_to_bar ====================

def test_row_to_bar_happy_path() -> None:
    bar = row_to_bar(GOOD_ROW)
    assert bar is not None
    assert bar.symbol == "sh.601872"
    assert bar.dt == datetime(2026, 9, 15, 15, 0)
    assert bar.open == pytest.approx(20.0)
    assert bar.high == pytest.approx(20.4)
    assert bar.low == pytest.approx(19.8)
    assert bar.close == pytest.approx(20.2)
    assert bar.prev_close == pytest.approx(20.0)
    assert bar.volume == pytest.approx(12_345_678)
    assert bar.adjust_flag is AdjustFlag.NONE
    assert not bar.is_suspended
    assert not bar.is_st


def test_row_to_bar_marks_st() -> None:
    bar = row_to_bar({**GOOD_ROW, "isST": "1"})
    assert bar is not None
    assert bar.is_st is True


def test_row_to_bar_marks_suspended() -> None:
    bar = row_to_bar({**GOOD_ROW, "tradestatus": "0"})
    assert bar is not None
    assert bar.is_suspended is True


def test_row_to_bar_reads_adjust_flag() -> None:
    assert row_to_bar({**GOOD_ROW, "adjustflag": "1"}).adjust_flag is AdjustFlag.HFQ


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_row_to_bar_returns_none_when_price_missing(field: str) -> None:
    """停牌/无数据日：价格为空 → 跳过该行。"""
    assert row_to_bar({**GOOD_ROW, field: ""}) is None


def test_row_to_bar_returns_none_when_preclose_missing() -> None:
    """**关键**：preclose 缺失则涨跌停价无法计算，宁可跳过该日，
    也不能用 prev_close=0 静默关闭涨跌停检查（那会让回测虚高）。"""
    assert row_to_bar({**GOOD_ROW, "preclose": ""}) is None


def test_row_to_bar_returns_none_when_preclose_non_positive() -> None:
    assert row_to_bar({**GOOD_ROW, "preclose": "0"}) is None


def test_row_to_bar_returns_none_when_date_missing() -> None:
    row = {**GOOD_ROW}
    row.pop("date")
    assert row_to_bar(row) is None


def test_row_to_bar_accepts_date_object() -> None:
    bar = row_to_bar({**GOOD_ROW, "date": date(2026, 9, 15)})
    assert bar is not None
    assert bar.dt.date() == date(2026, 9, 15)


def test_row_to_bar_missing_volume_defaults_zero() -> None:
    bar = row_to_bar({**GOOD_ROW, "volume": ""})
    assert bar is not None
    assert bar.volume == 0.0


def test_daily_fields_contains_critical_columns() -> None:
    """三个决定规则层正确性的字段必须在请求里。"""
    for field in ("preclose", "tradestatus", "isST"):
        assert field in DAILY_FIELDS


# ==================== 交易日历 ====================

def test_parse_trade_dates_filters_non_trading() -> None:
    rows = [
        {"calendar_date": "2026-09-12", "is_trading_day": "0"},
        {"calendar_date": "2026-09-14", "is_trading_day": "1"},
        {"calendar_date": "2026-09-15", "is_trading_day": "1"},
    ]
    assert parse_trade_dates(rows) == [date(2026, 9, 14), date(2026, 9, 15)]


def test_parse_trade_dates_dedupes_and_sorts() -> None:
    rows = [
        {"calendar_date": "2026-09-15", "is_trading_day": "1"},
        {"calendar_date": "2026-09-14", "is_trading_day": "1"},
        {"calendar_date": "2026-09-15", "is_trading_day": "1"},
    ]
    assert parse_trade_dates(rows) == [date(2026, 9, 14), date(2026, 9, 15)]


def test_parse_trade_dates_empty() -> None:
    assert parse_trade_dates([]) == []


# ==================== 标的元数据 ====================

def test_parse_stock_basic() -> None:
    inst = parse_stock_basic(
        {"code": "sh.601872", "code_name": "招商轮船", "ipoDate": "2006-12-01"}
    )
    assert inst.symbol == "sh.601872"
    assert inst.name == "招商轮船"
    assert inst.board is Board.MAIN
    assert inst.list_date == date(2006, 12, 1)


def test_parse_stock_basic_missing_code_raises() -> None:
    with pytest.raises(ProviderError, match="缺少 code"):
        parse_stock_basic({"code_name": "x"})


def test_parse_stock_basic_tolerates_bad_ipo_date() -> None:
    inst = parse_stock_basic({"code": "sh.601872", "ipoDate": ""})
    assert inst.list_date == date(1990, 1, 1)


# ==================== 分红 ====================

def test_parse_dividend_row_cash_only() -> None:
    ca = parse_dividend_row(
        {"code": "sh.601872", "dividOperateDate": "2026-07-10", "dividCashPsBeforeTax": "0.35"}
    )
    assert ca is not None
    assert ca.ex_date == date(2026, 7, 10)
    assert ca.cash_per_share == pytest.approx(0.35)
    assert ca.share_ratio == pytest.approx(0.0)


def test_parse_dividend_row_share_and_reserve() -> None:
    ca = parse_dividend_row(
        {
            "code": "sh.601872",
            "dividOperateDate": "2026-07-10",
            "dividStocksPs": "0.2",
            "dividReserveToStockPs": "0.1",
        }
    )
    assert ca is not None
    assert ca.share_ratio == pytest.approx(0.3)  # 送 0.2 + 转增 0.1


def test_parse_dividend_row_without_ex_date_returns_none() -> None:
    assert parse_dividend_row({"code": "sh.601872", "dividCashPsBeforeTax": "0.35"}) is None


def test_parse_dividend_row_all_zero_returns_none() -> None:
    assert (
        parse_dividend_row({"code": "sh.601872", "dividOperateDate": "2026-07-10"}) is None
    )


def test_parse_dividend_row_bad_date_returns_none() -> None:
    assert (
        parse_dividend_row(
            {"code": "x", "dividOperateDate": "not-a-date", "dividCashPsBeforeTax": "1"}
        )
        is None
    )


# ==================== 适配器注册与惰性导入 ====================

def test_baostock_provider_registered() -> None:
    assert "baostock" in PROVIDERS
    assert PROVIDERS["baostock"] is BaostockProvider


def test_module_imports_without_baostock() -> None:
    """纯解析函数不依赖 baostock 安装；类也只在 connect 时惰性导入。"""
    p = BaostockProvider()
    assert p._logged_in is False


def test_disconnect_without_login_is_noop() -> None:
    BaostockProvider().disconnect()


# ==================== 名单类接口（未导出的函数解析） ====================

def test_parse_symbol_set_from_code_key() -> None:
    rows = [{"code": "sh.601872"}, {"code": "sz.000001"}]
    assert parse_symbol_set(rows) == {"sh.601872", "sz.000001"}


def test_parse_symbol_set_falls_back_to_other_keys() -> None:
    """字段名未验证 → 候选名逐个尝试。"""
    assert parse_symbol_set([{"symbol": "sh.600026"}]) == {"sh.600026"}
    assert parse_symbol_set([{"code_name": "招商轮船"}]) == {"招商轮船"}


def test_parse_symbol_set_unknown_fields_returns_empty() -> None:
    assert parse_symbol_set([{"unexpected": "x"}]) == set()


def test_parse_symbol_set_empty() -> None:
    assert parse_symbol_set([]) == set()


def test_resolve_baostock_func_finds_top_level_export() -> None:
    """query_history_k_data_plus 是顶层导出的。"""
    pytest.importorskip("baostock")
    assert callable(resolve_baostock_func("query_history_k_data_plus"))


def test_resolve_baostock_func_finds_unexported_submodule_function() -> None:
    """``query_suspended_stocks`` 未在顶层导出（0.9.3 实测 AttributeError），
    但定义在 ``baostock.security.sectorinfo`` —— 解析器必须能找到它。"""
    pytest.importorskip("baostock")
    assert callable(resolve_baostock_func("query_suspended_stocks"))
    assert callable(resolve_baostock_func("query_stocks_in_risk"))


def test_resolve_baostock_func_unknown_raises() -> None:
    pytest.importorskip("baostock")
    with pytest.raises(ProviderError, match="未提供"):
        resolve_baostock_func("query_definitely_not_a_real_function")


def test_resolve_baostock_func_without_baostock_gives_clear_error(monkeypatch) -> None:
    """未装 baostock 时应给出可操作的报错，而不是裸 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "baostock" or name.startswith("baostock."):
            raise ImportError("simulated missing baostock")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ProviderError, match="未安装 baostock"):
        resolve_baostock_func("query_history_k_data_plus")
