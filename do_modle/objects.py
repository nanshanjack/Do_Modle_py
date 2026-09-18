"""全系统共享数据契约。

设计要点：

* ``rules`` 层只依赖本模块与 ``do_modle.numeric``，**不依赖 ``data``**，
  因此规则层可以脱离数据源独立单测。
* ``Bar`` 必须自带 ``prev_close`` 与 ``limit_up/limit_down``——否则涨跌停
  判定无法工作。
* ``Instrument`` 只承载**静态**元数据；ST 状态是逐日变化的（PIT），
  由调用方按日传入，不放在 ``Instrument`` 上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

__all__ = [
    "Side",
    "Board",
    "AdjustFlag",
    "Instrument",
    "Bar",
    "Verdict",
    "OrderRequest",
    "Trade",
    "Signal",
    "AccountSnapshot",
    "CorporateAction",
    "RejectReason",
]


class Side(str, Enum):
    """买卖方向。"""

    BUY = "BUY"
    SELL = "SELL"


class Board(str, Enum):
    """板块——决定涨跌幅比例。"""

    MAIN = "MAIN"  # 主板：600/601/603/605/000/001/002/003
    GEM = "GEM"  # 创业板：300/301
    STAR = "STAR"  # 科创板：688
    BSE = "BSE"  # 北交所：8xx/43x


class AdjustFlag(str, Enum):
    """复权口径。

    **三类价格严禁混用**：

    * 因子计算用 ``HFQ``（后复权，历史不变，无前视）
    * 成交价 / 涨跌停 / 账户市值用 ``NONE``（不复权 + ``prev_close``）
    * ``QFQ`` 仅用于展示，禁止进入计算
    """

    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"


class RejectReason(str, Enum):
    """拒单原因（落库用，便于归因统计）。"""

    SUSPENDED = "SUSPENDED"
    ONE_WORD_BOARD = "ONE_WORD_BOARD"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    LOT_SIZE = "LOT_SIZE"
    ILLIQUID = "ILLIQUID"
    T1_VIOLATION = "T1_VIOLATION"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"


@dataclass(frozen=True)
class Instrument:
    """标的静态元数据。

    ``is_st`` **不在此处**——ST 是逐日变化的状态，须按日由调用方传入
    ``LimitRuleTable`` / ``TradabilityGate``。
    """

    symbol: str
    name: str
    board: Board
    list_date: date
    lot_size: int = 100
    tick_size: float = 0.01


@dataclass(frozen=True)
class Bar:
    """日线（或分钟线）行情。

    ``prev_close`` 是**不复权口径的昨收**（已除权调整），是计算涨跌停价的
    唯一正确基准。用前复权价算涨跌停会得到错误结果。

    ``is_st`` 是**逐日**事实（数据源日线自带 ``isST`` 列），因此放在 ``Bar``
    上而非 ``Instrument`` 上——ST 状态会随时间变化，属 PIT 数据。

    **估值字段（``pe_ttm`` 等）的 PIT 安全性已实证验证**：``close/pbMRQ``
    （= 每股净资产）的跳变点落在**财报公布期**而非季末 —— 2016-06-30、
    09-30、12-31 均不跳变，跳变发生在 03-24 / 04-22 / 08-29 / 10-28。
    故数据源按公布日更新，可直接用于因子（详见 ``P5_估值因子.md``）。
    """

    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: float = 0.0
    amount: float = 0.0
    freq: str = "1d"
    adjust_flag: AdjustFlag = AdjustFlag.NONE
    is_suspended: bool = False
    is_st: bool = False
    limit_up: float | None = None
    limit_down: float | None = None
    # 估值（日频，PIT 安全）。保留**原始值**，收益率化在因子层做。
    # 可能为负（亏损 → pe_ttm<0；净资产为负 → pb_mrq<0），因子须处理。
    pe_ttm: float | None = None
    pb_mrq: float | None = None
    ps_ttm: float | None = None
    pcf_ncf_ttm: float | None = None
    turnover: float | None = None
    # 业绩预告/快报（**截至本 Bar 当日已公告**的最近一次，PIT 安全）
    # 由 ``strategy.panel.attach_forecasts`` 填充，非数据源直出
    fc_score: float | None = None  # 预告类型打分（预增 +1 … 首亏 −1）
    fc_chg_pct: float | None = None  # 净利润同比变动中值（%）
    fc_age_days: float | None = None  # 距公告日的交易日间隔（信息衰减）
    # 财务质量（**截至本 Bar 当日已公布**的最新财报，PIT 安全）
    # 由 ``strategy.panel.attach_financials`` 填充，非数据源直出
    fin_roe: float | None = None  # 净资产收益率（平均）
    fin_gp_margin: float | None = None  # 毛利率
    fin_np_margin: float | None = None  # 净利率
    fin_asset_turn: float | None = None  # 总资产周转率
    fin_liab_to_asset: float | None = None  # 资产负债率
    fin_yoy_ni: float | None = None  # 净利润同比
    fin_cfo_to_or: float | None = None  # 经营现金流 / 营业收入
    fin_dupont_roe: float | None = None  # 杜邦 ROE
    # 航运运价指数（**数据日期 + 2 天可见**，见 data.pit.KIND_SHIPPING_INDEX）
    # 由 ``strategy.panel.attach_shipping`` 填充
    ship_bdt: float | None = None  # BDTI 原油运输指数
    ship_bdi: float | None = None  # BDI 波罗的海干散货
    ship_bct: float | None = None  # BCTI 成品油运输

    @property
    def is_one_word_board(self) -> bool:
        """一字板：全天只有一个价格，无法在其它价位成交。"""
        return self.high == self.low

    def at_limit_up(self) -> bool:
        return self.limit_up is not None and self.open >= self.limit_up

    def at_limit_down(self) -> bool:
        return self.limit_down is not None and self.open <= self.limit_down


@dataclass(frozen=True)
class Verdict:
    """可交易性判定结果。

    ``allowed_quantity`` 仅在 ``passed=True`` 且数量被流动性裁剪时有值。
    """

    passed: bool
    reason: str = ""
    allowed_quantity: int | None = None

    @classmethod
    def ok(cls, allowed_quantity: int | None = None) -> "Verdict":
        return cls(True, "", allowed_quantity)

    @classmethod
    def reject(cls, reason: str | RejectReason) -> "Verdict":
        return cls(False, str(getattr(reason, "value", reason)), None)


@dataclass(frozen=True)
class OrderRequest:
    """订单意图。数量必须为 100 股整数倍（由 RiskGate / MatchingEngine 校验）。"""

    symbol: str
    dt: datetime
    side: Side
    quantity: int
    limit_price: float | None = None
    order_id: str = ""
    seq: int = 0


@dataclass(frozen=True)
class Trade:
    """成交回报。费用**逐项拆分**，禁止只记总额——否则成本归因无法做。"""

    symbol: str
    dt: datetime
    side: Side
    quantity: int
    price: float  # 含滑点成交价
    raw_price: float  # 未含滑点
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage_cost: float
    cash_flow: float  # 买入为负，卖出为正
    order_id: str = ""

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee

    @property
    def gross_amount(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class Signal:
    """策略输出。只表达**目标仓位**，不表达股数——股数换算属于 PositionSizer。"""

    symbol: str
    dt: datetime
    target_weight: float  # [0, 1]，long-only
    reason: str = ""  # 归因用，不参与决策


@dataclass(frozen=True)
class AccountSnapshot:
    """日终账户快照。

    两个盈亏口径**不可混淆**（语义已对 rqalpha 源码核验）：

    * ``trading_pnl`` / ``position_pnl`` 是**当日**盈亏分解——
      ``position_pnl`` 用**昨收**而非成本价，两者相加 = ``daily_pnl``
    * ``cumulative_pnl`` 是**累积**浮动盈亏——相对成本价
    """

    dt: date
    cash: float
    frozen: float
    market_value: float
    total: float
    trading_pnl: float  # 当日新成交部分相对最新价的盈亏
    position_pnl: float  # 昨仓部分相对【昨收】的盈亏
    cumulative_pnl: float = 0.0  # 累积浮动盈亏（相对成本价）

    @property
    def daily_pnl(self) -> float:
        """当日总盈亏 = trading_pnl + position_pnl（对应 rqalpha ``Account.daily_pnl``）。"""
        return self.trading_pnl + self.position_pnl


@dataclass(frozen=True)
class Forecast:
    """业绩预告 / 业绩快报。

    **``pub_date`` 是公告日，``stat_date`` 是报告期，两者不可混用。**

    例：2016-01-23 公告「2015 年净利润同比 +450%~500%」→
    ``pub_date=2016-01-23``、``stat_date=2015-12-31``。
    用 ``stat_date`` 当可见时点就是**前视**（提前 1 个月知道全年业绩）。

    PIT 可见时点由 ``data.pit.KIND_EXPRESS_REPORT`` 定义：**公告日次日 09:00**。

    ``chg_pct_mid`` 为净利润同比变动中值（%）；预告给出上下限时取中点，
    仅给单边时取该边。``kind`` 仅预告有（预增/预减/略增/略减/首亏/扭亏/…）。
    """

    symbol: str
    pub_date: date
    stat_date: date
    source: str  # "forecast"（预告）| "express"（快报）
    kind: str = ""
    chg_pct_mid: float | None = None
    chg_pct_up: float | None = None
    chg_pct_down: float | None = None

    def __post_init__(self) -> None:
        if self.source not in ("forecast", "express"):
            raise ValueError(f"未知 source: {self.source!r}")


# 预告类型 → 打分（用于因子；越正面越大）
FORECAST_KIND_SCORE: dict[str, float] = {
    "预增": 1.0,
    "略增": 0.5,
    "续盈": 0.25,
    "扭亏": 0.75,
    "减亏": 0.25,
    "不确定": 0.0,
    "略减": -0.5,
    "预减": -1.0,
    "增亏": -0.75,
    "首亏": -1.0,
}


def forecast_kind_score(kind: str) -> float | None:
    """预告类型打分。未知类型返回 ``None``（不猜）。"""
    return FORECAST_KIND_SCORE.get(kind.strip())


@dataclass(frozen=True)
class CorporateAction:
    """公司行为：送转 + 现金分红（+ 配股标记）。

    ``share_ratio`` 为**每股送转比例**（10 送 3 → 0.3）。
    ``cash_per_share`` 为**每股税前现金分红**。
    ``rights_ratio`` 仅用于**检测配股**——阶段一不支持，非零即拒绝处理。
    """

    symbol: str
    ex_date: date
    share_ratio: float = 0.0
    cash_per_share: float = 0.0
    rights_ratio: float = 0.0

    def __post_init__(self) -> None:
        if self.share_ratio < -1.0:
            raise ValueError(f"share_ratio 非法: {self.share_ratio}")
        if self.cash_per_share < 0:
            raise ValueError(f"cash_per_share 非法: {self.cash_per_share}")
        if self.rights_ratio < 0:
            raise ValueError(f"rights_ratio 非法: {self.rights_ratio}")
