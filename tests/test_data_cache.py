"""``data/cache.py`` 单测 —— 幂等 upsert、历史行不被修改。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from do_modle.data.cache import CacheError, DataCache


@pytest.fixture
def cache() -> DataCache:
    with DataCache(":memory:") as c:
        yield c


ROWS = [
    {"date": "2026-09-14", "symbol": "sh.601872", "close": 20.0},
    {"date": "2026-09-15", "symbol": "sh.601872", "close": 20.5},
]
KEYS = ("date", "symbol")


# ==================== 基本读写 ====================

def test_upsert_creates_table_and_rows(cache: DataCache) -> None:
    assert cache.upsert("daily_bar", ROWS, KEYS) == 2
    assert cache.count("daily_bar") == 2
    assert "daily_bar" in cache.tables()


def test_read_returns_dicts(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    got = cache.read("daily_bar", order_by="date")
    assert [r["close"] for r in got] == [20.0, 20.5]


def test_read_with_filter(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    got = cache.read("daily_bar", date="2026-09-15")
    assert len(got) == 1
    assert got[0]["close"] == 20.5


def test_read_descending(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    got = cache.read("daily_bar", order_by="date", descending=True)
    assert [r["date"] for r in got] == ["2026-09-15", "2026-09-14"]


def test_read_missing_table_returns_empty(cache: DataCache) -> None:
    assert cache.read("nope") == []


def test_upsert_empty_rows_returns_zero(cache: DataCache) -> None:
    assert cache.upsert("daily_bar", [], KEYS) == 0
    assert "daily_bar" not in cache.tables()


# ==================== 幂等与增量（设计硬要求） ====================

def test_upsert_is_idempotent(cache: DataCache) -> None:
    """同批数据重复写入不产生重复行。"""
    cache.upsert("daily_bar", ROWS, KEYS)
    cache.upsert("daily_bar", ROWS, KEYS)
    assert cache.count("daily_bar") == 2


def test_upsert_same_key_overwrites_value(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    cache.upsert(
        "daily_bar", [{"date": "2026-09-15", "symbol": "sh.601872", "close": 99.0}], KEYS
    )
    assert cache.count("daily_bar") == 2
    assert cache.read("daily_bar", date="2026-09-15")[0]["close"] == 99.0


def test_incremental_upsert_does_not_modify_history(cache: DataCache) -> None:
    """增量追加新日期时，历史行必须原样保留。"""
    cache.upsert("daily_bar", ROWS, KEYS)
    before = cache.read("daily_bar", date="2026-09-14")[0]
    cache.upsert(
        "daily_bar", [{"date": "2026-09-16", "symbol": "sh.601872", "close": 21.0}], KEYS
    )
    after = cache.read("daily_bar", date="2026-09-14")[0]
    assert before == after
    assert cache.count("daily_bar") == 3


def test_multi_symbol_does_not_collide(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    cache.upsert(
        "daily_bar", [{"date": "2026-09-15", "symbol": "sh.600026", "close": 12.0}], KEYS
    )
    assert cache.count("daily_bar") == 3
    assert cache.read("daily_bar", symbol="sh.600026")[0]["close"] == 12.0


# ==================== 校验 ====================

def test_inconsistent_columns_rejected(cache: DataCache) -> None:
    with pytest.raises(CacheError, match="列与首行不一致"):
        cache.upsert(
            "daily_bar",
            [
                {"date": "2026-09-14", "close": 1.0},
                {"date": "2026-09-15", "close": 2.0, "extra": 3},
            ],
            ("date",),
        )


def test_key_not_in_columns_rejected(cache: DataCache) -> None:
    with pytest.raises(CacheError, match="主键列不在数据列中"):
        cache.upsert("daily_bar", ROWS, ("date", "volume"))


def test_invalid_table_name_rejected(cache: DataCache) -> None:
    with pytest.raises(CacheError, match="非法标识符"):
        cache.upsert("daily_bar; DROP TABLE x", ROWS, KEYS)


def test_invalid_column_name_rejected(cache: DataCache) -> None:
    with pytest.raises(CacheError, match="非法标识符"):
        cache.upsert("t", [{"a b": 1}], ("a b",))


def test_invalid_filter_column_rejected(cache: DataCache) -> None:
    cache.upsert("daily_bar", ROWS, KEYS)
    with pytest.raises(CacheError, match="非法标识符"):
        cache.read("daily_bar", **{"1=1 OR 1=1": 1})


# ==================== 类型序列化 ====================

def test_date_and_datetime_serialized_as_iso(cache: DataCache) -> None:
    cache.upsert(
        "t",
        [{"d": date(2026, 9, 15), "ts": datetime(2026, 9, 15, 15, 0), "v": 1.0}],
        ("d",),
    )
    row = cache.read("t")[0]
    assert row["d"] == "2026-09-15"
    assert row["ts"] == "2026-09-15 15:00:00"


def test_bool_stored_as_integer(cache: DataCache) -> None:
    cache.upsert("t", [{"d": "2026-09-15", "flag": True}], ("d",))
    assert cache.read("t")[0]["flag"] == 1


def test_none_stored_as_null(cache: DataCache) -> None:
    cache.upsert("t", [{"d": "2026-09-15", "v": None}], ("d",))
    assert cache.read("t")[0]["v"] is None


# ==================== 生命周期 ====================

def test_context_manager_closes() -> None:
    with DataCache(":memory:") as c:
        c.upsert("t", [{"d": "2026-09-15"}], ("d",))
    with pytest.raises(Exception):
        c.read("t")


def test_upsert_many_alias(cache: DataCache) -> None:
    assert cache.upsert_many("daily_bar", iter(ROWS), KEYS) == 2


def test_tables_lists_created(cache: DataCache) -> None:
    cache.upsert("a", [{"k": 1}], ("k",))
    cache.upsert("b", [{"k": 1}], ("k",))
    assert cache.tables() == ["a", "b"]
