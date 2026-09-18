"""``rules/tradability.py`` 单测 —— 涨跌停 / 停牌 / 一字板 / 流动性 / 手数。

重点覆盖**涨跌停的非对称性**：一字板涨停时卖单可成交、买单不可。
"""

from __future__ import annotations

from datetime import date

import pytest

from do_modle.objects import Board, RejectReason, Side, Verdict
from do_modle.rules.tradability import LimitRuleTable, TradabilityGate
from tests._helpers import (
    BSE_INSTRUMENT,
    GEM_INSTRUMENT,
    MAIN_INSTRUMENT,
    STAR_INSTRUMENT,
    make_bar,
    make_instrument,
)

TABLE = LimitRuleTable()


def _gate(volume_limit_pct: float = 0.10) -> TradabilityGate:
    return TradabilityGate(TABLE, volume_limit_pct=volume_limit_pct)


# ================= 涨跌幅比例表（时间感知） =================

def test_main_board_ratio_10pct() -> None:
    assert TABLE.ratio(MAIN_INSTRUMENT, date(2026, 9, 15)) == pytest.approx(0.10)


def test_main_board_st_ratio_5pct() -> None:
    assert TABLE.ratio(MAIN_INSTRUMENT, date(2026, 9, 15), is_st=True) == pytest.approx(0.05)


def test_gem_ratio_20pct_after_reform() -> None:
    assert TABLE.ratio(GEM_INSTRUMENT, date(2021, 6, 1)) == pytest.approx(0.20)


def test_gem_ratio_10pct_before_reform() -> None:
    """创业板注册制（2020-08-24）之前是 10%。"""
    assert TABLE.ratio(GEM_INSTRUMENT, date(2019, 6, 1)) == pytest.approx(0.10)


def test_gem_st_still_20pct_after_reform() -> None:
    """注册制后创业板 ST 仍是 20%，不是 5%。"""
    assert TABLE.ratio(GEM_INSTRUMENT, date(2021, 6, 1), is_st=True) == pytest.approx(0.20)


def test_star_ratio_20pct() -> None:
    assert TABLE.ratio(STAR_INSTRUMENT, date(2021, 6, 1)) == pytest.approx(0.20)


def test_bse_ratio_30pct() -> None:
    assert TABLE.ratio(BSE_INSTRUMENT, date(2022, 6, 1)) == pytest.approx(0.30)


def test_new_listing_window_no_limit_after_2023() -> None:
    """2023-02-17 全面注册制后，主板新股前 5 个交易日不设涨跌幅。"""
    assert TABLE.ratio(MAIN_INSTRUMENT, date(2024, 5, 1), listing_trading_days=2) is None


def test_new_listing_window_not_applied_before_2023() -> None:
    assert TABLE.ratio(MAIN_INSTRUMENT, date(2022, 5, 1), listing_trading_days=2) == pytest.approx(0.10)


def test_new_listing_window_expires_after_5_days() -> None:
    assert TABLE.ratio(MAIN_INSTRUMENT, date(2024, 5, 1), listing_trading_days=5) == pytest.approx(0.10)


# ================= 涨跌停价计算 =================

def test_limit_prices_round_to_tick() -> None:
    up, down = TABLE.limit_prices(MAIN_INSTRUMENT, 10.0, date(2026, 9, 15))
    assert (up, down) == (11.0, 9.0)


def test_limit_prices_round_half_up() -> None:
    """10.03 * 1.1 = 11.033 -> 11.03（不是 11.04，也不是 11.033）。"""
    up, down = TABLE.limit_prices(MAIN_INSTRUMENT, 10.03, date(2026, 9, 15))
    assert up == pytest.approx(11.03)
    assert down == pytest.approx(9.03)


def test_limit_prices_none_when_no_limit() -> None:
    up, down = TABLE.limit_prices(
        MAIN_INSTRUMENT, 10.0, date(2024, 5, 1), listing_trading_days=1
    )
    assert up is None and down is None


# ================= 涨跌停判定（非对称） =================

def test_buy_rejected_at_limit_up() -> None:
    bar = make_bar(open_=11.0, prev_close=10.0)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.LIMIT_UP.value


def test_sell_allowed_at_limit_up() -> None:
    """涨停一字板时卖单可以成交（买盘排队）——非对称性的关键测试。"""
    bar = make_bar(open_=11.0, prev_close=10.0)
    verdict = _gate().check(bar, Side.SELL, 100, instrument=MAIN_INSTRUMENT)
    assert verdict.passed


def test_sell_rejected_at_limit_down() -> None:
    bar = make_bar(open_=9.0, prev_close=10.0)
    verdict = _gate().check(bar, Side.SELL, 100, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.LIMIT_DOWN.value


def test_buy_allowed_at_limit_down() -> None:
    bar = make_bar(open_=9.0, prev_close=10.0)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert verdict.passed


def test_bar_supplied_limit_prices_take_precedence() -> None:
    """Bar 自带涨跌停价时以它为准（数据源可能已算好）。"""
    bar = make_bar(open_=11.0, prev_close=10.0, limit_up=11.5, limit_down=8.5)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert verdict.passed  # 11.0 < 11.5


# ================= 一字板 =================

def test_one_word_board_not_at_limit_rejects_both_sides() -> None:
    """一字板且不在涨跌停价位 → 双向流动性枯竭。"""
    bar = make_bar(open_=10.2, high=10.2, low=10.2, prev_close=10.0)
    for side in (Side.BUY, Side.SELL):
        verdict = _gate().check(bar, side, 100, instrument=MAIN_INSTRUMENT)
        assert not verdict.passed
        assert verdict.reason == RejectReason.ONE_WORD_BOARD.value


def test_one_word_board_at_limit_up_allows_sell() -> None:
    bar = make_bar(open_=11.0, high=11.0, low=11.0, prev_close=10.0)
    assert _gate().check(bar, Side.SELL, 100, instrument=MAIN_INSTRUMENT).passed
    assert not _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT).passed


def test_one_word_board_at_limit_down_allows_buy() -> None:
    bar = make_bar(open_=9.0, high=9.0, low=9.0, prev_close=10.0)
    assert _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT).passed
    assert not _gate().check(bar, Side.SELL, 100, instrument=MAIN_INSTRUMENT).passed


# ================= 停牌 =================

def test_suspended_rejected() -> None:
    bar = make_bar(suspended=True)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.SUSPENDED.value


def test_suspension_takes_priority_over_limit_up() -> None:
    """停牌判定必须排在涨跌停之前。"""
    bar = make_bar(open_=11.0, prev_close=10.0, suspended=True)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert verdict.reason == RejectReason.SUSPENDED.value


# ================= 手数 =================

def test_buy_rejects_non_lot_multiple() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0)
    verdict = _gate().check(bar, Side.BUY, 150, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.LOT_SIZE.value


def test_sell_allows_odd_lot() -> None:
    """卖出允许零股（A股规则）。"""
    bar = make_bar(open_=20.0, prev_close=20.0)
    assert _gate().check(bar, Side.SELL, 150, instrument=MAIN_INSTRUMENT).passed


def test_zero_quantity_rejected() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0)
    assert not _gate().check(bar, Side.BUY, 0, instrument=MAIN_INSTRUMENT).passed


# ================= 流动性 =================

def test_liquidity_clips_order() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0, volume=10_000)
    verdict = _gate(volume_limit_pct=0.10).check(bar, Side.BUY, 5000, instrument=MAIN_INSTRUMENT)
    assert verdict.passed
    assert verdict.allowed_quantity == 1000


def test_liquidity_clip_aligns_to_lot() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0, volume=10_050)
    verdict = _gate(volume_limit_pct=0.10).check(bar, Side.BUY, 5000, instrument=MAIN_INSTRUMENT)
    assert verdict.allowed_quantity == 1000  # floor(1005 / 100) * 100


def test_liquidity_rejects_when_clip_rounds_to_zero() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0, volume=50)
    verdict = _gate(volume_limit_pct=0.10).check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.ILLIQUID.value


def test_no_clip_when_volume_is_zero() -> None:
    """volume=0 视为未提供成交量，跳过流动性裁剪。"""
    bar = make_bar(open_=20.0, prev_close=20.0, volume=0)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert verdict.passed
    assert verdict.allowed_quantity is None


def test_volume_limit_disabled() -> None:
    bar = make_bar(open_=20.0, prev_close=20.0, volume=10)
    verdict = _gate(volume_limit_pct=0.0).check(bar, Side.BUY, 5000, instrument=MAIN_INSTRUMENT)
    assert verdict.passed


# ================= 其他 =================

def test_gate_uses_instrument_tick_size() -> None:
    coarse = make_instrument(tick_size=0.10)
    up, _ = TABLE.limit_prices(coarse, 10.0, date(2026, 9, 15))
    assert up == pytest.approx(11.0)


def test_negative_volume_limit_rejected() -> None:
    with pytest.raises(ValueError):
        TradabilityGate(volume_limit_pct=-0.1)


def test_verdict_helpers() -> None:
    assert Verdict.ok().passed
    assert Verdict.reject(RejectReason.LIMIT_UP).reason == "LIMIT_UP"


def test_all_boards_resolvable() -> None:
    for inst in (MAIN_INSTRUMENT, GEM_INSTRUMENT, STAR_INSTRUMENT, BSE_INSTRUMENT):
        assert TABLE.ratio(inst, date(2026, 9, 15)) > 0


def test_unknown_board_raises() -> None:
    class Fake:
        board = "NOPE"

    with pytest.raises((ValueError, AttributeError)):
        TABLE.ratio(Fake(), date(2026, 9, 15))  # type: ignore[arg-type]


def test_board_enum_membership() -> None:
    assert {b.name for b in Board} == {"MAIN", "GEM", "STAR", "BSE"}


# ================= ST 的逐日来源 =================

def test_bar_is_st_used_when_not_passed() -> None:
    """``is_st`` 省略时取 ``bar.is_st``（数据源日线自带的逐日事实）。

    ST 主板涨跌幅 5%：preclose 10.0 → limit_up 10.5，开盘 10.5 即涨停，买单拒绝。
    """
    bar = make_bar(open_=10.5, prev_close=10.0, is_st=True)
    verdict = _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT)
    assert not verdict.passed
    assert verdict.reason == RejectReason.LIMIT_UP.value


def test_non_st_bar_allows_same_price() -> None:
    """非 ST 时 10.5 不是涨停（limit_up 11.0），买单通过。"""
    bar = make_bar(open_=10.5, prev_close=10.0, is_st=False)
    assert _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT).passed


def test_explicit_is_st_overrides_bar() -> None:
    """显式传入的 ``is_st`` 优先于 ``bar.is_st``。"""
    bar = make_bar(open_=10.5, prev_close=10.0, is_st=True)
    assert _gate().check(bar, Side.BUY, 100, instrument=MAIN_INSTRUMENT, is_st=False).passed
