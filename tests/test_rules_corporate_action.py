"""``rules/corporate_action.py`` 单测。"""

from __future__ import annotations

import pytest

from do_modle.objects import CorporateAction
from do_modle.rules.corporate_action import (
    CorporateActionHandler,
    UnsupportedCorporateAction,
)
from do_modle.rules.position import PositionBook
from tests._helpers import D0, D1, SYMBOL


@pytest.fixture
def book() -> PositionBook:
    b = PositionBook()
    b.before_trading(D0)
    b.buy(D0, 1000, 10.0)
    return b


@pytest.fixture
def handler() -> CorporateActionHandler:
    return CorporateActionHandler()


def test_share_ratio_applied(book: PositionBook, handler: CorporateActionHandler) -> None:
    cash = handler.apply(book, CorporateAction(SYMBOL, D1, share_ratio=0.3))
    assert cash == 0.0
    assert book.total == 1300
    assert book.avg_cost == pytest.approx(10.0 / 1.3)


def test_cash_dividend_returned(book: PositionBook, handler: CorporateActionHandler) -> None:
    cash = handler.apply(book, CorporateAction(SYMBOL, D1, cash_per_share=0.5))
    assert cash == pytest.approx(500.0)
    assert book.total == 1000


def test_combined_share_and_cash_order_matters(book: PositionBook, handler: CorporateActionHandler) -> None:
    """**顺序**：先按送转前股数派息，再放大股数。

    现金分红与送转股都以**股权登记日（T-1）收盘持仓**为基数（1000 股），
    而不是送转后的 1300 股。
    """
    cash = handler.apply(
        book, CorporateAction(SYMBOL, D1, share_ratio=0.3, cash_per_share=0.2)
    )
    assert book.total == 1300
    assert cash == pytest.approx(1000 * 0.2)  # 按登记日持仓，不是 1300


def test_share_ratio_only(book: PositionBook, handler: CorporateActionHandler) -> None:
    cash = handler.apply(book, CorporateAction(SYMBOL, D1, share_ratio=0.3))
    assert book.total == 1300
    assert cash == 0.0


def test_cash_only_keeps_quantity(book: PositionBook, handler: CorporateActionHandler) -> None:
    cash = handler.apply(book, CorporateAction(SYMBOL, D1, cash_per_share=0.2))
    assert book.total == 1000
    assert cash == pytest.approx(200.0)


def test_noop_action(book: PositionBook, handler: CorporateActionHandler) -> None:
    cash = handler.apply(book, CorporateAction(SYMBOL, D1))
    assert cash == 0.0
    assert book.total == 1000


def test_rights_issue_rejected(book: PositionBook, handler: CorporateActionHandler) -> None:
    """配股阶段一不支持，必须显式报错而非静默忽略。"""
    with pytest.raises(UnsupportedCorporateAction, match="配股"):
        handler.apply(book, CorporateAction(SYMBOL, D1, rights_ratio=0.2))


def test_rights_issue_allowed_when_enabled(book: PositionBook) -> None:
    handler = CorporateActionHandler(support_rights_issue=True)
    cash = handler.apply(book, CorporateAction(SYMBOL, D1, rights_ratio=0.2))
    assert cash == 0.0


def test_negative_share_ratio_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        CorporateAction(SYMBOL, D1, share_ratio=-1.5)


def test_negative_cash_per_share_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        CorporateAction(SYMBOL, D1, cash_per_share=-0.1)


def test_capital_reduction_ratio(book: PositionBook, handler: CorporateActionHandler) -> None:
    """缩股（ratio 为负但不小于 -1）：数量减少、成本上升。"""
    handler.apply(book, CorporateAction(SYMBOL, D1, share_ratio=-0.5))
    assert book.total == 500
    assert book.avg_cost == pytest.approx(20.0)
