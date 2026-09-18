"""10 项回测陷阱自检 —— **可执行断言**，不是文档。

清单来源：``marketcalls/vectorbt-backtesting-skills`` 的 ``pitfalls.md``（MIT）。

设计原则：

* 能自动判定的 → ``PASS`` / ``FAIL``
* 需要人工判断的 → ``WARN``（给出检查项，不假装通过）
* 依赖后续阶段的（如参数敏感性）→ ``SKIP``（说明原因与归属阶段）

**只报"全部通过"而没有 SKIP 的清单，通常意味着清单本身失效。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "CheckStatus",
    "CheckResult",
    "PitfallReport",
    "run_pitfall_checks",
    "bonferroni_z",
    "PITFALL_NAMES",
]


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str

    @property
    def ok(self) -> bool:
        return self.status is not CheckStatus.FAIL


PITFALL_NAMES: tuple[str, ...] = (
    "1.前视偏差",
    "2.未来函数",
    "3.幸存者偏差",
    "4.样本内外混用",
    "5.多重检验",
    "6.成本低估",
    "7.数据窥探",
    "8.回撤期外推",
    "9.不可交易性",
    "10.参数敏感性",
)

# Bonferroni 校正后的双侧 5% 临界 z 值（按检验次数 n）
_BONFERRONI_TABLE: tuple[tuple[float, float], ...] = (
    (1, 1.960),
    (2, 2.241),
    (5, 2.576),
    (10, 2.807),
    (20, 3.023),
    (50, 3.291),
    (100, 3.481),
)


def bonferroni_z(n_trials: int) -> float:
    """多重检验校正后的显著性阈值（双侧 5%）。

    >>> bonferroni_z(1)
    1.96
    >>> bonferroni_z(10)
    2.807
    """
    if n_trials < 1:
        raise ValueError(f"n_trials 必须 >= 1，收到 {n_trials}")
    if n_trials <= _BONFERRONI_TABLE[0][0]:
        return _BONFERRONI_TABLE[0][1]
    for (n0, z0), (n1, z1) in zip(_BONFERRONI_TABLE, _BONFERRONI_TABLE[1:]):
        if n_trials <= n1:
            # 在 log(n) 上线性插值
            import math

            t = (math.log(n_trials) - math.log(n0)) / (math.log(n1) - math.log(n0))
            return round(z0 + t * (z1 - z0), 3)
    return _BONFERRONI_TABLE[-1][1]


@dataclass
class PitfallReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        self.results.append(CheckResult(name, status, detail))

    # ---- 统计 ----

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.WARN]

    @property
    def skipped(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.SKIP]

    @property
    def passed(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.PASS]

    @property
    def ok(self) -> bool:
        """无 FAIL 即通过（WARN/SKIP 需人工确认，但不阻塞）。"""
        return not self.failures

    def by_name(self, name: str) -> CheckResult | None:
        for r in self.results:
            if r.name == name:
                return r
        return None

    def format_text(self) -> str:
        lines = []
        for r in self.results:
            lines.append(f"[{r.status.value:4s}] {r.name:14s} {r.detail}")
        lines.append("-" * 60)
        lines.append(
            f"通过 {len(self.passed)} · 失败 {len(self.failures)} · "
            f"警告 {len(self.warnings)} · 跳过 {len(self.skipped)}"
        )
        return "\n".join(lines)


# ==================== 主入口 ====================


def run_pitfall_checks(
    result: Any,
    *,
    bars: Sequence[Any] | None = None,
    start: date | None = None,
    end: date | None = None,
    in_sample_end: date | str | None = None,
    n_trials: int = 1,
    cost_config: Any | None = None,
    min_span_years: float = 4.0,
) -> PitfallReport:
    """对一次回测结果跑 10 项陷阱自检。

    :param result: ``BacktestResult``
    :param bars: 区间内全部 Bar（用于核对成交价与停牌）
    :param in_sample_end: 样本内截止日；为 ``None`` 时第 4 项告警
    :param n_trials: 试过的参数/策略组合数，用于多重检验校正
    """
    rep = PitfallReport()
    bar_by_date = {b.dt.date(): b for b in bars} if bars else {}
    trades = list(result.trades)
    records = list(result.records)
    span = _span_years(records)

    # ---- 1. 前视偏差 ----
    if not bar_by_date:
        rep.add(
            PITFALL_NAMES[0],
            CheckStatus.SKIP,
            "未提供 bars，无法核对成交价与行情的一致性",
        )
    else:
        bad = [t for t in trades if t.dt.date() not in bar_by_date]
        if bad:
            rep.add(
                PITFALL_NAMES[0],
                CheckStatus.FAIL,
                f"{len(bad)} 笔成交不在行情日期上（疑似使用了区间外数据）",
            )
        else:
            rep.add(
                PITFALL_NAMES[0],
                CheckStatus.PASS,
                f"{len(trades)} 笔成交全部落在行情日期上；引擎内建 PITGuard 断言已通过",
            )

    # ---- 2. 未来函数 ----
    if not trades or not bar_by_date:
        rep.add(PITFALL_NAMES[1], CheckStatus.SKIP, "无成交或未提供 bars")
    else:
        first_date = records[0]["date"] if records else None
        same_day = [t for t in trades if first_date is not None and t.dt.date() == first_date]
        price_mismatch = [
            t
            for t in trades
            if t.dt.date() in bar_by_date
            and abs(t.raw_price - bar_by_date[t.dt.date()].open) > 1e-9
        ]
        if same_day:
            rep.add(
                PITFALL_NAMES[1],
                CheckStatus.FAIL,
                f"{len(same_day)} 笔成交发生在首个交易日（信号日=成交日）",
            )
        elif price_mismatch:
            rep.add(
                PITFALL_NAMES[1],
                CheckStatus.FAIL,
                f"{len(price_mismatch)} 笔成交价 ≠ 当日开盘价（可能用了收盘价成交）",
            )
        else:
            rep.add(
                PITFALL_NAMES[1],
                CheckStatus.PASS,
                "全部成交价 = 成交日开盘价，且无信号日当日成交",
            )

    # ---- 3. 幸存者偏差 ----
    if not bar_by_date or end is None:
        rep.add(PITFALL_NAMES[2], CheckStatus.SKIP, "未提供 bars 或 end")
    else:
        last_bar = max(bar_by_date)
        gap_days = (end - last_bar).days
        if gap_days > 30:
            rep.add(
                PITFALL_NAMES[2],
                CheckStatus.FAIL,
                f"最后一根 Bar 为 {last_bar}，距区间末 {end} 有 {gap_days} 天 —— "
                "标的可能已停牌/退市，回测区间应截断",
            )
        else:
            rep.add(
                PITFALL_NAMES[2],
                CheckStatus.PASS,
                f"最后一根 Bar {last_bar} 距区间末 {gap_days} 天（未退市）",
            )

    # ---- 4. 样本内外混用 ----
    if in_sample_end is None:
        rep.add(
            PITFALL_NAMES[3],
            CheckStatus.WARN,
            "未声明样本内截止日；无法确认样本外是否只被查看过一次",
        )
    else:
        rep.add(
            PITFALL_NAMES[3],
            CheckStatus.PASS,
            f"样本内截止 {in_sample_end}；请人工确认样本外只查看过一次",
        )

    # ---- 5. 多重检验 ----
    threshold = bonferroni_z(n_trials)
    if n_trials <= 1:
        rep.add(
            PITFALL_NAMES[4],
            CheckStatus.SKIP,
            f"仅 1 组检验（阈值 z={threshold}）；若实际试过更多组合，须传入 n_trials",
        )
    else:
        rep.add(
            PITFALL_NAMES[4],
            CheckStatus.WARN,
            f"共 {n_trials} 组检验 → 显著性阈值须提高到 z={threshold}"
            "（请用 PerfReport.relative()['t_stat_excess'] 对照）",
        )

    # ---- 6. 成本低估 ----
    cfg = cost_config
    if cfg is None:
        rep.add(PITFALL_NAMES[5], CheckStatus.SKIP, "未提供 cost_config")
    elif cfg.slippage <= 0:
        rep.add(PITFALL_NAMES[5], CheckStatus.FAIL, "滑点为 0 —— 成本必然低估")
    elif not trades:
        rep.add(PITFALL_NAMES[5], CheckStatus.SKIP, "无成交，成本无法验证")
    else:
        zero_slip = [t for t in trades if abs(t.price - t.raw_price) < 1e-12]
        if zero_slip:
            rep.add(
                PITFALL_NAMES[5],
                CheckStatus.FAIL,
                f"{len(zero_slip)}/{len(trades)} 笔成交无滑点（成交价 = 开盘价）",
            )
        else:
            rep.add(
                PITFALL_NAMES[5],
                CheckStatus.PASS,
                f"滑点 {cfg.slippage:.4%} 且全部成交含滑点；"
                f"佣金 {cfg.commission_rate:.4%}，最低佣金 {cfg.min_commission}",
            )

    # ---- 7. 数据窥探 ----
    rep.add(
        PITFALL_NAMES[6],
        CheckStatus.WARN,
        "需人工确认：因子中不得包含『已知结论』本身"
        "（BDTI / 油价已被验证无预测力，不得作为特征回填）",
    )

    # ---- 8. 回撤期外推 ----
    if span is None:
        rep.add(PITFALL_NAMES[7], CheckStatus.SKIP, "无记录，无法判断区间长度")
    elif span < min_span_years:
        rep.add(
            PITFALL_NAMES[7],
            CheckStatus.FAIL,
            f"回测区间仅 {span:.2f} 年，不足 {min_span_years} 年"
            "（油运周期约 4–6 年，须覆盖至少一个完整周期）",
        )
    else:
        rep.add(
            PITFALL_NAMES[7],
            CheckStatus.PASS,
            f"回测区间 {span:.2f} 年，覆盖至少一个完整油运周期",
        )

    # ---- 9. 不可交易性 ----
    if not bar_by_date:
        rep.add(PITFALL_NAMES[8], CheckStatus.SKIP, "未提供 bars")
    else:
        suspended_days = [d for d, b in bar_by_date.items() if b.is_suspended]
        on_suspended = [
            t
            for t in trades
            if t.dt.date() in bar_by_date and bar_by_date[t.dt.date()].is_suspended
        ]
        if on_suspended:
            rep.add(
                PITFALL_NAMES[8],
                CheckStatus.FAIL,
                f"{len(on_suspended)} 笔成交发生在停牌日",
            )
        else:
            rep.add(
                PITFALL_NAMES[8],
                CheckStatus.PASS,
                f"区间内 {len(suspended_days)} 个停牌日，无成交发生在停牌日；"
                f"拒单记录 {len(result.rejects)} 条",
            )

    # ---- 10. 参数敏感性 ----
    rep.add(
        PITFALL_NAMES[9],
        CheckStatus.SKIP,
        "归属 P7：需参数网格 + 热力图，只接受『宽稳定区』",
    )

    return rep


def _span_years(records: Sequence[Mapping[str, Any]]) -> float | None:
    if len(records) < 2:
        return None
    first, last = records[0]["date"], records[-1]["date"]
    return (last - first).days / 365.25
