"""可交易性判定：涨跌停 / 停牌 / 一字板 / 流动性 / 手数。

判定顺序（**严格按此序，顺序错误会导致误判**）::

    1. 停牌                        -> 拒绝
    2. 涨停 且 买入                -> 拒绝（涨停不可买）
       跌停 且 卖出                -> 拒绝（跌停不可卖）
    3. 一字板 且 未处于涨跌停      -> 拒绝（流动性枯竭）
    4. 买入数量非 lot 整数倍       -> 拒绝
    5. 数量 > 成交量 * volume_limit_pct -> 裁剪到可成交量
       裁剪后为 0                  -> 拒绝

**注意涨跌停的非对称性**（这是设计文档 v1 的一处错误，此处已修正）：

* 一字板**涨停**：买盘排长队 → **卖单可以成交**，买单不可
* 一字板**跌停**：卖盘排长队 → **买单可以成交**，卖单不可
* 只有"一字板且不在涨跌停价位"才是双向流动性枯竭

涨跌停价必须用**不复权价 + prev_close** 计算；用前复权价会得到错误结果。
"""

from __future__ import annotations

from datetime import date

from do_modle.numeric import floor_to_lot, round_to_tick
from do_modle.objects import Bar, Board, Instrument, RejectReason, Side, Verdict

__all__ = ["LimitRuleTable", "TradabilityGate"]

# ---- 制度生效日（时间感知，不可写死单一比例）----

GEM_20PCT_FROM = date(2020, 8, 24)  # 创业板注册制：10% -> 20%
STAR_20PCT_FROM = date(2019, 7, 22)  # 科创板开市
BSE_30PCT_FROM = date(2021, 11, 15)  # 北交所开市
MAIN_FREE_WINDOW_FROM = date(2023, 2, 17)  # 全面注册制：主板新股前 5 日不设涨跌幅

NEW_LISTING_FREE_DAYS = 5

MAIN_RATIO = 0.10
ST_RATIO = 0.05
GEM_STAR_RATIO = 0.20
BSE_RATIO = 0.30


class LimitRuleTable:
    """涨跌幅比例表（按板块 + ST + 历史时期 + 新股窗口）。"""

    def ratio(
        self,
        instrument: Instrument,
        d: date,
        *,
        is_st: bool = False,
        listing_trading_days: int | None = None,
    ) -> float | None:
        """返回涨跌幅比例；``None`` 表示当日不设涨跌幅限制。"""
        if (
            listing_trading_days is not None
            and listing_trading_days < NEW_LISTING_FREE_DAYS
            and self.new_listing_free_applies(instrument, d)
        ):
            return None

        board = instrument.board
        if board is Board.MAIN:
            return ST_RATIO if is_st else MAIN_RATIO
        if board is Board.GEM:
            if d < GEM_20PCT_FROM:
                return ST_RATIO if is_st else MAIN_RATIO
            return GEM_STAR_RATIO
        if board is Board.STAR:
            return GEM_STAR_RATIO
        if board is Board.BSE:
            return BSE_RATIO
        raise ValueError(f"未知板块: {board}")

    def new_listing_free_applies(self, instrument: Instrument, d: date) -> bool:
        """新股上市初期是否不设涨跌幅。"""
        board = instrument.board
        if board is Board.MAIN:
            return d >= MAIN_FREE_WINDOW_FROM
        if board is Board.GEM:
            return d >= GEM_20PCT_FROM
        if board is Board.STAR:
            return d >= STAR_20PCT_FROM
        if board is Board.BSE:
            return d >= BSE_30PCT_FROM
        return False

    def limit_prices(
        self,
        instrument: Instrument,
        prev_close: float,
        d: date,
        *,
        is_st: bool = False,
        listing_trading_days: int | None = None,
    ) -> tuple[float | None, float | None]:
        """返回 ``(limit_up, limit_down)``；不设涨跌幅时返回 ``(None, None)``。"""
        ratio = self.ratio(
            instrument, d, is_st=is_st, listing_trading_days=listing_trading_days
        )
        if ratio is None or prev_close <= 0:
            return None, None
        tick = instrument.tick_size
        return (
            round_to_tick(prev_close * (1 + ratio), tick),
            round_to_tick(prev_close * (1 - ratio), tick),
        )


class TradabilityGate:
    """可交易性闸门。回测与实盘共用同一实例。"""

    def __init__(
        self,
        table: LimitRuleTable | None = None,
        volume_limit_pct: float = 0.10,
    ) -> None:
        if volume_limit_pct < 0:
            raise ValueError(f"volume_limit_pct 不可为负: {volume_limit_pct}")
        self.table = table or LimitRuleTable()
        self.volume_limit_pct = volume_limit_pct

    # ---- 涨跌停价解析 ----

    def resolve_limits(
        self,
        bar: Bar,
        instrument: Instrument,
        *,
        is_st: bool | None = None,
        listing_trading_days: int | None = None,
    ) -> tuple[float | None, float | None]:
        """优先使用 Bar 自带的涨跌停价，否则由 ``prev_close`` 计算。

        ``is_st`` 为 ``None`` 时取 ``bar.is_st``（数据源日线自带的逐日事实）。
        """
        if bar.limit_up is not None or bar.limit_down is not None:
            return bar.limit_up, bar.limit_down
        effective_st = bar.is_st if is_st is None else is_st
        return self.table.limit_prices(
            instrument,
            bar.prev_close,
            bar.dt.date(),
            is_st=effective_st,
            listing_trading_days=listing_trading_days,
        )

    # ---- 主判定 ----

    def check(
        self,
        bar: Bar,
        side: Side,
        quantity: int,
        *,
        instrument: Instrument,
        is_st: bool | None = None,
        listing_trading_days: int | None = None,
    ) -> Verdict:
        """判定订单是否可成交；可能返回被流动性裁剪后的数量。

        ``is_st`` 为 ``None`` 时取 ``bar.is_st``。
        """
        if quantity <= 0:
            return Verdict.reject(RejectReason.ILLIQUID)

        # 1. 停牌
        if bar.is_suspended:
            return Verdict.reject(RejectReason.SUSPENDED)

        limit_up, limit_down = self.resolve_limits(
            bar, instrument, is_st=is_st, listing_trading_days=listing_trading_days
        )
        at_up = limit_up is not None and bar.open >= limit_up
        at_down = limit_down is not None and bar.open <= limit_down

        # 2. 涨跌停（非对称）
        if side is Side.BUY and at_up:
            return Verdict.reject(RejectReason.LIMIT_UP)
        if side is Side.SELL and at_down:
            return Verdict.reject(RejectReason.LIMIT_DOWN)

        # 3. 一字板（排除已由涨跌停覆盖的情形）
        if bar.is_one_word_board and not at_up and not at_down:
            return Verdict.reject(RejectReason.ONE_WORD_BOARD)

        # 4. 手数：买入必须是 lot 整数倍；卖出允许零股
        lot = instrument.lot_size
        if side is Side.BUY and lot > 0 and quantity % lot != 0:
            return Verdict.reject(RejectReason.LOT_SIZE)

        # 5. 流动性裁剪
        allowed = quantity
        if self.volume_limit_pct > 0 and bar.volume > 0:
            cap = bar.volume * self.volume_limit_pct
            if cap < quantity:
                allowed = int(cap)
                if side is Side.BUY:
                    allowed = floor_to_lot(allowed, lot)
        if allowed <= 0:
            return Verdict.reject(RejectReason.ILLIQUID)
        if allowed < quantity:
            return Verdict.ok(allowed_quantity=allowed)
        return Verdict.ok()
