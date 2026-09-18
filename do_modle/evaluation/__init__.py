"""评估层——绩效指标、陷阱自检、第三方对账。

模块划分::

    perf.py        PerfReport   绩效指标（原生计算）+ quantstats HTML
    pitfalls.py    10 项陷阱清单（可执行断言）
    reconcile.py   akquant 对账（喂已裁剪订单）

**架构红线**：本层**禁止被 ``strategy`` import**（归因 ≠ 预测），
由 ``tests/test_architecture.py`` 强制。
"""

from __future__ import annotations

from do_modle.evaluation.perf import PerfReport, nav_from_result
from do_modle.evaluation.pitfalls import CheckResult, PitfallReport, run_pitfall_checks
from do_modle.evaluation.reconcile import (
    NON_RECONCILE_SCOPE,
    RECONCILE_SCOPE,
    ReconcileCase,
    ReconcileEngine,
    ReconcileReport,
    akquant_available,
)

__all__ = [
    "NON_RECONCILE_SCOPE",
    "RECONCILE_SCOPE",
    "CheckResult",
    "PerfReport",
    "PitfallReport",
    "ReconcileCase",
    "ReconcileEngine",
    "ReconcileReport",
    "akquant_available",
    "nav_from_result",
    "run_pitfall_checks",
]
