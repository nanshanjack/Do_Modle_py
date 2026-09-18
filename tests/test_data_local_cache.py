"""``data/adapters/local_cache.py`` 单测 —— 用临时 SQLite，不依赖网络。"""

from __future__ import annotations

from datetime import date

import pytest

from do_modle.data.adapters.local_cache import CachedProvider
from do_modle.data.cache import DataCache
from do_modle.data.provider import PROVIDERS, ProviderError
from do_modle.objects import AdjustFlag, Board
from tests._helpers import D0, D1, D2, SYMBOL


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "market.sqlite"
    with DataCache(path) as cache:
        cache.upsert(
            "daily_bar_none",
            [
                {
                    "symbol": SYMBOL, "date": "2026-09-14", "open": 10.0, "high": 10.2,
                    "low": 9.9, "close": 10.1, "prev_close": 10.0, "volume": 1000.0,
                    "amount": 10100.0, "is_suspended": False, "is_st": False,
                },
                {
                    "symbol": SYMBOL, "date": "2026-09-15", "open": 10.1, "high": 10.3,
                    "low": 10.0, "close": 10.2, "prev_close": 10.1, "volume": 0.0,
                    "amount": 0.0, "is_suspended": True, "is_st": True,
                },
            ],
            ("symbol", "date"),
        )
        cache.upsert(
            "daily_bar_hfq",
            [
                {
                    "symbol": SYMBOL, "date": "2026-09-14", "open": 15.0, "high": 15.3,
                    "low": 14.8, "close": 15.1, "prev_close": 15.0, "volume": 1000.0,
                    "amount": 10100.0, "is_suspended": False, "is_st": False,
                }
            ],
            ("symbol", "date"),
        )
        cache.upsert(
            "calendar",
            [{"date": "2026-09-14"}, {"date": "2026-09-15"}, {"date": "2026-09-16"}],
            ("date",),
        )
        cache.upsert(
            "instrument",
            [{"symbol": SYMBOL, "name": "招商轮船", "board": "MAIN",
              "list_date": "2006-12-01"}],
            ("symbol",),
        )
        cache.upsert(
            "dividend",
            [{"symbol": SYMBOL, "ex_date": "2026-09-16", "share_ratio": 0.3,
              "cash_per_share": 0.5}],
            ("symbol", "ex_date"),
        )
    return path


@pytest.fixture
def provider(db):
    p = CachedProvider(db)
    yield p
    p.close()


def test_registered() -> None:
    assert "cache" in PROVIDERS


def test_missing_db_raises(tmp_path) -> None:
    with pytest.raises(ProviderError, match="缓存不存在"):
        CachedProvider(tmp_path / "nope.sqlite")


def test_get_bars_none(provider: CachedProvider) -> None:
    bars = provider.get_bars(SYMBOL, "1d", D0, D2)
    assert len(bars) == 2
    assert bars[0].close == pytest.approx(10.1)
    assert bars[0].adjust_flag is AdjustFlag.NONE


def test_get_bars_reads_suspended_and_st(provider: CachedProvider) -> None:
    bars = provider.get_bars(SYMBOL, "1d", D0, D2)
    assert bars[1].is_suspended is True
    assert bars[1].is_st is True


def test_get_bars_hfq_returns_different_prices(provider: CachedProvider) -> None:
    bars = provider.get_bars(SYMBOL, "1d", D0, D2, adjust=AdjustFlag.HFQ)
    assert len(bars) == 1
    assert bars[0].close == pytest.approx(15.1)
    assert bars[0].adjust_flag is AdjustFlag.HFQ


def test_qfq_rejected(provider: CachedProvider) -> None:
    """前复权不入库——它会重算历史价格，引入前视。"""
    with pytest.raises(ProviderError, match="前复权"):
        provider.get_bars(SYMBOL, "1d", D0, D2, adjust=AdjustFlag.QFQ)


def test_date_range_filter(provider: CachedProvider) -> None:
    assert len(provider.get_bars(SYMBOL, "1d", D1, D1)) == 1
    assert provider.get_bars(SYMBOL, "1d", date(2027, 1, 1), date(2027, 2, 1)) == []


def test_get_calendar(provider: CachedProvider) -> None:
    assert provider.get_calendar(D0, D1) == [D0, D1]
    assert len(provider.get_calendar(D0, D2)) == 3


def test_get_instrument(provider: CachedProvider) -> None:
    inst = provider.get_instrument(SYMBOL)
    assert inst.name == "招商轮船"
    assert inst.board is Board.MAIN
    assert inst.list_date == date(2006, 12, 1)


def test_get_instrument_unknown_raises(provider: CachedProvider) -> None:
    with pytest.raises(ProviderError):
        provider.get_instrument("sh.999999")


def test_get_dividends(provider: CachedProvider) -> None:
    divs = provider.get_dividends(SYMBOL, D0, D2)
    assert len(divs) == 1
    assert divs[0].ex_date == D2
    assert divs[0].share_ratio == pytest.approx(0.3)
    assert divs[0].cash_per_share == pytest.approx(0.5)


def test_get_dividends_out_of_range(provider: CachedProvider) -> None:
    assert provider.get_dividends(SYMBOL, D0, D1) == []


def test_stats(provider: CachedProvider) -> None:
    stats = provider.stats()
    assert stats["daily_bar_none"] == 2
    assert stats["calendar"] == 3


def test_context_manager_closes(db) -> None:
    with CachedProvider(db) as p:
        assert len(p.get_calendar(D0, D2)) == 3
