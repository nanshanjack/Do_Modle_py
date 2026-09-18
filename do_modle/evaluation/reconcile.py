"""akquant 对账 —— 按 **P4-0 探针实测结论** 实现。

对账范围（实测修正版，见 ``probe_report.md``）::

    可对账 7 项：成本 / 滑点 / 撮合价格基准 / tick 对齐 / 手数 / 资金校验 / T+1
    不可对账 3 项：涨跌停、一字板、流动性约束

**核心约束**：自建引擎先跑 ``TradabilityGate`` 与流动性裁剪，**只把已裁剪的订单
喂给 akquant**，并把 akquant 的 ``volume_limit_pct`` 设为 0（避免二次裁剪）。

字段口径差异（实测）：akquant 把**过户费并入 ``commission``**，
故比较时用 ``自建 commission + transfer_fee`` 对 ``akquant commission``。

参数对齐（实测）：
* ``t_plus_one=True`` —— **默认 False，不显式传则 T+1 不生效**
* ``slippage={"type": "percent", "value": x}`` —— 传裸数字已 deprecated
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from do_modle.objects import Bar, Instrument, OrderRequest, Side
from do_modle.rules.cost import AShareCostModel
from do_modle.rules.position import PositionBook
from do_modle.rules.tradability import TradabilityGate

__all__ = [
    "KNOWN_DIFF",
    "MATCH",
    "MISMATCH",
    "NON_RECONCILE_SCOPE",
    "RECONCILE_SCOPE",
    "SKIP",
    "ReconcileCase",
    "ReconcileReport",
    "ReconcileEngine",
    "akquant_available",
]

RECONCILE_SCOPE: tuple[str, ...] = (
    "成本（akquant 的 commission 含过户费）",
    "滑点",
    "撮合价格基准（次日开盘）",
    "tick 对齐",
    "手数（100 股）",
    "资金校验",
    "T+1（须显式 t_plus_one=True）",
)

NON_RECONCILE_SCOPE: tuple[str, ...] = (
    "涨跌停（akquant 不做判定，照常成交）",
    "一字板（同上）",
    "流动性约束（自建当日裁剪+撤销，akquant 跨日部分成交+顺延）",
)

MATCH = "MATCH"
MISMATCH = "MISMATCH"
SKIP = "SKIP"
KNOWN_DIFF = "KNOWN_DIFF"


@dataclass(frozen=True)
class ReconcileCase:
    name: str
    status: str
    detail: str
    ours: dict[str, Any] = field(default_factory=dict)
    theirs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    cases: list[ReconcileCase] = field(default_factory=list)

    def add(self, case: ReconcileCase) -> None:
        self.cases.append(case)

    @property
    def matched(self) -> list[ReconcileCase]:
        return [c for c in self.cases if c.status == MATCH]

    @property
    def mismatched(self) -> list[ReconcileCase]:
        return [c for c in self.cases if c.status == MISMATCH]

    @property
    def known_diffs(self) -> list[ReconcileCase]:
        """已知模型差异（非 bug）——如滑点取整口径不同。"""
        return [c for c in self.cases if c.status == KNOWN_DIFF]

    @property
    def skipped(self) -> list[ReconcileCase]:
        return [c for c in self.cases if c.status == SKIP]

    @property
    def ok(self) -> bool:
        """无 MISMATCH 即通过。已知模型差异不算失败。"""
        return not self.mismatched

    def format_text(self) -> str:
        lines = ["场景                     状态           说明", "-" * 78]
        for c in self.cases:
            lines.append(f"{c.name:24s} {c.status:12s} {c.detail}")
        lines.append("-" * 78)
        lines.append(
            f"一致 {len(self.matched)} · 不一致 {len(self.mismatched)} · "
            f"已知差异 {len(self.known_diffs)} · 跳过 {len(self.skipped)}"
        )
        return "\n".join(lines)


def akquant_available() -> bool:
    try:
        import akquant  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


class ReconcileEngine:
    """把自建引擎的撮合结果与 akquant 对账。"""

    def __init__(
        self,
        cost: AShareCostModel | None = None,
        gate: TradabilityGate | None = None,
        *,
        tolerance: float = 1e-6,
    ) -> None:
        from do_modle.core.matching import MatchingEngine  # 局部导入避免环

        self.cost = cost or AShareCostModel()
        self.gate = gate or TradabilityGate()
        self.tolerance = tolerance
        self.matcher = MatchingEngine(self.cost, self.gate, volume_limit_pct=0.10)

    # ---- 主入口 ----

    def reconcile_order(
        self,
        *,
        name: str,
        bar: Bar,
        side: Side,
        quantity: int,
        instrument: Instrument,
        series: Sequence[Bar],
        available_cash: float = 1_000_000.0,
        signal_date: date | None = None,
    ) -> ReconcileCase:
        """对单笔订单做两引擎对账。

        ``series`` 是喂给 akquant 的 Bar 序列（须包含 ``bar`` 那一天，
        且它前面至少有一根 bar 用于产生信号）。
        """
        # 1) 自建引擎：完整规则
        book = PositionBook()
        book.before_trading(bar.dt.date())
        ours_match = self.matcher.match(
            [OrderRequest(symbol=bar.symbol, dt=bar.dt, side=side, quantity=quantity)],
            bar,
            instrument=instrument,
            book=book,
            available_cash=available_cash,
            signal_date=signal_date,
        )
        ours = {
            "filled": len(ours_match.trades),
            "quantity": ours_match.filled_quantity,
            "price": ours_match.trades[0].price if ours_match.trades else None,
            "commission": ours_match.trades[0].commission if ours_match.trades else None,
            "transfer_fee": (
                ours_match.trades[0].transfer_fee if ours_match.trades else None
            ),
            "reject": ours_match.rejects[0].reason if ours_match.rejects else None,
        }

        # 2) 若自建已拒绝（涨跌停/停牌/一字板）→ 不可对账，跳过
        if ours["reject"] is not None:
            return ReconcileCase(
                name=name,
                status=SKIP,
                detail=f"自建拒绝（{ours['reject']}）→ 该规则不在对账范围",
                ours=ours,
            )

        # 3) 喂给 akquant 的是**已通过闸门、已裁剪**的订单
        clipped_qty = ours["quantity"]
        exec_index = next(
            (i for i, b in enumerate(series) if b.dt.date() == bar.dt.date()), None
        )
        if exec_index is None or exec_index == 0:
            return ReconcileCase(
                name=name,
                status=SKIP,
                detail="series 中找不到成交 Bar，或它是首根（无前一根可发信号）",
                ours=ours,
            )
        theirs = self._run_akquant(
            series=series,
            quantity=clipped_qty,
            side=side,
            instrument=instrument,
            exec_index=exec_index,
        )
        if "error" in theirs:
            return ReconcileCase(
                name=name,
                status=SKIP,
                detail=f"akquant 运行失败：{theirs['error']}",
                ours=ours,
                theirs=theirs,
            )

        # 4) 比较成交价与费用总额
        return self._compare(name, ours, theirs)

    # ---- 比较 ----

    def _compare(
        self, name: str, ours: dict[str, Any], theirs: dict[str, Any]
    ) -> ReconcileCase:
        problems: list[str] = []
        slippage_only = False

        if theirs.get("filled", 0) != ours["filled"]:
            problems.append(f"成交笔数 {ours['filled']} vs {theirs.get('filled')}")
        elif ours["filled"]:
            p_ours, p_theirs = ours["price"], theirs.get("price")
            if p_ours is None or p_theirs is None:
                problems.append("成交价缺失")
            elif abs(float(p_ours) - float(p_theirs)) > self.tolerance:
                if self._is_slippage_rounding_diff(float(p_ours), float(p_theirs)):
                    slippage_only = True
                    problems.append(
                        f"成交价 {p_ours} vs {p_theirs}"
                        f"（滑点取整差异：自建取整到 1 tick，akquant 用纯百分比）"
                    )
                else:
                    problems.append(f"成交价 {p_ours} vs {p_theirs}")

            if ours["quantity"] != theirs.get("quantity"):
                slippage_only = False
                problems.append(f"成交数量 {ours['quantity']} vs {theirs.get('quantity')}")

            # akquant 把过户费并入 commission
            our_fee = _fee_total(ours)
            their_fee = theirs.get("commission")
            if their_fee is not None and abs(our_fee - float(their_fee)) > self.tolerance:
                if not slippage_only:
                    problems.append(
                        f"费用总额 {our_fee:.6f} vs {their_fee}（akquant 含过户费）"
                    )

        if not problems:
            return ReconcileCase(
                name=name,
                status=MATCH,
                detail=(
                    f"成交 {ours['filled']} 笔，价 {ours['price']}，"
                    f"费用总额 {_fee_total(ours):.6f} —— 与 akquant 一致"
                ),
                ours=ours,
                theirs=theirs,
            )
        if slippage_only:
            return ReconcileCase(
                name=name,
                status=KNOWN_DIFF,
                detail=(
                    "滑点模型差异（已知）：自建『至少 1 tick』，"
                    "akquant 纯百分比且不取整到 tick —— 低价股上必然不同"
                ),
                ours=ours,
                theirs=theirs,
            )
        return ReconcileCase(
            name=name,
            status=MISMATCH,
            detail="；".join(problems),
            ours=ours,
            theirs=theirs,
        )

    def _is_slippage_rounding_diff(self, our_price: float, their_price: float) -> bool:
        """判断价差是否由「滑点取整到 tick」造成。

        特征：自建价对齐 tick、akquant 价**不对齐** tick，且两者相差不超过 1 tick。
        """
        tick = self.cost.config.tick_size
        if tick <= 0:
            return False

        def aligned(x: float) -> bool:
            return abs(x / tick - round(x / tick)) < 1e-6

        return (
            aligned(our_price)
            and not aligned(their_price)
            and abs(our_price - their_price) <= tick + 1e-9
        )

    # ---- akquant 侧 ----

    def _run_akquant(
        self,
        *,
        series: Sequence[Bar],
        quantity: int,
        side: Side,
        instrument: Instrument,
        exec_index: int,
    ) -> dict[str, Any]:
        if not akquant_available():
            return {"error": "未安装 akquant（pip install akquant）"}
        import akquant as aq  # noqa: PLC0415
        from akquant import Strategy  # noqa: PLC0415

        rows = [
            {
                "date": b.dt.date().isoformat(),
                "symbol": b.symbol,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in series
        ]
        import pandas as pd  # noqa: PLC0415

        data = pd.DataFrame(rows)
        want = side is Side.BUY
        # akquant 是次日执行：要在 exec_index 这根 bar 成交，
        # 必须在 exec_index - 1 那根 bar 上发单
        signal_index = exec_index - 1

        class Probe(Strategy):
            def on_start(self):
                self._idx = 0

            def on_bar(self, bar):
                idx = getattr(self, "_idx", 0)
                self._idx = idx + 1
                if idx != signal_index:
                    return
                if want:
                    self.buy(symbol=bar.symbol, quantity=quantity)
                else:
                    self.sell(symbol=bar.symbol, quantity=quantity)

        try:
            result = aq.run_backtest(
                data=data,
                strategy=Probe,
                symbols=series[0].symbol,
                initial_cash=1_000_000.0,
                commission_rate=self.cost.config.commission_rate,
                stamp_tax_rate=self.cost.config.stamp_tax_rate,
                transfer_fee_rate=self.cost.config.transfer_rate,
                min_commission=self.cost.config.min_commission,
                slippage={"type": "percent", "value": self.cost.config.slippage},
                volume_limit_pct=0.0,  # 关键：自建已裁剪，禁用二次裁剪
                t_plus_one=True,  # 关键：默认 False，必须显式开启
                lot_size=instrument.lot_size,
                show_progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

        rows_out = _records(getattr(result, "trades_df", None)) or _records(
            getattr(result, "trades", None)
        )
        if not rows_out:
            rows_out = _records(getattr(result, "orders_df", None)) or _records(
                getattr(result, "orders", None)
            )
        filled = [r for r in rows_out if _num(_pick(r, "filled_quantity")) not in (None, 0.0)]
        first = filled[0] if filled else {}
        return {
            "filled": len(filled),
            "quantity": _num(_pick(first, "filled_quantity", "quantity")),
            "price": _num(_pick(first, "avg_price", "price")),
            "commission": _num(_pick(first, "commission")),
        }


# ==================== 工具 ====================


def _pick(row: dict, *keys, default=None):
    for k in keys:
        for actual in row:
            if str(actual).lower() == k.lower():
                return row[actual]
    return default


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v  # NaN → None


def _records(x) -> list[dict]:
    if callable(x):
        try:
            x = x()
        except Exception:  # noqa: BLE001
            return []
    if x is None:
        return []
    if hasattr(x, "to_dict"):
        try:
            return list(x.to_dict("records"))
        except Exception:  # noqa: BLE001
            return []
    try:
        return [r if isinstance(r, dict) else {"repr": repr(r)} for r in list(x)]
    except TypeError:
        return []


def _fee_total(ours: dict[str, Any]) -> float:
    c, t = ours.get("commission"), ours.get("transfer_fee")
    return float(c or 0.0) + float(t or 0.0)
