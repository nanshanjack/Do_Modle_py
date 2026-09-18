"""``data/provider.py`` 单测 —— 抽象接口与注册表。"""

from __future__ import annotations

from datetime import date

import pytest

from do_modle.data.provider import (
    PROVIDERS,
    DataProvider,
    ProviderError,
    create_provider,
    register,
)
from do_modle.objects import AdjustFlag, Board, CorporateAction, Instrument
from tests._helpers import D0, D1, D2, SYMBOL, make_bar
from tests.fakes import FakeProvider

INSTRUMENT = Instrument(
    symbol=SYMBOL, name="招商轮船", board=Board.MAIN, list_date=date(2006, 12, 1)
)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(
        bars={(SYMBOL, AdjustFlag.NONE): [make_bar(d=D0), make_bar(d=D1)]},
        instruments={SYMBOL: INSTRUMENT},
        calendar=[D0, D1],
        suspended={D1: {SYMBOL}},
        risk_warning={D1: {"sh.600999"}},
        dividends={SYMBOL: [CorporateAction(SYMBOL, D2, cash_per_share=0.5)]},
    )


# ==================== 注册表 ====================

def test_fake_provider_registered() -> None:
    assert "fake" in PROVIDERS
    assert PROVIDERS["fake"] is FakeProvider


def test_create_provider_by_name() -> None:
    assert isinstance(create_provider("fake"), FakeProvider)


def test_create_unknown_provider_raises() -> None:
    with pytest.raises(ProviderError, match="未注册的数据源"):
        create_provider("nonexistent")


def test_register_duplicate_name_with_other_class_raises() -> None:
    with pytest.raises(ValueError, match="已被占用"):

        @register("fake")
        class _Other(DataProvider):  # pragma: no cover
            def get_bars(self, *a, **k): ...
            def get_instrument(self, *a, **k): ...
            def get_calendar(self, *a, **k): ...


def test_register_same_class_twice_is_ok() -> None:
    register("fake")(FakeProvider)
    assert PROVIDERS["fake"] is FakeProvider


# ==================== 必须实现的接口 ====================

def test_abstract_methods_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        DataProvider()  # type: ignore[abstract]


# ==================== 可选接口的保守默认 ====================

def test_default_board_falls_back_to_instrument(provider: FakeProvider) -> None:
    assert provider.get_board(SYMBOL, D1) is Board.MAIN


def test_default_optional_methods_are_empty() -> None:
    class Bare(DataProvider):
        def get_bars(self, symbol, freq="1d", start=None, end=None, adjust=AdjustFlag.NONE):
            return []

        def get_instrument(self, symbol):
            return INSTRUMENT

        def get_calendar(self, start, end):
            return []

    b = Bare()
    assert b.get_suspended(D1) == set()
    assert b.get_risk_warning(D1) == set()
    assert b.get_dividends(SYMBOL, D0, D2) == []


# ==================== FakeProvider 行为 ====================

def test_get_bars_filters_date_range(provider: FakeProvider) -> None:
    assert len(provider.get_bars(SYMBOL, "1d", D0, D2)) == 2
    assert len(provider.get_bars(SYMBOL, "1d", D1, D2)) == 1


def test_get_bars_separates_adjust_flag() -> None:
    p = FakeProvider(
        bars={
            (SYMBOL, AdjustFlag.NONE): [make_bar(d=D0)],
            (SYMBOL, AdjustFlag.HFQ): [make_bar(d=D0), make_bar(d=D1)],
        }
    )
    assert len(p.get_bars(SYMBOL, "1d", adjust=AdjustFlag.NONE)) == 1
    assert len(p.get_bars(SYMBOL, "1d", adjust=AdjustFlag.HFQ)) == 2


def test_get_instrument_unknown_raises(provider: FakeProvider) -> None:
    with pytest.raises(KeyError):
        provider.get_instrument("sh.999999")


def test_get_calendar(provider: FakeProvider) -> None:
    assert provider.get_calendar(D0, D1) == [D0, D1]


def test_get_suspended_and_risk_warning(provider: FakeProvider) -> None:
    assert provider.get_suspended(D1) == {SYMBOL}
    assert provider.get_risk_warning(D1) == {"sh.600999"}


def test_get_dividends_filters_range(provider: FakeProvider) -> None:
    assert len(provider.get_dividends(SYMBOL, D0, D2)) == 1
    assert provider.get_dividends(SYMBOL, D0, D1) == []


# ==================== 会话管理 ====================

def test_context_manager_connects_and_disconnects(provider: FakeProvider) -> None:
    assert not provider.connected
    with provider:
        assert provider.connected
    assert not provider.connected


def test_connect_is_not_idempotent_by_default(provider: FakeProvider) -> None:
    """基类 connect 无状态；子类需自行保证幂等（baostock 适配器已实现）。"""
    provider.connect()
    provider.connect()
    assert provider.connect_calls == 2
