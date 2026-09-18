"""回测引擎（最小闭环）——编排日历、事件、撮合、账务、记录。

**核心时序**（对应设计文档 §3.1）::

    T   日 09:00  日初结转（Ledger.on_before_trading）
    T+1 日 09:30  执行 T 日产生的订单，成交价 = T+1 开盘价
    T+1 日 15:00  用 T+1 收盘出信号，挂到 T+2 执行
    T+1 日 15:05  日终结算 + 记录

即：**信号在 T 日收盘产生，订单在 T+1 开盘执行**。日单有效期为 1 个交易日；
若次日无 Bar（停牌），订单按停牌拒单处理（不跨日顺延）。

**策略与风控通过可调用对象注入**，``core`` 不反向依赖 ``strategy`` / ``execution``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol, Sequence

from do_modle.core.clock import SETTLE_TIME, Clock
from do_modle.core.event import Event, EventBus, EventType
from do_modle.core.ledger import Ledger
from do_modle.core.matching import MatchResult, MatchingEngine, RejectedOrder
from do_modle.core.recorder import Recorder
from do_modle.data.pit import PITGuard
from do_modle.data.provider import DataProvider, ProviderError
from do_modle.objects import (
    AccountSnapshot,
    AdjustFlag,
    Bar,
    CorporateAction,
    Instrument,
    OrderRequest,
    RejectReason,
    Side,
    Trade,
    Verdict,
)
from do_modle.rules.account import Account
from do_modle.rules.cost import AShareCostModel
from do_modle.rules.portfolio import Portfolio
from do_modle.rules.position import PositionBook
from do_modle.rules.tradability import TradabilityGate

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "EngineContext",
    "BarHandler",
    "PreTradeHook",
]


class BarHandler(Protocol):
    """策略入口：接收当日上下文，返回订单意图。"""

    def __call__(self, ctx: "EngineContext") -> "Sequence[OrderRequest] | None": ...


PreTradeHook = Callable[[OrderRequest, "EngineContext"], Verdict]


@dataclass(frozen=True)
class EngineContext:
    """策略在某一交易日可见的全部信息。

    ``history`` 已按 ``PITGuard`` 过滤——**策略不应也无法访问未来 Bar**。
    """

    trading_date: date
    symbol: str
    instrument: Instrument
    bar: Bar | None
    account: Account
    portfolio: Portfolio
    book: PositionBook
    prices: dict[str, float]
    last_prices: dict[str, float]
    clock: Clock
    history: tuple[Bar, ...] = ()

    # ---- 便捷查询 ----

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def available_cash(self) -> float:
        return self.account.available_cash

    @property
    def position(self) -> int:
        return self.book.total

    @property
    def sellable(self) -> int:
        return self.book.sellable

    @property
    def market_value(self) -> float:
        return self.book.total * self.last_prices.get(self.symbol, 0.0)

    @property
    def total(self) -> float:
        return self.cash + self.market_value

    def order(self, side: Side, quantity: int, *, seq: int = 0) -> OrderRequest:
        """构造订单（成交价由撮合器按次日开盘决定，故不设限价）。"""
        return OrderRequest(
            symbol=self.symbol,
            dt=self.bar.dt if self.bar else self.clock.now,
            side=side,
            quantity=quantity,
            seq=seq,
        )


@dataclass
class BacktestResult:
    """回测结果。``nav`` 与 ``snapshots`` 长度一致，逐点可比。"""

    nav: list[tuple[date, float]] = field(default_factory=list)
    snapshots: list[AccountSnapshot] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    rejects: list[RejectedOrder] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    corporate_actions: list[CorporateAction] = field(default_factory=list)
    dividend_cash: float = 0.0  # 累计税前现金分红

    @property
    def nav_values(self) -> list[float]:
        return [v for _, v in self.nav]

    @property
    def final_nav(self) -> float:
        return self.nav[-1][1] if self.nav else 0.0

    @property
    def total_fee(self) -> float:
        return sum(t.total_fee for t in self.trades)

    @property
    def reject_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.rejects:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        return counts


class BacktestEngine:
    """日频回测引擎。"""

    def __init__(
        self,
        *,
        provider: DataProvider,
        instrument: Instrument,
        symbol: str,
        handler: BarHandler,
        start: date,
        end: date,
        initial_cash: float,
        cost: AShareCostModel | None = None,
        gate: TradabilityGate | None = None,
        pit: PITGuard | None = None,
        bus: EventBus | None = None,
        pre_trade_hook: PreTradeHook | None = None,
        volume_limit_pct: float = 0.10,
        insufficient_cash_policy: str = "shrink",
        freq: str = "1d",
        apply_corporate_actions: bool = True,
    ) -> None:
        self.provider = provider
        self.instrument = instrument
        self.symbol = symbol
        self.handler = handler
        self.start = start
        self.end = end
        self.freq = freq
        self.cost = cost or AShareCostModel()
        self.gate = gate or TradabilityGate(volume_limit_pct=volume_limit_pct)
        self.pit = pit or PITGuard()
        self.bus = bus or EventBus()
        self.pre_trade_hook = pre_trade_hook
        self.apply_corporate_actions = apply_corporate_actions
        self.matching = MatchingEngine(
            self.cost,
            self.gate,
            volume_limit_pct=volume_limit_pct,
            insufficient_cash_policy=insufficient_cash_policy,  # type: ignore[arg-type]
        )
        self.portfolio = Portfolio([symbol])
        self.account = Account(initial_cash=initial_cash)
        self.ledger = Ledger(self.portfolio, self.account)
        self.recorder = Recorder()

    # ---- 主流程 ----

    def run(self) -> BacktestResult:
        bars = self.provider.get_bars(
            self.symbol, self.freq, self.start, self.end, adjust=AdjustFlag.NONE
        )
        bars_by_date: dict[date, Bar] = {b.dt.date(): b for b in bars}
        calendar = self.provider.get_calendar(self.start, self.end)
        if not calendar:
            calendar = sorted(bars_by_date)

        clock = Clock(calendar)
        result = BacktestResult()
        book = self.portfolio.book(self.symbol)

        actions_by_date = self._load_corporate_actions()

        last_prices: dict[str, float] = {}
        history: list[Bar] = []
        pending: list[OrderRequest] = []
        pending_signal_date: date | None = None
        next_exec_date: date | None = None

        while (trading_date := clock.advance()) is not None:
            as_of = clock.at(SETTLE_TIME)
            bar = bars_by_date.get(trading_date)

            self.ledger.on_before_trading(trading_date)
            self.bus.publish(Event(EventType.BEFORE_TRADING, trading_date))

            # --- 0. 除权除息（必须早于下单：T 日盘前持仓即已享有送转/分红）---
            for action in actions_by_date.get(trading_date, ()):
                cash = self.ledger.on_corporate_action(action)
                result.corporate_actions.append(action)
                result.dividend_cash += cash
                self.bus.publish(Event(EventType.CORPORATE_ACTION, action))

            # --- 1. 执行昨日订单（成交价 = 今日开盘）---
            if pending and next_exec_date == trading_date:
                if bar is None:
                    # 日单有效期为 1 个交易日：次日无 Bar（停牌）→ 拒单
                    result.rejects.extend(
                        RejectedOrder(o, RejectReason.SUSPENDED.value) for o in pending
                    )
                else:
                    match = self._execute(pending, bar, book, pending_signal_date)
                    result.trades.extend(match.trades)
                    result.rejects.extend(match.rejects)
                pending = []
                pending_signal_date = None
                next_exec_date = None

            # --- 2. 当日行情与信号 ---
            if bar is not None:
                last_prices[self.symbol] = bar.close
                history.append(bar)
                self.pit.assert_no_lookahead_bars(history, as_of)
                self.bus.publish(Event(EventType.BAR, bar))

                ctx = EngineContext(
                    trading_date=trading_date,
                    symbol=self.symbol,
                    instrument=self.instrument,
                    bar=bar,
                    account=self.account,
                    portfolio=self.portfolio,
                    book=book,
                    prices={self.symbol: bar.close},
                    last_prices=dict(last_prices),
                    clock=clock,
                    history=tuple(history),
                )
                orders, hook_rejects = self._collect_orders(ctx)
                if hook_rejects:
                    result.rejects.extend(hook_rejects)
                if orders:
                    pending = list(orders)
                    pending_signal_date = trading_date
                    next_exec_date = clock.next_date(trading_date)

            # --- 3. 结算与记录 ---
            prices = {self.symbol: last_prices.get(self.symbol, 0.0)}
            prev_closes = (
                {self.symbol: bar.prev_close}
                if bar is not None
                else {self.symbol: last_prices.get(self.symbol, 0.0)}
            )
            snapshot = self.ledger.settle(trading_date, prices, prev_closes)
            result.snapshots.append(snapshot)
            result.nav.append((trading_date, snapshot.total))

            self.bus.publish(Event(EventType.AFTER_TRADING, snapshot))
            result.records.append(
                self.recorder.snapshot(
                    trading_date,
                    cash=snapshot.cash,
                    position=book.total,
                    price=last_prices.get(self.symbol, 0.0),
                    market_value=snapshot.market_value,
                    total=snapshot.total,
                    daily_pnl=snapshot.daily_pnl,
                    cumulative_pnl=snapshot.cumulative_pnl,
                    trades=len(result.trades),
                    rejects=len(result.rejects),
                )
            )

        result.records = self.recorder.to_rows()
        self.recorder.flush()
        return result

    # ---- 内部 ----

    def _load_corporate_actions(self) -> dict[date, list[CorporateAction]]:
        """按除权日索引公司行为。"""
        if not self.apply_corporate_actions:
            return {}
        try:
            actions = self.provider.get_dividends(self.symbol, self.start, self.end)
        except (NotImplementedError, ProviderError):
            return {}
        out: dict[date, list[CorporateAction]] = {}
        for action in actions:
            out.setdefault(action.ex_date, []).append(action)
        return out

    def _collect_orders(
        self, ctx: EngineContext
    ) -> tuple[list[OrderRequest], list[RejectedOrder]]:
        """取策略订单，并过一遍 ``pre_trade_hook``（P6 的 RiskGate 从这里接入）。

        返回 ``(通过风控的订单, 被风控拒掉的订单)``——拒单必须进入结果，
        否则风控形同虚设。
        """
        orders = list(self.handler(ctx) or [])
        if self.pre_trade_hook is None:
            return orders, []
        passed: list[OrderRequest] = []
        rejects: list[RejectedOrder] = []
        for order in orders:
            verdict = self.pre_trade_hook(order, ctx)
            if verdict.passed:
                passed.append(order)
            else:
                reject = RejectedOrder(order, verdict.reason)
                rejects.append(reject)
                self.bus.publish(Event(EventType.REJECT, reject))
        return passed, rejects

    def _execute(
        self,
        orders: list[OrderRequest],
        bar: Bar,
        book: PositionBook,
        signal_date: date | None,
    ) -> MatchResult:
        match = self.matching.match(
            orders,
            bar,
            instrument=self.instrument,
            book=book,
            available_cash=self.account.available_cash,
            signal_date=signal_date,
        )
        for trade in match.trades:
            self.ledger.on_trade(trade)
            self.bus.publish(Event(EventType.TRADE, trade))
        for reject in match.rejects:
            self.bus.publish(Event(EventType.REJECT, reject))
        return match
