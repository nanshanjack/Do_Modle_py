"""A股持仓簿：用 FIFO 批次队列表达 T+1。

核心思路（借鉴 rqalpha 的 ``PositionQueue``，**代码全部自写**）：

* 每个批次 ``PositionLot`` 记录 ``(开仓日, 数量, 成本价)``
* 平仓从队首消耗 → FIFO → 天然"先平昨仓"
* ``sellable = total - today_bought - frozen`` 即 T+1 可卖量
* 日期即状态，无需额外标记"今仓"

**与 rqalpha 的差异**：本实现无全局 ``Environment`` 单例依赖，纯数据结构，
可脱离引擎独立单测。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Iterator

__all__ = ["PositionLot", "PositionBook", "T1Violation"]


class T1Violation(Exception):
    """违反 T+1 约束（当日买入当日卖出，或超量卖出）。"""


@dataclass
class PositionLot:
    """持仓批次。"""

    open_date: date
    quantity: int
    cost_price: float

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"批次数量不可为负: {self.quantity}")
        if self.cost_price < 0:
            raise ValueError(f"批次成本价不可为负: {self.cost_price}")


class PositionBook:
    """单标的持仓簿。

    使用前必须先调用 :meth:`before_trading` 设定当前交易日——``sellable``
    依赖它来判断哪些批次是"昨仓"。
    """

    def __init__(self) -> None:
        self._lots: deque[PositionLot] = deque()
        self._today: date | None = None
        self._frozen: int = 0

    # ---- 生命周期 ----

    def before_trading(self, trading_date: date) -> None:
        """日初：结转昨仓、清空冻结量。"""
        self._today = trading_date
        self._frozen = 0

    @property
    def trading_date(self) -> date | None:
        return self._today

    # ---- 查询 ----

    @property
    def total(self) -> int:
        """总持仓（股）。"""
        return sum(lot.quantity for lot in self._lots)

    @property
    def today_bought(self) -> int:
        """当日买入量（T+1 不可卖）。"""
        if self._today is None:
            return 0
        return sum(lot.quantity for lot in self._lots if lot.open_date == self._today)

    @property
    def yesterday_held(self) -> int:
        """昨仓结转量（可卖）。"""
        return self.total - self.today_bought

    @property
    def frozen(self) -> int:
        """挂单冻结量。"""
        return self._frozen

    @property
    def sellable(self) -> int:
        """T+1 可卖量 = 总量 - 当日买入 - 冻结。"""
        return self.total - self.today_bought - self._frozen

    @property
    def avg_cost(self) -> float:
        """加权平均成本（按数量加权）。空仓返回 0.0。"""
        total = self.total
        if total == 0:
            return 0.0
        return sum(lot.quantity * lot.cost_price for lot in self._lots) / total

    @property
    def total_cost(self) -> float:
        """持仓总成本（不含费用）。"""
        return sum(lot.quantity * lot.cost_price for lot in self._lots)

    def lots(self) -> tuple[PositionLot, ...]:
        """只读批次快照（队首 = 最早买入）。"""
        return tuple(self._lots)

    def __len__(self) -> int:
        return self.total

    def __iter__(self) -> Iterator[PositionLot]:
        return iter(self._lots)

    # ---- 冻结 ----

    def freeze(self, quantity: int) -> None:
        """冻结数量（挂单未成交）。"""
        if quantity < 0:
            raise ValueError(f"冻结数量不可为负: {quantity}")
        if quantity > self.total - self._frozen:
            raise ValueError(f"冻结超过持仓: 冻结 {quantity}, 可用 {self.total - self._frozen}")
        self._frozen += quantity

    def unfreeze(self, quantity: int) -> None:
        """解冻数量（撤单）。"""
        if quantity < 0:
            raise ValueError(f"解冻数量不可为负: {quantity}")
        self._frozen = max(0, self._frozen - quantity)

    # ---- 交易 ----

    def buy(self, trading_date: date, quantity: int, price: float) -> None:
        """买入。同日加仓按数量加权平均成本合并为一个批次。"""
        self._require_session()
        if quantity <= 0:
            raise ValueError(f"买入数量必须为正: {quantity}")
        if price <= 0:
            raise ValueError(f"买入价格必须为正: {price}")
        if self._lots and self._lots[-1].open_date == trading_date:
            lot = self._lots[-1]
            new_qty = lot.quantity + quantity
            lot.cost_price = (lot.quantity * lot.cost_price + quantity * price) / new_qty
            lot.quantity = new_qty
        else:
            self._lots.append(PositionLot(trading_date, quantity, price))

    def sell(
        self, trading_date: date, quantity: int, price: float
    ) -> list[tuple[date, int, float]]:
        """FIFO 卖出，返回平仓明细 ``[(开仓日, 数量, 成本价), ...]``。

        违反 T+1 时抛 :class:`T1Violation`。
        """
        self._require_session()
        if quantity <= 0:
            raise ValueError(f"卖出数量必须为正: {quantity}")
        if quantity > self.sellable:
            raise T1Violation(
                f"T+1 限制：可卖 {self.sellable}（总量 {self.total}，"
                f"今仓 {self.today_bought}，冻结 {self._frozen}），请求 {quantity}"
            )
        details: list[tuple[date, int, float]] = []
        remaining = quantity
        while remaining > 0:
            lot = self._lots[0]
            take = min(lot.quantity, remaining)
            details.append((lot.open_date, take, lot.cost_price))
            lot.quantity -= take
            remaining -= take
            if lot.quantity == 0:
                self._lots.popleft()
        return details

    # ---- 公司行为 ----

    def apply_share_ratio(self, ratio: float, expected_quantity: int | None = None) -> None:
        """送股/转增：数量按 ``1+ratio`` 放大，成本价同比例摊薄，总成本不变。

        ``expected_quantity`` 为**交易所给出的送股后总股数**。逐批次取整会产生
        误差（如 3 个批次各 round 后之和 ≠ round(总量 × 1.3)），必须用该值校正，
        差额补到最后一个批次。**此设计与 rqalpha ``handle_split`` 一致。**
        """
        if ratio == 0:
            return
        if ratio < -1:
            raise ValueError(f"送转比例非法: {ratio}")
        factor = 1.0 + ratio
        for lot in self._lots:
            lot.quantity = int(round(lot.quantity * factor))
            lot.cost_price = lot.cost_price / factor
        if expected_quantity is not None and self._lots:
            diff = int(expected_quantity) - self.total
            if diff != 0:
                self._lots[-1].quantity += diff
                if self._lots[-1].quantity < 0:
                    raise ValueError(
                        f"送转校正后批次数量为负：expected={expected_quantity}, 实际={self.total}"
                    )

    def receive_cash_dividend(self, cash_per_share: float) -> float:
        """现金分红（税前）。返回现金流入金额。"""
        if cash_per_share < 0:
            raise ValueError(f"每股分红不可为负: {cash_per_share}")
        return self.total * cash_per_share

    # ---- 盈亏 ----

    def unrealized_pnl(self, price: float) -> tuple[float, float]:
        """**累积**浮动盈亏（相对成本价），返回 ``(今仓, 昨仓)``。

        用于账户市值与总收益核算。若需要**当日**盈亏分解，用
        :meth:`daily_pnl_breakdown`。
        """
        trading = 0.0
        position = 0.0
        for lot in self._lots:
            pnl = (price - lot.cost_price) * lot.quantity
            if self._today is not None and lot.open_date == self._today:
                trading += pnl
            else:
                position += pnl
        return trading, position

    def daily_pnl_breakdown(self, price: float, prev_close: float) -> tuple[float, float]:
        """**当日**盈亏分解，返回 ``(trading_pnl, position_pnl)``。

        语义与 rqalpha 一致（已对源码核验）::

            trading_pnl  = 当日新成交部分相对最新价的盈亏
                         = sum(今仓批次: (price - cost_price) * qty)
            position_pnl = 昨仓部分相对【昨收】的盈亏
                         = 昨仓量 * (price - prev_close)

        注意 ``position_pnl`` 用的是 **prev_close（昨收）而非成本价**——这是
        "当日赚了多少"的口径，不是"相对成本赚了多少"。两者相加 = 当日总盈亏，
        对应 rqalpha 的 ``Account.daily_pnl``。
        """
        trading = 0.0
        if self._today is not None:
            for lot in self._lots:
                if lot.open_date == self._today:
                    trading += (price - lot.cost_price) * lot.quantity
        position = self.yesterday_held * (price - prev_close)
        return trading, position

    # ---- 内部 ----

    def _require_session(self) -> None:
        if self._today is None:
            raise RuntimeError("必须先调用 before_trading() 设定当前交易日")
