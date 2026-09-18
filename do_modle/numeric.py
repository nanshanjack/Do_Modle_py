"""数值工具。

包含两类：

1. **取整**：价格对齐 tick、数量对齐手数。用 ``Decimal`` 而非 ``float``，
   避免 ``10.005 -> 10.0`` 这类二进制误差。
2. **秩统计**：``rank`` / ``spearman`` / ``pearson``。放在这里（而非
   ``evaluation``）是因为 ``strategy`` 也需要它们，而架构红线禁止
   ``strategy`` import ``evaluation``。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

__all__ = [
    "round_to_tick",
    "floor_to_lot",
    "ceil_to_lot",
    "round_half_up",
    "rank",
    "spearman",
    "pearson",
    "mean",
    "std",
]


# ==================== 取整 ====================


def round_to_tick(value: float, tick: float = 0.01) -> float:
    """把价格四舍五入到 ``tick`` 的整数倍。

    >>> round_to_tick(11.033)
    11.03
    >>> round_to_tick(10.005)
    10.01
    """
    if tick <= 0:
        return float(value)
    d = Decimal(repr(float(value)))
    t = Decimal(repr(float(tick)))
    steps = (d / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * t)


def round_half_up(value: float, digits: int = 2) -> float:
    """四舍五入到指定小数位（HALF_UP，非银行家舍入）。"""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


def floor_to_lot(quantity: float, lot: int = 100) -> int:
    """向下取整到 ``lot`` 的整数倍（用于买入数量）。

    >>> floor_to_lot(1234)
    1200
    """
    if lot <= 0:
        return int(quantity)
    return int(quantity // lot) * lot


def ceil_to_lot(quantity: float, lot: int = 100) -> int:
    """向上取整到 ``lot`` 的整数倍。"""
    if lot <= 0:
        return int(quantity)
    q = int(quantity)
    return ((q + lot - 1) // lot) * lot


# ==================== 基础统计 ====================


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def std(xs: Sequence[float], *, sample: bool = True) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    denom = n - 1 if sample else n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / denom)


# ==================== 秩统计 ====================


def rank(xs: Sequence[float]) -> list[float]:
    """平均秩（处理并列），1-based。

    >>> rank([10, 20, 20, 30])
    [1.0, 2.5, 2.5, 4.0]
    """
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson 相关。长度不等或长度 < 2 时返回 ``nan``。"""
    n = len(xs)
    if n != len(ys) or n < 2:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman 秩相关。对单调非线性变换免疫，对异常值稳健。

    >>> spearman([1, 2, 3, 4], [1, 4, 9, 16])
    1.0
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return float("nan")
    return pearson(rank(xs), rank(ys))
