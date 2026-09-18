"""``DataProvider`` 抽象与注册表。

设计目标：**换数据源不改引擎**（借鉴 rqalpha 的 ``mod`` 机制，简化为注册表）。

策略与引擎只依赖 :class:`DataProvider` 接口，不认具体数据源。新增数据源只需
实现接口并 ``@register("name")``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Callable, TypeVar

from do_modle.objects import AdjustFlag, Bar, Board, CorporateAction, Instrument

__all__ = [
    "DataProvider",
    "PROVIDERS",
    "register",
    "create_provider",
    "ProviderError",
]

PROVIDERS: dict[str, type["DataProvider"]] = {}

T = TypeVar("T", bound="DataProvider")


class ProviderError(RuntimeError):
    """数据源相关错误（连接失败、字段缺失等）。"""


def register(name: str) -> Callable[[type[T]], type[T]]:
    """把适配器注册到全局表。"""

    def deco(cls: type[T]) -> type[T]:
        if name in PROVIDERS and PROVIDERS[name] is not cls:
            raise ValueError(f"数据源名称已被占用: {name}")
        cls.name = name
        PROVIDERS[name] = cls
        return cls

    return deco


def create_provider(name: str, **kwargs: Any) -> "DataProvider":
    """按名称创建数据源实例。"""
    if name not in PROVIDERS:
        raise ProviderError(f"未注册的数据源: {name}；可用: {sorted(PROVIDERS)}")
    return PROVIDERS[name](**kwargs)


class DataProvider(ABC):
    """数据源接口。

    只有 ``get_bars`` / ``get_instrument`` / ``get_calendar`` 是必须实现的；
    其余提供保守默认值，让新数据源可以先跑起来再补。
    """

    name: str = ""

    # ---- 必须实现 ----

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        freq: str,
        start: date,
        end: date,
        adjust: AdjustFlag = AdjustFlag.NONE,
    ) -> list[Bar]:
        """取 K 线。``adjust`` 决定复权口径，返回的 Bar 会标记 ``adjust_flag``。"""

    @abstractmethod
    def get_instrument(self, symbol: str) -> Instrument:
        """取标的静态元数据。"""

    @abstractmethod
    def get_calendar(self, start: date, end: date) -> list[date]:
        """取交易日历（升序）。"""

    # ---- 可选（保守默认） ----

    def get_board(self, symbol: str, d: date) -> Board:
        """板块归属（按日，PIT）。默认取 Instrument 上的静态板块。"""
        return self.get_instrument(symbol).board

    def get_suspended(self, d: date) -> set[str]:
        """当日停牌标的集合。默认空集。"""
        return set()

    def get_risk_warning(self, d: date) -> set[str]:
        """当日风险警示（ST）标的集合。默认空集。

        单标的下通常直接用 ``Bar.is_st``（日线自带），此接口用于批量场景。
        """
        return set()

    def get_dividends(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateAction]:
        """区间内的分红送转。默认空列表。"""
        return []

    # ---- 上下文管理（需要会话的数据源覆盖） ----

    def connect(self) -> None:
        """建立会话（如 baostock login）。默认无操作。"""

    def disconnect(self) -> None:
        """释放会话。默认无操作。"""

    def __enter__(self) -> "DataProvider":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    # ---- 工具 ----

    @staticmethod
    def _sorted_unique(dates: list[date]) -> list[date]:
        return sorted(set(dates))
