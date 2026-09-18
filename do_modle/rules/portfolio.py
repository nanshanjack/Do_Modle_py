"""多标的持仓容器。

**即使阶段一只有 601872 一个 key，也必须用容器**——这是"功能可以砍、
接口不能砍"原则的落点。后续扩展标的时不需要改引擎与账务。
"""

from __future__ import annotations

from datetime import date
from typing import Iterator

from do_modle.rules.position import PositionBook

__all__ = ["Portfolio"]


class Portfolio:
    """持仓组合：``symbol -> PositionBook``。"""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self._books: dict[str, PositionBook] = {}
        for sym in symbols or []:
            self._books[sym] = PositionBook()

    # ---- 访问 ----

    def book(self, symbol: str) -> PositionBook:
        """取得（必要时创建）某标的的持仓簿。"""
        if symbol not in self._books:
            self._books[symbol] = PositionBook()
        return self._books[symbol]

    def has_position(self, symbol: str) -> bool:
        return symbol in self._books and self._books[symbol].total > 0

    def symbols(self) -> tuple[str, ...]:
        return tuple(self._books.keys())

    def held_symbols(self) -> tuple[str, ...]:
        return tuple(s for s, b in self._books.items() if b.total > 0)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._books

    def __iter__(self) -> Iterator[tuple[str, PositionBook]]:
        return iter(self._books.items())

    # ---- 生命周期 ----

    def before_trading(self, trading_date: date) -> None:
        """日初：所有标的的持仓簿一起结转。"""
        for book in self._books.values():
            book.before_trading(trading_date)

    # ---- 估值 ----

    def market_value(self, prices: dict[str, float]) -> float:
        """持仓市值。持仓非零但缺价格时抛 ``KeyError``（避免静默用错价）。"""
        total = 0.0
        for symbol, book in self._books.items():
            if book.total == 0:
                continue
            if symbol not in prices:
                raise KeyError(f"缺少 {symbol} 的估值价格（持仓 {book.total} 股）")
            total += book.total * prices[symbol]
        return total

    def total_cost(self) -> float:
        return sum(book.total_cost for book in self._books.values())

    def unrealized_pnl(self, prices: dict[str, float]) -> tuple[float, float]:
        """**累积**浮动盈亏（相对成本），返回 ``(今仓, 昨仓)``，跨标的口径统一。"""
        trading = 0.0
        position = 0.0
        for symbol, book in self._books.items():
            if book.total == 0:
                continue
            if symbol not in prices:
                raise KeyError(f"缺少 {symbol} 的估值价格（持仓 {book.total} 股）")
            t_pnl, p_pnl = book.unrealized_pnl(prices[symbol])
            trading += t_pnl
            position += p_pnl
        return trading, position

    def daily_pnl_breakdown(
        self, prices: dict[str, float], prev_closes: dict[str, float]
    ) -> tuple[float, float]:
        """**当日**盈亏分解，返回 ``(trading_pnl, position_pnl)``，跨标的汇总。

        语义见 :meth:`PositionBook.daily_pnl_breakdown`。缺少 ``prev_closes``
        时抛 ``KeyError``——静默用 0 会让当日盈亏失真。
        """
        trading = 0.0
        position = 0.0
        for symbol, book in self._books.items():
            if book.total == 0:
                continue
            if symbol not in prices:
                raise KeyError(f"缺少 {symbol} 的估值价格（持仓 {book.total} 股）")
            if symbol not in prev_closes:
                raise KeyError(f"缺少 {symbol} 的昨收价（持仓 {book.total} 股）")
            t_pnl, p_pnl = book.daily_pnl_breakdown(
                prices[symbol], prev_closes[symbol]
            )
            trading += t_pnl
            position += p_pnl
        return trading, position
