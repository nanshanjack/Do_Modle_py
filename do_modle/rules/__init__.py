"""A股规则层——零外部依赖（只允许 import 标准库 + ``do_modle.objects`` + ``do_modle.numeric``）。

模块划分::

    cost.py              AShareCostModel   佣金/印花税/过户费/滑点
    position.py          PositionBook      FIFO 批次队列 + T+1
    tradability.py       TradabilityGate   涨跌停/停牌/一字板/流动性/手数
    corporate_action.py  CorporateActionHandler  送转 + 现金分红
    portfolio.py         Portfolio         多标的容器（阶段一只有 1 个 key）
    account.py           Account           资金 + 日终快照

风控、撮合、账务各自只有一处实现；本层不做撮合、不做风控决策。
"""

from __future__ import annotations

from do_modle.rules.account import Account
from do_modle.rules.corporate_action import CorporateActionHandler, UnsupportedCorporateAction
from do_modle.rules.cost import AShareCostModel, CostConfig, FeeDetail
from do_modle.rules.portfolio import Portfolio
from do_modle.rules.position import PositionBook, PositionLot, T1Violation
from do_modle.rules.tradability import LimitRuleTable, TradabilityGate

__all__ = [
    "Account",
    "AShareCostModel",
    "CorporateActionHandler",
    "CostConfig",
    "FeeDetail",
    "LimitRuleTable",
    "Portfolio",
    "PositionBook",
    "PositionLot",
    "T1Violation",
    "TradabilityGate",
    "UnsupportedCorporateAction",
]
