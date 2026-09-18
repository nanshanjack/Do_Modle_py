"""公司行为处理：送转 + 现金分红。

阶段一**不支持配股**——检测到配股标记时抛
:class:`UnsupportedCorporateAction`，调用方应把该区间标记为不可用。

**已知简化**：现金分红按**税前**计入。个人红利税实行差异化征收
（持股 >1 年免征，1 个月~1 年减按 50% 计入，<1 个月全额计入），
且**在卖出时补缴**，与持有期挂钩。精确建模会显著增加归因复杂度，
阶段一按税前计入并在报告中显式标注。
"""

from __future__ import annotations

from do_modle.objects import CorporateAction
from do_modle.rules.position import PositionBook

__all__ = ["CorporateActionHandler", "UnsupportedCorporateAction"]


class UnsupportedCorporateAction(Exception):
    """遇到阶段一不支持的公司行为（如配股）。"""


class CorporateActionHandler:
    """把公司行为应用到持仓簿。"""

    def __init__(self, *, support_rights_issue: bool = False) -> None:
        self.support_rights_issue = support_rights_issue

    def apply(self, book: PositionBook, action: CorporateAction) -> float:
        """应用公司行为，返回**现金流入**（税前分红）。

        **顺序至关重要**：现金分红与送转股都以**股权登记日（T-1）收盘持仓**为基数。

        * 先按 **送转前** 的股数派息
        * 再按 ``1 + 送转比例`` 放大股数

        若先送转再派息，分红基数会被放大，**多派**现金（实测 13 次除权累计多派
        24.80 元）。这个 bug 由 ``scripts/validate_full.py`` 的独立重算发现。
        """
        self.validate(action)
        cash = 0.0
        if action.cash_per_share:
            cash = book.receive_cash_dividend(action.cash_per_share)
        if action.share_ratio:
            book.apply_share_ratio(action.share_ratio)
        return cash

    def validate(self, action: CorporateAction) -> None:
        """校验公司行为是否可处理。"""
        if action.rights_ratio and not self.support_rights_issue:
            raise UnsupportedCorporateAction(
                f"{action.symbol} {action.ex_date} 存在配股（比例 {action.rights_ratio}），"
                "阶段一不支持；请把该区间标记为不可用"
            )
        if action.share_ratio < -1.0:
            raise UnsupportedCorporateAction(f"送转比例非法: {action.share_ratio}")
