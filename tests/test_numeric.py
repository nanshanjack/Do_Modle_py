"""``do_modle/numeric.py`` 单测 —— 价格取整与手数对齐。"""

from __future__ import annotations

import pytest

from do_modle.numeric import ceil_to_lot, floor_to_lot, round_half_up, round_to_tick


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (11.033, 11.03),
        (9.027, 9.03),
        (10.005, 10.01),
        (10.0, 10.0),
        (11.0, 11.0),
        (2.8314, 2.83),
    ],
)
def test_round_to_tick(raw: float, expected: float) -> None:
    assert round_to_tick(raw) == pytest.approx(expected)


def test_round_to_tick_uses_half_up_not_bankers() -> None:
    """10.005 在二进制里是 10.004999...，银行家舍入会给 10.0；必须用 HALF_UP。"""
    assert round_to_tick(10.005) == pytest.approx(10.01)


def test_round_to_tick_custom_tick() -> None:
    assert round_to_tick(2.8314, tick=0.001) == pytest.approx(2.831)


def test_round_to_tick_non_positive_tick_returns_value() -> None:
    assert round_to_tick(2.8314, tick=0) == pytest.approx(2.8314)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1234, 1200), (1200, 1200), (99, 0), (100, 100), (0, 0)],
)
def test_floor_to_lot(raw: int, expected: int) -> None:
    assert floor_to_lot(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1234, 1300), (1200, 1200), (1, 100), (0, 0)],
)
def test_ceil_to_lot(raw: int, expected: int) -> None:
    assert ceil_to_lot(raw) == expected


def test_lot_helpers_with_custom_lot() -> None:
    assert floor_to_lot(250, lot=50) == 250
    assert ceil_to_lot(251, lot=50) == 300


@pytest.mark.parametrize(
    ("raw", "digits", "expected"),
    [(1.005, 2, 1.01), (1.004, 2, 1.0), (2.5, 0, 3.0), (3.5, 0, 4.0)],
)
def test_round_half_up(raw: float, digits: int, expected: float) -> None:
    assert round_half_up(raw, digits) == pytest.approx(expected)
