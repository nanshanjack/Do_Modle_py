"""截面 IC —— **因子评估的主指标**。

## 为什么必须用截面 IC，而不是单标的时序 IC

单标的（601872）的时序 IC 是「1 个标的的因子值序列」与「同一标的的未来收益序列」
的相关系数。问题在于：

1. **N = 1 的截面**：每个时点只有 1 个观测，无法区分「因子的横截面区分能力」
   与「这个标的这段时间恰好涨了」
2. **噪声下界**：随机因子的时序 IC 标准差约 ``1/√T``，但**有效独立样本数远小于 T**
   （日收益自相关 + 重叠窗口），实测随机因子 IC 常出现 0.1 以上的"显著"值
3. **前六轮验证的失败很可能部分源于此**——BDTI / 油价"无预测力"的结论，
   在单标的时序口径下统计功效极低，**不足以支撑否定结论**

截面 IC 的每个时点有 N 个观测（航运板块 N ≈ 12），噪声从 ``1/√1`` 降到 ``1/√N``：

| N | 随机因子 IC 的噪声下界 |
|---|---|
| 1 | 1.000 |
| 4 | 0.500 |
| 10 | 0.316 |
| 20 | 0.224 |
| 30 | 0.183 |

→ **N 必须 ≥ 10** 才真正降低噪声。

## 口径定义

* ``IC_t = corr(factor_i,t , return_i,t→t+h)``，横截面上的 Spearman 秩相关
* 主指标 **IC_IR = mean(IC) / std(IC)**（不是 IC 均值）
* ``t 统计量 = IC_IR × √T``，T 为有效 IC 观测数
* 报告必须给出 **N**（每期截面标的数）——**只报 IC 不报 N 视为不合格**

## 板块截面的固有局限

整个截面就是同一行业，**无法剔除行业 beta**。行业层面的共同涨跌会同时影响
因子值与收益，制造虚假相关。缓解手段：因子值做**横截面去均值**（等价于做空行业），
收益也做横截面去均值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from do_modle.numeric import mean as _mean
from do_modle.numeric import pearson as _pearson
from do_modle.numeric import rank
from do_modle.numeric import spearman
from do_modle.objects import Bar

__all__ = [
    "CrossSection",
    "ICSeries",
    "StabilityReport",
    "Window",
    "spearman",
    "rank",
    "demean",
    "build_cross_sections",
    "compute_ic_series",
    "filter_sections",
    "split_sections",
    "momentum_state",
    "compare_segments",
    "rolling_windows",
    "evaluate_stability",
    "report_observations",
    "build_report_sections",
    "NOISE_FLOOR",
]

# std 低于此值视为退化（等值序列的浮点残差约 1e-17）
_STD_EPSILON = 1e-12

# 随机因子的截面 IC 噪声下界 ≈ 1/sqrt(N)
NOISE_FLOOR: dict[int, float] = {
    1: 1.000,
    4: 0.500,
    10: 0.316,
    20: 0.224,
    30: 0.183,
}


def demean(xs: Sequence[float]) -> list[float]:
    """横截面去均值——缓解行业 beta 造成的虚假相关。"""
    if not xs:
        return []
    m = sum(xs) / len(xs)
    return [x - m for x in xs]


# ==================== 数据结构 ====================


@dataclass(frozen=True)
class CrossSection:
    """某一时点的横截面。

    ``factors`` 与 ``returns`` 的键必须一致（同一批标的）。
    """

    dt: date
    factors: dict[str, float]
    returns: dict[str, float]

    def __post_init__(self) -> None:
        if set(self.factors) != set(self.returns):
            raise ValueError(
                f"{self.dt} 因子与收益的标的集合不一致："
                f"{sorted(set(self.factors) ^ set(self.returns))}"
            )

    @property
    def n(self) -> int:
        return len(self.factors)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.factors)

    def ic(self, *, use_rank: bool = True, demean_values: bool = True) -> float:
        """本期截面 IC。"""
        if self.n < 3:
            return float("nan")
        syms = self.symbols
        f = [self.factors[s] for s in syms]
        r = [self.returns[s] for s in syms]
        if demean_values:
            f, r = demean(f), demean(r)
        return spearman(f, r) if use_rank else _pearson(f, r)


@dataclass
class ICSeries:
    """IC 时间序列及其统计量。"""

    values: list[tuple[date, float, int]] = field(default_factory=list)

    def add(self, dt: date, ic: float, n: int) -> None:
        self.values.append((dt, ic, n))

    @property
    def ics(self) -> list[float]:
        return [v for _, v, _ in self.values if v == v]  # 去 NaN

    @property
    def ns(self) -> list[int]:
        return [n for _, v, n in self.values if v == v]

    @property
    def n_periods(self) -> int:
        return len(self.ics)

    @property
    def mean_n(self) -> float:
        return sum(self.ns) / len(self.ns) if self.ns else 0.0

    @property
    def mean_ic(self) -> float:
        return sum(self.ics) / len(self.ics) if self.ics else float("nan")

    @property
    def std_ic(self) -> float:
        v = self.ics
        if len(v) < 2:
            return float("nan")
        m = self.mean_ic
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

    @property
    def ic_ir(self) -> float:
        """**主指标**。

        ``std`` 退化时返回 ``nan`` 而不是天文数字：等值序列的 std 会有 ~1e-17 的
        浮点残差，直接相除会得到 1e15 量级的"无穷好"IC_IR —— 那是纯粹的假象。
        """
        s = self.std_ic
        if s != s or s < _STD_EPSILON:
            return float("nan")
        return self.mean_ic / s

    @property
    def t_stat_naive(self) -> float:
        """**朴素** t 值 ``IC_IR × √T``。

        ⚠️ **这个值系统性高估显著性，不应作为判据。**

        原因：IC 序列高度自相关。实测 lag-1 自相关 **0.64~0.69**，根源是
        **前向收益窗口重叠** —— 5 日前向收益下，相邻两日的 IC 有 4/5 的
        收益区间重叠，必然强相关。

        保留此属性仅用于对照，正式判据请用 :attr:`t_stat`。
        """
        ir = self.ic_ir
        if ir != ir:
            return float("nan")
        return ir * math.sqrt(self.n_periods)

    @property
    def effective_sample_size(self) -> float:
        """自相关修正后的**有效独立样本数**。

        ``T_eff = T / (1 + 2·Σρ_k)``，自相关和用 **Bartlett 截断**
        （累加正自相关，首次转负即停）—— 这是 Newey-West 的标准做法。
        """
        v = self.ics
        T = len(v)
        if T < 10:
            return float(T)
        m = self.mean_ic
        denom = sum((x - m) ** 2 for x in v)
        if denom <= 0:
            return float(T)
        max_lag = min(T - 1, 250)
        acc = 0.0
        for k in range(1, max_lag + 1):
            num = sum((v[i] - m) * (v[i + k] - m) for i in range(T - k))
            rho = num / denom
            if rho <= 0:
                break  # Bartlett 截断
            acc += rho
        infl = 1.0 + 2.0 * acc
        return T / infl if infl > 0 else float(T)

    @property
    def t_stat(self) -> float:
        """**自相关稳健**的 t 值 ``IC_IR × √T_eff`` —— 正式判据。

        对比：``turnover_mean_20`` 的朴素 t = +6.65，修正后 **+3.30**（差 2 倍）。
        """
        ir = self.ic_ir
        if ir != ir:
            return float("nan")
        return ir * math.sqrt(self.effective_sample_size)

    @property
    def t_inflation(self) -> float:
        """朴素 t 相对修正 t 的**膨胀倍数**。>1.5 说明自相关严重。"""
        a, b = self.t_stat_naive, self.t_stat
        if a != a or b != b or b == 0:
            return float("nan")
        return abs(a / b)

    @property
    def ic_win_rate(self) -> float:
        """IC > 0 的比例。"""
        v = self.ics
        return sum(1 for x in v if x > 0) / len(v) if v else float("nan")

    @property
    def noise_floor(self) -> float:
        """随机因子在本 N 下的 IC 噪声下界（1/√N）。"""
        n = self.mean_n
        return 1.0 / math.sqrt(n) if n > 0 else float("nan")

    def summary(self) -> dict[str, float]:
        return {
            "n_periods": float(self.n_periods),
            "mean_n": self.mean_n,
            "mean_ic": self.mean_ic,
            "std_ic": self.std_ic,
            "ic_ir": self.ic_ir,
            "t_stat": self.t_stat,
            "t_stat_naive": self.t_stat_naive,
            "t_eff_n": self.effective_sample_size,
            "ic_win_rate": self.ic_win_rate,
            "noise_floor": self.noise_floor,
        }

    def format_text(self, name: str = "") -> str:
        s = self.summary()
        head = f"[{name}] " if name else ""
        return (
            f"{head}IC 期数={int(s['n_periods'])}  平均 N={s['mean_n']:.1f}\n"
            f"  mean_IC={s['mean_ic']:+.4f}  std_IC={s['std_ic']:.4f}  "
            f"IC>0 占比={s['ic_win_rate']:.1%}\n"
            f"  **IC_IR={s['ic_ir']:+.4f}**  t={s['t_stat']:+.3f}"
            f"（有效 N={s['t_eff_n']:.0f}）  "
            f"随机噪声下界={s['noise_floor']:.3f}"
        )


# ==================== 构建与计算 ====================


def build_cross_sections(
    factor_panel: Mapping[date, Mapping[str, float]],
    return_panel: Mapping[date, Mapping[str, float]],
    *,
    min_symbols: int = 5,
) -> list[CrossSection]:
    """把「日期 → {标的: 因子值}」与「日期 → {标的: 未来收益}」对齐成截面。

    只保留两边都有值、且标的数 ≥ ``min_symbols`` 的日期。
    """
    out: list[CrossSection] = []
    for dt in sorted(set(factor_panel) & set(return_panel)):
        f, r = factor_panel[dt], return_panel[dt]
        common = set(f) & set(r)
        common = {s for s in common if f[s] == f[s] and r[s] == r[s]}  # 去 NaN
        if len(common) < min_symbols:
            continue
        out.append(
            CrossSection(
                dt=dt,
                factors={s: f[s] for s in common},
                returns={s: r[s] for s in common},
            )
        )
    return out


def compute_ic_series(
    sections: Sequence[CrossSection],
    *,
    use_rank: bool = True,
    demean_values: bool = True,
) -> ICSeries:
    """逐期计算截面 IC，返回 IC 序列。"""
    series = ICSeries()
    for cs in sections:
        series.add(cs.dt, cs.ic(use_rank=use_rank, demean_values=demean_values), cs.n)
    return series


def filter_sections(
    sections: Sequence[CrossSection],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[CrossSection]:
    """按日期区间筛选截面（``start`` / ``end`` 均含端点）。"""
    out: list[CrossSection] = []
    for cs in sections:
        if start is not None and cs.dt < start:
            continue
        if end is not None and cs.dt > end:
            continue
        out.append(cs)
    return out


def split_sections(
    sections: Sequence[CrossSection],
    state: Mapping[date, str],
) -> dict[str, list[CrossSection]]:
    """按**市场状态**把截面分组。

    ``state`` 为 ``{日期: 状态名}``（如 ``{"up": ..., "down": ...}``）。
    日期不在 ``state`` 中的截面会被丢弃。
    """
    out: dict[str, list[CrossSection]] = {}
    for cs in sections:
        key = state.get(cs.dt)
        if key is None:
            continue
        out.setdefault(key, []).append(cs)
    return out


def momentum_state(
    bars: Sequence[Bar],
    lookback: int = 60,
) -> dict[date, str]:
    """由某标的的自身动量生成市场状态：``close/close_{-lookback} - 1`` 正/负。

    返回 ``{日期: "up" | "down"}``。用于检验因子在不同市况下的稳健性——
    **若因子只在单边市况下有效，视为不可用**。
    """
    ordered = sorted(bars, key=lambda b: b.dt)
    out: dict[date, str] = {}
    for i in range(lookback, len(ordered)):
        base = ordered[i - lookback].close
        if base <= 0:
            continue
        out[ordered[i].dt.date()] = "up" if ordered[i].close > base else "down"
    return out


def report_observations(bars: Sequence[Bar], field: str = "fin_roe") -> list[date]:
    """找出**财报值发生变化的那些 Bar 的日期** = 新财报首次可见的交易日。

    这是按报告期采样的基础：每个变化点对应一次新的财报公布，
    对应一个独立的观测。相邻观测间隔约一个季度。
    """
    out: list[date] = []
    prev: float | None = None
    for b in bars:
        v = getattr(b, field, None)
        if v is not None and v != prev:
            out.append(b.dt.date())
        prev = v
    return out


def build_report_sections(
    factor_panel: Mapping[date, Mapping[str, float]],
    return_panel: Mapping[date, Mapping[str, float]],
    obs_dates_by_symbol: Mapping[str, Sequence[date]],
    *,
    min_symbols: int = 5,
) -> list[CrossSection]:
    """按**报告期**构建截面 —— 每个标的每季**只取一个观测**。

    ## 为什么必须这样做

    财报因子值在两次公告之间**完全不变**。若按日频算截面 IC：

    * 同一份财报被重复计入约 **60 次**
    * IC 序列 lag-1 自相关可达 **0.95+**
    * 朴素 t 值虚高 **10 倍以上**（有效样本 ≈ 期数/60）

    按报告期采样后，每个观测对应一次**独立的信息事件**，自相关问题消除。

    ## 实现

    ``obs_dates_by_symbol`` 由 :func:`report_observations` 生成
    （财报值变化的日期）。按自然季度分组，同季度的观测构成一个截面。

    截面代表日取该季度内最早观测日（仅用于排序与展示）。
    """
    by_period: dict[str, dict[str, tuple[float, float]]] = {}
    period_rep: dict[str, date] = {}

    for sym, dates in obs_dates_by_symbol.items():
        for d in dates:
            period = f"{d.year}Q{(d.month - 1) // 3 + 1}"
            fv = factor_panel.get(d, {}).get(sym)
            rv = return_panel.get(d, {}).get(sym)
            if fv is None or rv is None:
                continue
            by_period.setdefault(period, {})[sym] = (fv, rv)
            if period not in period_rep or d < period_rep[period]:
                period_rep[period] = d

    out: list[CrossSection] = []
    for period in sorted(by_period):
        per = by_period[period]
        if len(per) < min_symbols:
            continue
        out.append(
            CrossSection(
                dt=period_rep[period],
                factors={s: v[0] for s, v in per.items()},
                returns={s: v[1] for s, v in per.items()},
            )
        )
    return out


def compare_segments(
    segments: Mapping[str, Sequence[CrossSection]],
    *,
    use_rank: bool = True,
) -> dict[str, ICSeries]:
    """对多组截面分别算 IC 序列。"""
    return {
        name: compute_ic_series(secs, use_rank=use_rank)
        for name, secs in segments.items()
    }


# ==================== Walk-Forward 滚动窗口 ====================


@dataclass(frozen=True)
class Window:
    label: str
    start: date
    end: date

    def __str__(self) -> str:
        return f"{self.label}({self.start}~{self.end})"


def rolling_windows(
    start: date,
    end: date,
    *,
    window_days: int = 730,
    step_days: int = 365,
) -> list[Window]:
    """生成**重叠的滚动窗口**，用于检验因子在不同时期的稳定性。

    单次切分（样本内/外）只有 1 个观测，统计功效低；滚动窗口提供多个独立观测。
    """
    if window_days < 60:
        raise ValueError(f"window_days 过小（{window_days}），至少 60")
    if step_days < 1:
        raise ValueError(f"step_days 必须 >= 1，收到 {step_days}")
    from datetime import timedelta

    out: list[Window] = []
    s = start
    while True:
        e = min(s + timedelta(days=window_days), end)
        if (e - s).days < window_days * 0.6:
            break  # 尾部残窗过短，丢弃
        out.append(Window(f"W{len(out) + 1}", s, e))
        if e >= end:
            break
        s = s + timedelta(days=step_days)
    return out


@dataclass
class StabilityReport:
    """因子在滚动窗口上的稳定性。"""

    windows: list[Window] = field(default_factory=list)
    ic_irs: list[float] = field(default_factory=list)

    def add(self, window: Window, ic_ir: float) -> None:
        self.windows.append(window)
        self.ic_irs.append(ic_ir)

    @property
    def valid(self) -> list[float]:
        return [v for v in self.ic_irs if v == v]

    @property
    def n_windows(self) -> int:
        return len(self.valid)

    @property
    def sign_consistency(self) -> float:
        """与全期中位符号一致的比例。"""
        v = self.valid
        if not v:
            return float("nan")
        ref = _median(v)
        if ref == 0:
            return float("nan")
        pos = ref > 0
        return sum(1 for x in v if (x > 0) == pos) / len(v)

    @property
    def min_abs_ic_ir(self) -> float:
        """**最差窗口**的 |IC_IR| —— 决定因子能否在坏时期存活。"""
        v = self.valid
        return min(abs(x) for x in v) if v else float("nan")

    @property
    def worst_window(self) -> str:
        v = self.ic_irs
        if not v:
            return "—"
        idx = min(
            (i for i in range(len(v)) if v[i] == v[i]),
            key=lambda i: abs(v[i]),
            default=-1,
        )
        return str(self.windows[idx]) if idx >= 0 else "—"

    def format_text(self, name: str = "") -> str:
        head = f"[{name}] " if name else ""
        return (
            f"{head}窗口数={self.n_windows}  "
            f"符号一致率={self.sign_consistency:.0%}  "
            f"最差窗口|IC_IR|={self.min_abs_ic_ir:.4f} ({self.worst_window})"
        )


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return float("nan")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def evaluate_stability(
    sections: Sequence[CrossSection],
    windows: Sequence[Window],
    *,
    use_rank: bool = True,
) -> StabilityReport:
    """在每个滚动窗口上算 IC_IR，返回稳定性报告。"""
    rep = StabilityReport()
    for w in windows:
        sub = filter_sections(sections, start=w.start, end=w.end)
        series = compute_ic_series(sub, use_rank=use_rank)
        rep.add(w, series.ic_ir)
    return rep
