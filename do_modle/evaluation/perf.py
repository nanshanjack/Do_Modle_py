"""绩效指标。

**设计取舍**：核心指标**原生计算**，不依赖 quantstats。

理由：
1. 指标标准化程度高，自建约 100 行，可控且可单测
2. quantstats 会拖入 pandas + scipy + matplotlib 等重依赖，**不应进入单测路径**
3. 我们需要**相对基准**的口径（超额收益 / 信息比率 / beta / alpha），quantstats 的口径不同

quantstats 仅用于 :meth:`PerfReport.html` 生成 tearsheet（可选，未安装时给出降级提示）。

**基准是必填语义**：单标的 long-only 择时策略的天然基准是**自身 B&H**，
不是沪深300。只报绝对收益视为不合格（设计文档 §8.4）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

__all__ = ["PerfReport", "nav_from_result", "TRADING_DAYS_PER_YEAR"]

# A股一年约 243 个交易日；取 244 为常用口径（与多数库一致）
TRADING_DAYS_PER_YEAR = 244

NavPoint = tuple[date, float]


def nav_from_result(result) -> list[NavPoint]:
    """从 ``BacktestResult`` 取净值序列。"""
    return list(result.nav)


@dataclass
class PerfReport:
    """净值序列的绩效分析。

    >>> r = PerfReport([(date(2026,1,1), 100.0), (date(2026,1,2), 101.0)])
    >>> r.metrics()["total_return"]
    0.01
    """

    nav: list[NavPoint]
    benchmark: list[NavPoint] | None = None
    periods_per_year: int = TRADING_DAYS_PER_YEAR
    risk_free_rate: float = 0.0
    _cache: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if len(self.nav) < 2:
            raise ValueError(f"净值序列至少需要 2 个点，收到 {len(self.nav)}")
        values = [v for _, v in self.nav]
        if any(v <= 0 for v in values):
            raise ValueError("净值必须为正数（否则收益率无法计算）")
        if self.benchmark is not None:
            if len(self.benchmark) != len(self.nav):
                raise ValueError(
                    f"基准长度 {len(self.benchmark)} 与净值长度 {len(self.nav)} 不一致"
                )

    # ---- 基础序列 ----

    @property
    def values(self) -> list[float]:
        return [v for _, v in self.nav]

    @property
    def dates(self) -> list[date]:
        return [d for d, _ in self.nav]

    @property
    def returns(self) -> list[float]:
        """日收益率序列（长度 = n - 1）。"""
        v = self.values
        return [v[i] / v[i - 1] - 1.0 for i in range(1, len(v))]

    @property
    def benchmark_returns(self) -> list[float] | None:
        if self.benchmark is None:
            return None
        v = [x for _, x in self.benchmark]
        return [v[i] / v[i - 1] - 1.0 for i in range(1, len(v))]

    @property
    def n_periods(self) -> int:
        return len(self.nav) - 1

    # ---- 绝对指标 ----

    def metrics(self) -> dict[str, float]:
        """绝对绩效指标。"""
        if self._cache:
            return dict(self._cache)
        rets = self.returns
        v = self.values
        ppy = self.periods_per_year
        years = self.n_periods / ppy

        total_return = v[-1] / v[0] - 1.0
        annual_return = (v[-1] / v[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
        mean_r = _mean(rets)
        std_r = _std(rets, sample=True)
        annual_vol = std_r * math.sqrt(ppy)
        sharpe = (
            (annual_return - self.risk_free_rate) / annual_vol if annual_vol > 0 else 0.0
        )

        downside = [r for r in rets if r < 0]
        downside_std = _std(downside, sample=True) * math.sqrt(ppy)
        excess = annual_return - self.risk_free_rate
        if downside_std > 0:
            sortino = excess / downside_std
        else:
            # 无负收益 → 下行风险为 0。返回 inf 而非 0.0：
            # 0.0 会被误读成"很差"，与事实相反。
            sortino = math.inf if excess > 0 else 0.0

        max_dd, dd_days = _max_drawdown(v)
        calmar = annual_return / abs(max_dd) if max_dd < 0 else 0.0

        wins = [r for r in rets if r > 0]
        out: dict[str, float] = {
            "start": v[0],
            "end": v[-1],
            "days": float(self.n_periods),
            "years": years,
            "total_return": total_return,
            "annual_return": annual_return,
            "annual_volatility": annual_vol,
            "daily_mean_return": mean_r,
            "daily_std_return": std_r,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_drawdown": max_dd,
            "max_drawdown_days": float(dd_days),
            "calmar": calmar,
            "win_rate": len(wins) / len(rets) if rets else 0.0,
            "best_day": max(rets) if rets else 0.0,
            "worst_day": min(rets) if rets else 0.0,
        }
        self._cache = out
        return dict(out)

    # ---- 相对基准 ----

    def relative(self) -> dict[str, float]:
        """相对基准的指标。**未提供基准时抛异常**——不允许只报绝对收益。"""
        if self.benchmark is None:
            raise ValueError(
                "未提供基准，无法计算超额收益。单标的 long-only 策略的基准应为自身 B&H"
            )
        rets = self.returns
        bench = self.benchmark_returns
        assert bench is not None

        diff = [a - b for a, b in zip(rets, bench)]
        years = self.n_periods / self.periods_per_year
        bv = [x for _, x in self.benchmark]
        bench_total = bv[-1] / bv[0] - 1.0
        bench_annual = (bv[-1] / bv[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

        mean_diff = _mean(diff)
        std_diff = _std(diff, sample=True)
        tracking_error = std_diff * math.sqrt(self.periods_per_year)
        info_ratio = (
            mean_diff / std_diff * math.sqrt(self.periods_per_year)
            if std_diff > 0
            else 0.0
        )

        var_b = _var(bench, sample=True)
        beta = _cov(rets, bench, sample=True) / var_b if var_b > 0 else 0.0
        m = self.metrics()
        alpha = m["annual_return"] - (
            self.risk_free_rate + beta * (bench_annual - self.risk_free_rate)
        )

        # t 统计量：超额收益的显著性（独立样本近似）
        t_stat = (
            mean_diff / (std_diff / math.sqrt(self.n_periods)) if std_diff > 0 else 0.0
        )

        return {
            "benchmark_total_return": bench_total,
            "benchmark_annual_return": bench_annual,
            "excess_total_return": m["total_return"] - bench_total,
            "excess_annual_return": m["annual_return"] - bench_annual,
            "tracking_error": tracking_error,
            "information_ratio": info_ratio,
            "beta": beta,
            "alpha_annual": alpha,
            "t_stat_excess": t_stat,
        }

    # ---- 报告 ----

    def summary(self) -> dict[str, float]:
        """绝对 + 相对（有基准时）合并。"""
        out = self.metrics()
        if self.benchmark is not None:
            out.update(self.relative())
        return out

    def format_text(self) -> str:
        """人类可读的多行摘要。"""
        m = self.summary()
        lines = [
            f"区间        {self.dates[0]} ~ {self.dates[-1]}  ({m['years']:.2f} 年, {int(m['days'])} 交易日)",
            f"净值        {m['start']:,.2f} → {m['end']:,.2f}",
            f"总收益      {m['total_return']:+.2%}   年化 {m['annual_return']:+.2%}",
            f"年化波动    {m['annual_volatility']:.2%}",
            f"Sharpe      {m['sharpe']:.3f}    Sortino {m['sortino']:.3f}    Calmar {m['calmar']:.3f}",
            f"最大回撤    {m['max_drawdown']:.2%}  ({int(m['max_drawdown_days'])} 交易日)",
            f"日胜率      {m['win_rate']:.2%}    最好 {m['best_day']:+.2%} / 最差 {m['worst_day']:+.2%}",
        ]
        if self.benchmark is not None:
            lines += [
                "-" * 60,
                f"基准总收益  {m['benchmark_total_return']:+.2%}   年化 {m['benchmark_annual_return']:+.2%}",
                f"超额总收益  {m['excess_total_return']:+.2%}   年化 {m['excess_annual_return']:+.2%}",
                f"信息比率    {m['information_ratio']:.3f}   跟踪误差 {m['tracking_error']:.2%}",
                f"Beta        {m['beta']:.3f}   Alpha(年化) {m['alpha_annual']:+.2%}",
                f"超额 t 值   {m['t_stat_excess']:.3f}",
            ]
        return "\n".join(lines)

    def html(self, path: str | Path) -> Path:
        """生成 quantstats HTML tearsheet（需 ``pip install quantstats``）。

        未安装时抛 ``ImportError`` 并给出安装提示——不静默降级。
        """
        try:
            import pandas as pd  # noqa: PLC0415
            import quantstats as qs  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "生成 HTML 报告需要 quantstats：pip install quantstats"
            ) from exc

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        series = pd.Series(self.values, index=pd.to_datetime(self.dates))
        rets = series.pct_change().dropna()
        bench = None
        if self.benchmark is not None:
            bs = pd.Series([v for _, v in self.benchmark], index=pd.to_datetime(self.dates))
            bench = bs.pct_change().dropna()
        qs.reports.html(rets, benchmark=bench, output=str(out), title="do_modle")
        return out


# ==================== 内部工具（纯函数，可单测） ====================


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: Sequence[float], *, sample: bool = True) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mu = _mean(xs)
    denom = n - 1 if sample else n
    return sum((x - mu) ** 2 for x in xs) / denom


def _std(xs: Sequence[float], *, sample: bool = True) -> float:
    return math.sqrt(_var(xs, sample=sample))


def _cov(xs: Sequence[float], ys: Sequence[float], *, sample: bool = True) -> float:
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    denom = n - 1 if sample else n
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / denom


def _max_drawdown(values: Sequence[float]) -> tuple[float, int]:
    """返回 ``(最大回撤, 最长回撤持续交易日数)``。回撤为负值或 0。

    持续天数 = 从创新高那天到最后一个仍低于该高点的交易日之间的间隔。
    """
    peak = values[0]
    peak_idx = 0
    max_dd = 0.0
    dd_days = 0
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            peak_idx = i
        dd = v / peak - 1.0
        if dd < max_dd:
            max_dd = dd
        if dd < 0:
            dd_days = max(dd_days, i - peak_idx)
    return max_dd, dd_days
