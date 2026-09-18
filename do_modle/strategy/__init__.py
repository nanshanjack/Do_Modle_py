"""策略层——因子、合成、面板。

模块划分::

    factors.py    因子定义（纯函数，PIT 安全）
    panel.py      从缓存构建因子面板 / 收益面板
    composite.py  多因子合成（横截面标准化 + 等权）

**架构红线**：``strategy`` **不得 import ``evaluation``**（归因 ≠ 预测）。
因子评估在 ``evaluation/xsec.py`` 中做，由 ``tests/test_architecture.py`` 强制。
共用的秩统计已下沉到 ``numeric.py``。
"""

from __future__ import annotations

from do_modle.strategy.composite import (
    combine_panels,
    combine_ranks,
    panel_correlation,
    rank_cross_section,
    zscore_cross_section,
)
from do_modle.strategy.factors import FACTORS, FactorSpec, compute_factor, factor_categories
from do_modle.strategy.panel import build_factor_panel, build_return_panel

__all__ = [
    "FACTORS",
    "FactorSpec",
    "build_factor_panel",
    "build_return_panel",
    "combine_panels",
    "combine_ranks",
    "compute_factor",
    "factor_categories",
    "panel_correlation",
    "rank_cross_section",
    "zscore_cross_section",
]
