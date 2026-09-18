"""``rules/cost.py`` 单测 —— 覆盖用户实际费率（万五 + 免五）。"""

from __future__ import annotations

import pytest

from do_modle.objects import Side
from do_modle.rules.cost import AShareCostModel, CostConfig

PRICE = 20.0  # 选 20.0 使滑点取整无歧义：20 * 1.0005 = 20.01


@pytest.fixture
def model() -> AShareCostModel:
    return AShareCostModel()


# ---- 1. 用户费率前提 ----

def test_default_config_matches_user_rates() -> None:
    """默认配置必须是用户实际费率：万分之一 + 免五。"""
    cfg = CostConfig()
    assert cfg.commission_rate == 0.0001
    assert cfg.min_commission == 0.0
    assert cfg.stamp_tax_rate == 0.0005
    assert cfg.transfer_rate == 0.00001


def test_no_minimum_commission_on_small_order(model: AShareCostModel) -> None:
    """免五：小额订单佣金按比例收取，不触发 5 元最低值。"""
    detail = model.buy(100, PRICE)  # 成交金额 2001 元
    assert detail.commission == pytest.approx(2001.0 * 0.0001)
    assert detail.commission < 5.0


def test_minimum_commission_still_configurable() -> None:
    """最低佣金可配置——配置为 5 元时应触底（保留与券商口径的兼容性）。"""
    model = AShareCostModel(CostConfig(min_commission=5.0))
    detail = model.buy(100, PRICE)
    assert detail.commission == pytest.approx(5.0)


# ---- 2. 滑点方向与"至少 1 tick"模型 ----

def test_slippage_direction_buy_up_sell_down(model: AShareCostModel) -> None:
    """买入上滑、卖出下滑——方向写反会让回测凭空盈利。"""
    assert model.apply_slippage(PRICE, Side.BUY) > PRICE
    assert model.apply_slippage(PRICE, Side.SELL) < PRICE
    assert model.apply_slippage(PRICE, Side.BUY) == pytest.approx(20.01)
    assert model.apply_slippage(PRICE, Side.SELL) == pytest.approx(19.99)


def test_slippage_at_least_one_tick_on_low_price(model: AShareCostModel) -> None:
    """**模型 B**：低价股上百分比滑点小于半个 tick，必须强制 1 tick。

    4.88 × 0.05% = 0.00244 元 < 半个 tick(0.005) —— 纯取整会抹成 0，
    导致 2016–2020（股价 3–10 元）期间系统性低估成本。
    """
    assert model.apply_slippage(4.88, Side.BUY) == pytest.approx(4.89)
    assert model.apply_slippage(4.88, Side.SELL) == pytest.approx(4.87)


def test_slippage_one_tick_at_very_low_price(model: AShareCostModel) -> None:
    assert model.apply_slippage(3.30, Side.BUY) == pytest.approx(3.31)
    assert model.apply_slippage(3.30, Side.SELL) == pytest.approx(3.29)


def test_slippage_percentage_wins_when_larger_than_one_tick() -> None:
    """百分比滑点大于 1 tick 时取百分比结果，不会被 1 tick 上限截断。"""
    model = AShareCostModel(CostConfig(slippage=0.01))  # 1%
    assert model.apply_slippage(20.0, Side.BUY) == pytest.approx(20.20)
    assert model.apply_slippage(20.0, Side.SELL) == pytest.approx(19.80)


def test_zero_slippage_gives_tick_aligned_price(model: AShareCostModel) -> None:
    """``slippage = 0`` 时不做任何偏移（等价于旧模型 A）。"""
    zero = AShareCostModel(CostConfig(slippage=0.0))
    assert zero.apply_slippage(4.88, Side.BUY) == pytest.approx(4.88)
    assert zero.apply_slippage(4.88, Side.SELL) == pytest.approx(4.88)


def test_slippage_cost_is_recorded(model: AShareCostModel) -> None:
    detail = model.buy(1000, PRICE)
    assert detail.slippage_cost == pytest.approx(1000 * 0.01)


# ---- 3. 买入费用 ----

def test_buy_fee_breakdown(model: AShareCostModel) -> None:
    detail = model.buy(1000, PRICE)
    assert detail.exec_price == pytest.approx(20.01)
    assert detail.amount == pytest.approx(20010.0)
    assert detail.commission == pytest.approx(2.001)
    assert detail.transfer_fee == pytest.approx(0.2001)
    assert detail.cash_flow == pytest.approx(-20012.2011)


def test_buy_has_no_stamp_tax(model: AShareCostModel) -> None:
    """印花税仅卖出方征收。"""
    assert model.buy(1000, PRICE).stamp_tax == 0.0


# ---- 4. 卖出费用 ----

def test_sell_fee_breakdown(model: AShareCostModel) -> None:
    detail = model.sell(1000, PRICE)
    assert detail.exec_price == pytest.approx(19.99)
    assert detail.amount == pytest.approx(19990.0)
    assert detail.commission == pytest.approx(1.999)
    assert detail.stamp_tax == pytest.approx(9.995)
    assert detail.transfer_fee == pytest.approx(0.1999)
    assert detail.cash_flow == pytest.approx(19977.8061)


def test_sell_cash_flow_is_positive(model: AShareCostModel) -> None:
    assert model.sell(1000, PRICE).cash_flow > 0


def test_sell_cost_exceeds_buy_cost_for_same_amount(model: AShareCostModel) -> None:
    """卖出一侧多一道印花税，总费用必然高于买入。"""
    assert model.sell(1000, PRICE).total_fee > model.buy(1000, PRICE).total_fee


# ---- 5. 往返成本率 ----

def test_round_trip_rate_excluding_slippage(model: AShareCostModel) -> None:
    """往返成本率（不含滑点）= 0.0001*2 + 0.0005 + 0.00001*2 = 0.072%"""
    assert model.round_trip_rate(200_000) == pytest.approx(0.00072)


def test_round_trip_rate_including_slippage(model: AShareCostModel) -> None:
    """含双边滑点 0.05%*2 → 0.172%"""
    assert model.round_trip_rate(200_000, include_slippage=True) == pytest.approx(0.00172)


def test_round_trip_rate_is_amount_independent_when_no_min_commission(
    model: AShareCostModel,
) -> None:
    """免五后不存在单笔金额临界点：大小单成本率相同。"""
    small = model.round_trip_rate(2_000)
    large = model.round_trip_rate(2_000_000)
    assert small == pytest.approx(large)


def test_round_trip_rate_zero_amount(model: AShareCostModel) -> None:
    assert model.round_trip_rate(0) == 0.0


# ---- 6. 参数校验 ----

@pytest.mark.parametrize("quantity", [0, -100])
def test_buy_rejects_non_positive_quantity(model: AShareCostModel, quantity: int) -> None:
    with pytest.raises(ValueError):
        model.buy(quantity, PRICE)


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_sell_rejects_non_positive_price(model: AShareCostModel, price: float) -> None:
    with pytest.raises(ValueError):
        model.sell(100, price)


def test_negative_rate_rejected() -> None:
    with pytest.raises(ValueError):
        CostConfig(commission_rate=-0.001)


def test_from_mapping_ignores_unknown_keys() -> None:
    cfg = CostConfig.from_mapping({"commission_rate": 0.0003, "unknown": 123})
    assert cfg.commission_rate == 0.0003
    assert cfg.min_commission == 0.0


# ---- 7. 与 Trade 的衔接 ----

def test_to_trade_preserves_fee_breakdown(model: AShareCostModel) -> None:
    from datetime import datetime

    detail = model.sell(1000, PRICE)
    trade = model.to_trade(detail, symbol="sh.601872", dt=datetime(2026, 9, 15, 9, 30))
    assert trade.side is Side.SELL
    assert trade.quantity == 1000
    assert trade.stamp_tax == pytest.approx(9.995)
    assert trade.total_fee == pytest.approx(detail.total_fee)
    assert trade.cash_flow == pytest.approx(detail.cash_flow)
