"""A股交易成本模型。

**用户的真实费率（2026-09-17 确认）**：佣金**万分之一**、**免五**（无最低佣金）。

因此 ``min_commission`` 默认为 ``0.0``——不存在"单笔金额临界点"，成本率恒定。

费用计算顺序（固定，不可调换）::

    买入: 佣金 = max(金额*佣金率, 最低佣金)
          过户费 = 金额*过户费率
          现金流出 = 金额 + 佣金 + 过户费

    卖出: 佣金 = max(金额*佣金率, 最低佣金)
          印花税 = 金额*印花税率        <- 仅卖出方
          过户费 = 金额*过户费率
          现金流入 = 金额 - 佣金 - 印花税 - 过户费

往返成本率（不含滑点）= 佣金率*2 + 印花税率 + 过户费率*2
                     = 0.0001*2 + 0.0005 + 0.00001*2 = **0.072%**

**滑点模型（已定稿为"至少 1 tick"）**：成交价必须对齐 tick（股票 0.01 元）。
低价股上 0.05% 的滑点会小于半个 tick（如 4.88 元 → 0.00244 元 < 0.005），
纯取整会把它抹成 0，导致**回测系统性低估成本**。故当 ``slippage > 0`` 时，
强制成交价至少移动 **1 个 tick**。这比"低估成本"更安全——低估会让不可用的
策略看起来可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from do_modle.numeric import round_to_tick
from do_modle.objects import Side, Trade

__all__ = ["CostConfig", "FeeDetail", "AShareCostModel"]


@dataclass(frozen=True)
class CostConfig:
    """交易成本参数。默认值 = 用户实际费率。"""

    commission_rate: float = 0.0001  # 万分之一
    min_commission: float = 0.0  # 免五
    stamp_tax_rate: float = 0.0005  # 仅卖出
    transfer_rate: float = 0.00001  # 双边
    slippage: float = 0.0005
    tick_size: float = 0.01

    def __post_init__(self) -> None:
        for name in ("commission_rate", "stamp_tax_rate", "transfer_rate", "slippage"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不可为负: {getattr(self, name)}")
        if self.min_commission < 0:
            raise ValueError(f"min_commission 不可为负: {self.min_commission}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> "CostConfig":
        """从配置映射构造（忽略未知键）。"""
        raw = raw or {}
        names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: float(v) for k, v in raw.items() if k in names})  # type: ignore[arg-type]


@dataclass(frozen=True)
class FeeDetail:
    """单笔成交的费用明细。**逐项拆分**，供成本归因使用。"""

    side: Side
    quantity: int
    raw_price: float
    exec_price: float
    amount: float  # 成交金额（含滑点）
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage_cost: float
    cash_flow: float  # 买入为负，卖出为正

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee


class AShareCostModel:
    """A股成本模型。纯函数式，无状态。"""

    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()

    # ---- 基础 ----

    def apply_slippage(self, price: float, side: Side) -> float:
        """买入上滑、卖出下滑，并对齐最小变动单位。

        **滑点至少 1 个 tick**（当 ``slippage > 0``）：低价股上百分比滑点会小于
        半个 tick，纯取整会把它抹成 0。强制 1 tick 避免系统性低估成本。

        方向写反会导致回测凭空盈利。
        """
        if price <= 0:
            return 0.0
        tick = self.config.tick_size
        base = round_to_tick(price, tick)
        rate = self.config.slippage
        if rate <= 0:
            return base
        if side is Side.BUY:
            slipped = round_to_tick(base * (1 + rate), tick)
            return max(slipped, round_to_tick(base + tick, tick))
        slipped = round_to_tick(base * (1 - rate), tick)
        return min(slipped, round_to_tick(base - tick, tick))

    def slippage_cost(self, quantity: int, raw_price: float, side: Side) -> float:
        return abs(quantity * (self.apply_slippage(raw_price, side) - raw_price))

    # ---- 费用 ----

    def _commission(self, amount: float) -> float:
        return max(amount * self.config.commission_rate, self.config.min_commission)

    def buy(self, quantity: int, price: float, *, symbol: str = "", dt=None) -> FeeDetail:
        """买入费用明细。"""
        self._validate(quantity, price)
        exec_price = self.apply_slippage(price, Side.BUY)
        amount = quantity * exec_price
        commission = self._commission(amount)
        transfer_fee = amount * self.config.transfer_rate
        return FeeDetail(
            side=Side.BUY,
            quantity=quantity,
            raw_price=price,
            exec_price=exec_price,
            amount=amount,
            commission=commission,
            stamp_tax=0.0,
            transfer_fee=transfer_fee,
            slippage_cost=abs(quantity * (exec_price - price)),
            cash_flow=-(amount + commission + transfer_fee),
        )

    def sell(self, quantity: int, price: float, *, symbol: str = "", dt=None) -> FeeDetail:
        """卖出费用明细（含印花税）。"""
        self._validate(quantity, price)
        exec_price = self.apply_slippage(price, Side.SELL)
        amount = quantity * exec_price
        commission = self._commission(amount)
        stamp_tax = amount * self.config.stamp_tax_rate
        transfer_fee = amount * self.config.transfer_rate
        return FeeDetail(
            side=Side.SELL,
            quantity=quantity,
            raw_price=price,
            exec_price=exec_price,
            amount=amount,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage_cost=abs(quantity * (exec_price - price)),
            cash_flow=amount - commission - stamp_tax - transfer_fee,
        )

    # ---- 估算 ----

    def round_trip_rate(self, amount: float, *, include_slippage: bool = False) -> float:
        """往返总成本率（用于快速估算）。

        ``include_slippage=True`` 时把双边滑点计入。
        """
        if amount <= 0:
            return 0.0
        buy_fee = self._commission(amount) + amount * self.config.transfer_rate
        sell_fee = (
            self._commission(amount)
            + amount * self.config.stamp_tax_rate
            + amount * self.config.transfer_rate
        )
        rate = (buy_fee + sell_fee) / amount
        if include_slippage:
            rate += self.config.slippage * 2
        return rate

    # ---- 内部 ----

    @staticmethod
    def _validate(quantity: int, price: float) -> None:
        if quantity <= 0:
            raise ValueError(f"数量必须为正: {quantity}")
        if price <= 0:
            raise ValueError(f"价格必须为正: {price}")

    def to_trade(
        self,
        detail: FeeDetail,
        *,
        symbol: str,
        dt,
        order_id: str = "",
    ) -> Trade:
        """把 ``FeeDetail`` 转成统一 ``Trade`` 对象。"""
        return Trade(
            symbol=symbol,
            dt=dt,
            side=detail.side,
            quantity=detail.quantity,
            price=detail.exec_price,
            raw_price=detail.raw_price,
            commission=detail.commission,
            stamp_tax=detail.stamp_tax,
            transfer_fee=detail.transfer_fee,
            slippage_cost=detail.slippage_cost,
            cash_flow=detail.cash_flow,
            order_id=order_id,
        )
