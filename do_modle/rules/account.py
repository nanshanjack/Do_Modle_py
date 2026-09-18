"""账户：资金 + 冻结 + 日终快照。

资金语义（A股）::

    T 日卖出所得资金 T 日即可用于买入（可用），T+1 日才可提取（可取）。
    本实现只区分 ``cash`` / ``frozen``；"可取"不在回测范围内。

``freeze`` 用于挂单未成交时占用资金，``unfreeze`` 用于撤单释放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from do_modle.objects import AccountSnapshot, Side, Trade
from do_modle.rules.portfolio import Portfolio

__all__ = ["Account"]


@dataclass
class Account:
    """现金账户。"""

    initial_cash: float = 0.0
    cash: float = 0.0
    frozen: float = 0.0
    realized_pnl: float = 0.0
    _snapshots: list[AccountSnapshot] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.initial_cash < 0:
            raise ValueError(f"初始资金不可为负: {self.initial_cash}")
        if self.cash == 0.0:
            self.cash = self.initial_cash
        if self.cash < 0:
            raise ValueError(f"现金不可为负: {self.cash}")

    # ---- 查询 ----

    @property
    def available_cash(self) -> float:
        """可用资金 = 现金 - 冻结。"""
        return self.cash - self.frozen

    # ---- 冻结 ----

    def freeze(self, amount: float) -> None:
        if amount < 0:
            raise ValueError(f"冻结金额不可为负: {amount}")
        if amount > self.available_cash + 1e-9:
            raise ValueError(f"冻结超过可用资金: 冻结 {amount}, 可用 {self.available_cash}")
        self.frozen += amount

    def unfreeze(self, amount: float) -> None:
        if amount < 0:
            raise ValueError(f"解冻金额不可为负: {amount}")
        self.frozen = max(0.0, self.frozen - amount)

    # ---- 现金流入（分红等） ----

    def deposit(self, amount: float) -> None:
        """现金流入（如税前现金分红）。非交易性流入，不计入 ``on_trade``。"""
        if amount < 0:
            raise ValueError(f"deposit 金额不可为负: {amount}（转出请用 with_trade）")
        self.cash += amount

    # ---- 成交 ----

    def on_trade(self, trade: Trade) -> None:
        """应用成交：``cash_flow`` 买入为负、卖出为正。"""
        new_cash = self.cash + trade.cash_flow
        if new_cash < -1e-6:
            raise ValueError(
                f"现金不足：当前 {self.cash:.4f}，本次现金流 {trade.cash_flow:.4f}"
            )
        self.cash = max(0.0, new_cash)

    # ---- 结算 ----

    def settle(
        self,
        dt: date,
        portfolio: Portfolio,
        prices: dict[str, float],
        prev_closes: dict[str, float] | None = None,
    ) -> AccountSnapshot:
        """日终结算，产出快照并追加到历史。

        ``prev_closes`` 提供时，``trading_pnl`` / ``position_pnl`` 为**当日**
        盈亏分解（语义同 rqalpha）；省略时退化为**累积**浮动盈亏分今/昨仓。
        ``cumulative_pnl`` 始终是累积口径。
        """
        market_value = portfolio.market_value(prices)
        cumulative = sum(portfolio.unrealized_pnl(prices))
        if prev_closes is None:
            trading_pnl, position_pnl = portfolio.unrealized_pnl(prices)
        else:
            trading_pnl, position_pnl = portfolio.daily_pnl_breakdown(prices, prev_closes)
        snapshot = AccountSnapshot(
            dt=dt,
            cash=self.cash,
            frozen=self.frozen,
            market_value=market_value,
            total=self.cash + market_value,
            trading_pnl=trading_pnl,
            position_pnl=position_pnl,
            cumulative_pnl=cumulative,
        )
        self._snapshots.append(snapshot)
        return snapshot

    @property
    def snapshots(self) -> tuple[AccountSnapshot, ...]:
        return tuple(self._snapshots)

    @property
    def last_snapshot(self) -> AccountSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    # ---- 辅助 ----

    def max_buy_quantity(
        self, price: float, lot_size: int = 100, *, buffer: float = 0.0
    ) -> int:
        """在可用资金约束下能买入的最大股数（向下取整到 lot）。"""
        if price <= 0 or lot_size <= 0:
            return 0
        budget = max(0.0, self.available_cash - buffer)
        return int(budget // (price * lot_size)) * lot_size

    def expected_cash_after(self, side: Side, detail_amount: float, fee: float) -> float:
        """估算成交后的现金（用于下单前的可行性检查）。"""
        if side is Side.BUY:
            return self.available_cash - detail_amount - fee
        return self.available_cash + detail_amount - fee
