"""状态记录器——逐日快照，供事后复盘与归因。

设计要点：

* 只记录**可复现的状态**（数值 / 字符串），不记录对象引用
* 记录键固定，便于直接转成表格
* ``flush`` 是持久化钩子；P3 阶段为内存实现，P4 接报告时再落盘
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

__all__ = ["Recorder"]

# 固定字段顺序，保证导出表格的列稳定
BASE_FIELDS: tuple[str, ...] = (
    "date",
    "cash",
    "position",
    "price",
    "market_value",
    "total",
    "daily_pnl",
    "cumulative_pnl",
    "trades",
    "rejects",
)


@dataclass
class Recorder:
    """逐日状态记录。"""

    records: list[dict[str, Any]] = field(default_factory=list)

    # ---- 写 ----

    def snapshot(self, trading_date: date, **state: Any) -> dict[str, Any]:
        """记录一天的状态。

        ``date`` 由第一个位置参数提供；其余键值原样记录，但会拒绝覆盖 ``date``。
        """
        if "date" in state:
            raise ValueError("state 中不得包含 'date'（由参数提供）")
        row: dict[str, Any] = {"date": trading_date}
        row.update(state)
        self.records.append(row)
        return row

    # ---- 读 ----

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    @property
    def last(self) -> dict[str, Any] | None:
        return self.records[-1] if self.records else None

    def field(self, name: str) -> list[Any]:
        """取某一列（缺失记 ``None``），长度与记录数一致。"""
        return [row.get(name) for row in self.records]

    def columns(self) -> list[str]:
        """按首次出现顺序汇总所有列名。"""
        seen: dict[str, None] = {}
        for row in self.records:
            for key in row:
                seen.setdefault(key, None)
        return list(seen)

    def to_rows(self, fields: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """导出为规整的行（指定列则只保留该列，缺失补 ``None``）。"""
        if fields is None:
            return [dict(row) for row in self.records]
        return [{f: row.get(f) for f in fields} for row in self.records]

    def clear(self) -> None:
        self.records.clear()

    def flush(self) -> int:
        """持久化钩子。P3 为内存实现，返回记录条数。"""
        return len(self.records)
