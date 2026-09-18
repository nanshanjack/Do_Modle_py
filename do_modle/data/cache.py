"""``DataCache`` —— 本地缓存，基于 **SQLite（标准库）**。

设计文档写的是 "Parquet/SQLite"，此处**选 SQLite**，理由：

1. **零额外依赖**（stdlib），而 Parquet 需要 pandas + pyarrow（~50MB）
2. **原生 upsert 语义**——设计强制要求"增量 upsert 幂等、历史行不被修改"，
   SQLite 的 ``INSERT OR REPLACE`` 直接满足；Parquet 需全量重写
3. 单标的 2500 行规模下两者性能无差异
4. 表结构动态、可用 SQL 灵活查询（P5 取因子列时有用）

后续如需 bulk 导出，可加 ``export_parquet()``，但**不作为主存储**。

**存储原则**：

* 原始**不复权**数据是唯一真相源，复权数据是派生物，可随时重算
* 所有 meta 表必须带日期维度，不得存"当前快照"
* 按主键 upsert，禁止整表覆盖
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["DataCache", "CacheError"]

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CacheError(RuntimeError):
    """缓存层错误。"""


def _check_ident(name: str) -> str:
    """表名/列名白名单校验（SQLite 不支持参数化标识符，只能校验）。"""
    if not _IDENT_RE.match(name):
        raise CacheError(f"非法标识符: {name!r}（只允许字母、数字、下划线）")
    return name


def _sql_type(value: Any) -> str:
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


def _to_db(value: Any) -> Any:
    """把 Python 值转成 SQLite 可存储的标量。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


class DataCache:
    """SQLite 缓存。支持上下文管理。

    >>> with DataCache(":memory:") as c:
    ...     c.upsert("daily_bar", [{"date": "2026-09-15", "close": 20.0}], ("date",))
    1
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    # ---- 生命周期 ----

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DataCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 写 ----

    def upsert(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        keys: Sequence[str],
    ) -> int:
        """按主键 upsert，返回写入行数。

        * 表不存在则按首行的键创建
        * 所有行的列必须一致
        * 重复调用幂等（同主键覆盖，其它行不受影响）
        """
        table = _check_ident(table)
        if not rows:
            return 0
        columns = list(rows[0].keys())
        for col in columns:
            _check_ident(col)
        for i, row in enumerate(rows):
            if list(row.keys()) != columns:
                raise CacheError(
                    f"第 {i} 行的列与首行不一致：{list(row.keys())} != {columns}"
                )
        key_cols = [_check_ident(k) for k in keys]
        missing = [k for k in key_cols if k not in columns]
        if missing:
            raise CacheError(f"主键列不在数据列中: {missing}")

        self._ensure_table(table, columns, rows[0], key_cols)

        placeholders = ", ".join("?" for _ in columns)
        col_sql = ", ".join(f'"{c}"' for c in columns)
        sql = f'INSERT OR REPLACE INTO "{table}" ({col_sql}) VALUES ({placeholders})'
        payload = [tuple(_to_db(row[c]) for c in columns) for row in rows]
        with self._conn:
            self._conn.executemany(sql, payload)
        return len(payload)

    def _ensure_table(
        self,
        table: str,
        columns: list[str],
        sample: Mapping[str, Any],
        key_cols: list[str],
    ) -> None:
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            self._migrate_columns(table, columns, sample)
            return
        defs = []
        for col in columns:
            decl = _sql_type(sample[col])
            defs.append(f'"{col}" {decl}')
        defs.append(f'PRIMARY KEY ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in key_cols)})')
        ddl = f'CREATE TABLE "{table}" ({", ".join(defs)})'
        with self._conn:
            self._conn.execute(ddl)

    def _migrate_columns(
        self, table: str, columns: list[str], sample: Mapping[str, Any]
    ) -> None:
        """已存在的表补齐缺失列（``ALTER TABLE ADD COLUMN``，可空）。

        这样新增字段（如估值列）无需删表重建，已落库的历史行保留原值、
        新列为 NULL；下次同步会回填。
        """
        existing = {
            row[1] for row in self._conn.execute(f'PRAGMA table_info("{table}")')
        }
        missing = [c for c in columns if c not in existing]
        if not missing:
            return
        with self._conn:
            for col in missing:
                self._conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{col}" {_sql_type(sample[col])}'
                )

    # ---- 读 ----

    def read(
        self,
        table: str,
        *,
        order_by: str | None = None,
        descending: bool = False,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """按等值条件查询。"""
        table = _check_ident(table)
        if not self._has_table(table):
            return []
        where = ""
        params: list[Any] = []
        if filters:
            clauses = []
            for col, val in filters.items():
                clauses.append(f'"{_check_ident(col)}" = ?')
                params.append(_to_db(val))
            where = " WHERE " + " AND ".join(clauses)
        order = ""
        if order_by:
            direction = "DESC" if descending else "ASC"
            order = f' ORDER BY "{_check_ident(order_by)}" {direction}'
        sql = f'SELECT * FROM "{table}"{where}{order}'
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count(self, table: str, **filters: Any) -> int:
        return len(self.read(table, **filters))

    def tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def _has_table(self, table: str) -> bool:
        return (
            self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    # ---- 便捷 ----

    def upsert_many(
        self, table: str, rows: Iterable[Mapping[str, Any]], keys: Sequence[str]
    ) -> int:
        """批量 upsert（等价于 ``upsert``，语义化别名）。"""
        return self.upsert(table, list(rows), keys)
