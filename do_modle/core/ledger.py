"""账务——**系统中唯一的账务更新点**。

职责边界：

* 应用成交到 ``Account``（现金）与 ``PositionBook``（持仓）
* 记录已实现盈亏
* 日终结算产出快照

**不做**撮合、不做风控、不做估值取价。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from do_modle.objects import AccountSnapshot, CorporateAction, Side, Trade
from do_modle.rules.account import Account
from do_modle.rules.corporate_action import CorporateActionHandler
from do_modle.rules.portfolio import Portfolio

__all__ = ["Ledger"]


@dataclass
class Ledger:
    """账户 + 组合的账务。"""

    portfolio: Portfolio
    account: Account
    realized_pnl: float = 0.0
    trade_count: int = 0
    dividend_cash: float = 0.0  # 累计税前现金分红
    corporate_action_count: int = 0

    # ---- 生命周期 ----

    def on_before_trading(self, trading_date: date) -> None:
        """日初：所有持仓簿结转昨仓、清空冻结。"""
        self.portfolio.before_trading(trading_date)

    # ---- 公司行为 ----

    def on_corporate_action(self, action: CorporateAction) -> float:
        """应用公司行为，返回现金流入。

        **必须在除权日盘前调用**：股权登记日是 T-1，除权除息日是 T，
        故 T 日开盘前持仓即已享有送转与分红。
        """
        handler = CorporateActionHandler()
        cash = handler.apply(self.portfolio.book(action.symbol), action)
        if cash:
            self.account.deposit(cash)
            self.dividend_cash += cash
        self.corporate_action_count += 1
        return cash

    # ---- 成交 ----

    def on_trade(self, trade: Trade) -> float:
        """应用一笔成交，返回该笔的已实现盈亏（买入为 0）。

        买入用**含滑点成交价**作为批次成本——那才是真实付出的价格。
        """
        book = self.portfolio.book(trade.symbol)
        trading_date = trade.dt.date() if hasattr(trade.dt, "date") else trade.dt

        realized = 0.0
        if trade.side is Side.BUY:
            book.buy(trading_date, trade.quantity, trade.price)
        else:
            details = book.sell(trading_date, trade.quantity, trade.price)
            realized = sum(
                (trade.price - cost_price) * qty for _, qty, cost_price in details
            )
            self.realized_pnl += realized

        self.account.on_trade(trade)
        self.trade_count += 1
        return realized

    def on_trades(self, trades: "list[Trade] | tuple[Trade, ...]") -> float:
        """批量应用成交，返回累计已实现盈亏。"""
        return sum(self.on_trade(t) for t in trades)

    # ---- 结算 ----

    def settle(
        self,
        trading_date: date,
        prices: dict[str, float],
        prev_closes: dict[str, float] | None = None,
    ) -> AccountSnapshot:
        """日终结算。"""
        return self.account.settle(trading_date, self.portfolio, prices, prev_closes)

    # ---- 查询 ----

    def position(self, symbol: str) -> int:
        return self.portfolio.book(symbol).total

    def sellable(self, symbol: str) -> int:
        return self.portfolio.book(symbol).sellable

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def available_cash(self) -> float:
        return self.account.available_cash
