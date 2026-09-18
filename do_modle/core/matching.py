"""撮合引擎——**系统中唯一的撮合点**。

撮合规则（设计文档 §3.2，逐条可测）::

    1. 成交价基准 = bar.open（次日开盘）  ← 禁止改为当日收盘
    2. 滑点：买入上滑、卖出下滑
    3. 涨停不可买 / 跌停不可卖（非对称，见 rules/tradability）
    4. 停牌 / 一字板不可成交
    5. 成交量约束：<= volume * volume_limit_pct
    6. 买入数量对齐 100 股
    7. 资金不足 → 按可用资金缩减数量（可配置为拒单）
    8. 卖出 <= PositionBook.sellable（T+1）
    9. 部分成交：受流动性约束裁剪，剩余默认撤销
    10. 卖出所得连费用都盖不住 → 拒单（防现金转负）

**订单处理顺序**：**先卖后买**（各自按 ``seq`` 升序）。理由：A股 T 日卖出资金
T 日即可用于买入，先卖后买才能正确建模"调仓"场景。

**未来函数守卫**：``match`` 接收 ``signal_date``；若成交 Bar 的日期不晚于信号日，
直接抛 ``ValueError``。这比"靠约定"可靠。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Sequence

from do_modle.numeric import floor_to_lot
from do_modle.objects import Bar, Instrument, OrderRequest, RejectReason, Side, Trade
from do_modle.rules.cost import AShareCostModel
from do_modle.rules.position import PositionBook
from do_modle.rules.tradability import TradabilityGate

__all__ = ["MatchingEngine", "MatchResult", "RejectedOrder"]

CashPolicy = Literal["shrink", "reject"]


@dataclass(frozen=True)
class RejectedOrder:
    """被拒订单 + 原因。原因必须落库——否则风控与规则无法归因。"""

    order: OrderRequest
    reason: str

    @property
    def symbol(self) -> str:
        return self.order.symbol

    @property
    def side(self) -> Side:
        return self.order.side


@dataclass(frozen=True)
class MatchResult:
    """一次撮合的结果。"""

    trades: tuple[Trade, ...] = ()
    rejects: tuple[RejectedOrder, ...] = ()

    @property
    def filled_quantity(self) -> int:
        return sum(t.quantity for t in self.trades)

    @property
    def cash_flow(self) -> float:
        return sum(t.cash_flow for t in self.trades)

    @property
    def total_fee(self) -> float:
        return sum(t.total_fee for t in self.trades)

    def __len__(self) -> int:
        return len(self.trades)

    def __bool__(self) -> bool:
        return bool(self.trades) or bool(self.rejects)


class MatchingEngine:
    """日频撮合器。回测与实盘共用同一实现（实盘侧只替换执行价来源）。"""

    def __init__(
        self,
        cost: AShareCostModel | None = None,
        gate: TradabilityGate | None = None,
        *,
        volume_limit_pct: float = 0.10,
        insufficient_cash_policy: CashPolicy = "shrink",
        execution_price: str = "next_open",
    ) -> None:
        if execution_price != "next_open":
            raise ValueError(
                f"execution_price 只允许 'next_open'，收到 {execution_price!r}；"
                "用当日收盘价成交会引入未来函数"
            )
        if insufficient_cash_policy not in ("shrink", "reject"):
            raise ValueError(f"未知 insufficient_cash_policy: {insufficient_cash_policy!r}")
        self.cost = cost or AShareCostModel()
        self.gate = gate or TradabilityGate(volume_limit_pct=volume_limit_pct)
        self.volume_limit_pct = volume_limit_pct
        self.insufficient_cash_policy: CashPolicy = insufficient_cash_policy
        self.execution_price = execution_price

    # ---- 主流程 ----

    def match(
        self,
        orders: Sequence[OrderRequest],
        bar: Bar,
        *,
        instrument: Instrument,
        book: PositionBook,
        available_cash: float,
        signal_date: date | None = None,
    ) -> MatchResult:
        """撮合一批订单。不修改 ``book`` / 账户——账务由 ``Ledger`` 负责。"""
        if signal_date is not None and bar.dt.date() <= signal_date:
            raise ValueError(
                f"未来函数：信号日 {signal_date}，成交 Bar 日期 {bar.dt.date()}；"
                "成交必须发生在信号日之后"
            )

        trades: list[Trade] = []
        rejects: list[RejectedOrder] = []
        cash = available_cash
        sellable = book.sellable

        for order in self._ordered(orders):
            verdict = self.gate.check(
                bar, order.side, order.quantity, instrument=instrument
            )
            if not verdict.passed:
                rejects.append(RejectedOrder(order, verdict.reason))
                continue
            qty = order.quantity if verdict.allowed_quantity is None else verdict.allowed_quantity
            qty = floor_to_lot(qty, instrument.lot_size) if order.side is Side.BUY else qty
            if qty <= 0:
                rejects.append(RejectedOrder(order, RejectReason.ILLIQUID.value))
                continue

            if order.side is Side.BUY:
                qty = self._fit_buy(qty, bar.open, cash, instrument.lot_size)
                if qty <= 0:
                    rejects.append(RejectedOrder(order, RejectReason.INSUFFICIENT_CASH.value))
                    continue
                detail = self.cost.buy(qty, bar.open)
                cash += detail.cash_flow  # cash_flow 为负
            else:
                qty = min(qty, sellable)
                if qty <= 0:
                    rejects.append(RejectedOrder(order, RejectReason.T1_VIOLATION.value))
                    continue
                detail = self.cost.sell(qty, bar.open)
                if detail.cash_flow < 0:
                    # 卖出所得连费用都盖不住（极小数量 + 最低佣金）→ 拒单，防现金转负
                    rejects.append(
                        RejectedOrder(order, RejectReason.INSUFFICIENT_CASH.value)
                    )
                    continue
                cash += detail.cash_flow  # cash_flow 为正
                sellable -= qty

            trades.append(
                self.cost.to_trade(
                    detail, symbol=order.symbol, dt=bar.dt, order_id=order.order_id
                )
            )

        return MatchResult(tuple(trades), tuple(rejects))

    # ---- 内部 ----

    @staticmethod
    def _ordered(orders: Sequence[OrderRequest]) -> list[OrderRequest]:
        """先卖后买（各自按 seq 升序）。

        A股 T 日卖出资金 T 日即可用于买入，先卖后买才能正确建模调仓。
        """
        sells = sorted((o for o in orders if o.side is Side.SELL), key=lambda o: o.seq)
        buys = sorted((o for o in orders if o.side is Side.BUY), key=lambda o: o.seq)
        return [*sells, *buys]

    def _fit_buy(self, qty: int, price: float, cash: float, lot: int) -> int:
        """把买入数量压到可用资金以内（按 lot 递减）。"""
        if cash <= 0:
            return 0
        qty = floor_to_lot(qty, lot)
        if self.insufficient_cash_policy == "reject":
            return qty if -self.cost.buy(qty, price).cash_flow <= cash + 1e-9 else 0
        while qty > 0:
            if -self.cost.buy(qty, price).cash_flow <= cash + 1e-9:
                return qty
            qty -= lot
        return 0
