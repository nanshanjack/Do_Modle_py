# A股量化交易系统 · 实现设计（复用 / 改进 / 新建三分类）

> 上游文档：`开源项目可复用模块分析.md`、`A股量化交易系统_架构设计.md`
> 本文回答：**哪些直接用、哪些改、哪些自己写**，以及**怎么落地成代码**
> 阶段一标的：601872.SH 招商轮船

---

## 一、三分类总表

| # | 能力 | 分类 | 具体动作 | 依据 |
|---|---|---|---|---|
| 1 | 绩效指标与 tearsheet | **① 复用** | `pip install quantstats`，零改造封装 | Apache-2.0，标准化程度高，自建无收益 |
| 2 | 事件引擎模型 | **① 复用** | `pip install vnpy`，用其 `EventEngine` 思路；不引入其 GUI/数据库模块 | MIT |
| 3 | 统一数据对象 | **① 复用** | 参照 vnpy `object.py` 的字段设计（`TickData/OrderData/TradeData`） | MIT |
| 4 | 回测方法论 | **① 复用** | 直接采用 vectorbt-skills 的 `pitfalls.md` / `robustness-testing.md` / `walk-forward.md` / `parameter-optimization.md` | MIT，纯知识 |
| 5 | 行情数据源 | **① 复用** | `akshare` / `baostock` / `tushare`（第三方库，非本报告 6 项之内，但必需） | 均为宽松许可 |
| 6 | A股 T+1 持仓队列 | **② 改进** | 重写为 `PositionBook`（丢弃 rqalpha 的 `Environment` 单例依赖） | rqalpha 商业许可受限 + 只需股票 |
| 7 | 账户与盈亏拆分 | **② 改进** | 重写 `Account`，保留 `trading_pnl / position_pnl` 的**区分口径** | 同上 |
| 8 | 送股/拆股处理 | **② 改进** | 重写为 `CorporateActionHandler`，仅保留送转/分红 | 同上 |
| 9 | 事件类型设计 | **② 改进** | 照抄事件**语义**（`BEFORE_TRADING/BAR/AFTER_TRADING`），实现自写 | 参考设计 |
| 10 | Mod 可插拔机制 | **② 改进** | 简化为 `DataProvider` 注册表（`dict[str, type]`） | 参考设计 |
| 11 | 多周期 K 线合成 | **② 改进** | 借鉴 vnpy `BarGenerator`，但必须适配 A股**非连续交易时段**（11:30–13:00 休市） | 直接抄会错 |
| 12 | 因子定义 | **② 改进** | 参考 qlib Alpha158 的**定义口径**，实现全部自写 | MIT 但数据格式专有 |
| 13 | IC / 评估口径 | **② 改进** | 参考 qlib `contrib/evaluate`；单标的改为**时序 IC** | 同上 |
| 14 | 增值税/附加税 | **② 改进** | 接口预留，**阶段一不实现**（个人账户无此项） | 机构口径才需要 |
| 15 | A股规则核心（可卖量/涨跌停/停牌） | **③ 新建** | 无现成可依赖项 | — |
| 16 | **PIT 时点守卫** | **③ 新建** | 防前视的架构级组件 | 无现成可依赖项 |
| 17 | 风控闸门 `RiskGate` | **③ 新建** | 回测/实盘共用 | 个性化 |
| 18 | 归因 `Attribution` | **③ 新建** | 与预测物理隔离 | 个性化 + 原报告"归因≠预测" |
| 19 | 状态记录 `Recorder` | **③ 新建** | 逐 bar 快照，供事后复盘 | — |
| 20 | 成本模型 | **③ 新建** | 结构借鉴 vectorbt-skills，参数按 A股自填 | 报告已给完整实现 |
| 21 | CLI / 每日任务编排 | **③ 新建** | — | — |
| 22 | 实盘网关 | **① 复用（延后）** | Phase 5 接 vnpy gateway 或券商 miniQMT | 自建成本极高 |

### 分类的判断准则（可复用）

1. **许可干净 + 无需改造 → 复用**（①）
2. **许可干净但耦合重 / 许可受限 → 改进（重写）**（②）
3. **个性化强 或 直接决定回测可信度 → 新建**（③）

> **③ 新建清单里 16 号 `PITGuard` 是本项目最容易被低估、代价最高的组件。** 前六轮验证的失败很可能部分源于此，而非因子本身。

---

## 二、逐项详解

### 2.1 ① 复用项（4 个）

| 项 | 用法 | 注意 |
|---|---|---|
| quantstats | `qs.reports.html(returns, benchmark=bh_returns)` | **必须同时传入 B&H 基准**，否则单看绝对收益无意义 |
| vnpy | 只 import `vnpy.event` / `vnpy.trader.object` 的**设计**；**不要装全套** | vnpy 全套依赖 vnpy_gui/数据库，纯回测用不上，会污染环境 |
| vectorbt-skills | 作为**清单**使用：`evaluation/pitfalls.py` 把 10 项硬编码为可执行断言 | 纯文档，无代码可复用 |
| 数据源库 | 统一在 `data/adapters/` 下适配 | **akshare 接口不稳定**，必须锁定版本 + 写适配层 |

> ⚠️ **vnpy 的使用边界**：原报告写"直接依赖 vnpy"，但 vnpy 是**实盘框架**，其价值集中在 gateway。**阶段一~四只需借鉴其事件模型与数据对象设计，不需要真正安装 vnpy**。把它推迟到 Phase 5 才是正确用法，否则会引入大量无用依赖。这是对原报告的一处修正。

### 2.2 ② 改进项（9 个）

| 项 | 原实现的问题 | 改进方案 |
|---|---|---|
| `PositionQueue` → `PositionBook` | 依赖 `Environment` 全局单例；支持期货/期权 | 纯数据结构，零全局状态；只支持股票；`sell()` 内置 T+1 校验 |
| `Account` → `Account` | 同上 | 保留 `trading_pnl / position_pnl` 双口径，去掉融资融券字段 |
| `handle_split` → `CorporateActionHandler` | 与 Instrument 强耦合 | 只处理送转/分红，输入为 `(ratio, cash_per_share)` |
| EVENT 设计 → `EventBus` | rqalpha 的引擎与数据层耦合 | 只做 pub/sub + 类型常量，不含业务 |
| Mod 机制 → Provider 注册表 | 完整 Mod 系统过重 | `PROVIDERS: dict[str, type[DataProvider]]` |
| `BarGenerator` | 假设交易时段连续 | **A股必须显式建模 9:30–11:30 / 13:00–15:00 两段**，跨午休不可合成 |
| Alpha158 → 因子库 | 数据格式专有；含大量横截面因子 | 只取**时序可用**部分；`cross_sectional` 字段标记其余 |
| qlib evaluate → 时序 IC | 单标的无横截面 | 改为 `spearman(factor_t, forward_return_{t+1..t+n})` |
| `CapitalGainsTaxMixin` | 机构口径 | **阶段一不实现**，只留 `TaxModel` 空接口 |

### 2.3 ③ 新建项（8 个）—— 按实现难度排序

| 项 | 难度 | 说明 |
|---|---|---|
| `AShareCostModel` | ★ | 公式明确，报告已给完整实现 |
| `PositionBook` | ★★ | FIFO + 日期批次，逻辑清晰但边界多（部分平仓/同日加仓/跨除权） |
| `TradabilityGate` | ★★ | 涨跌停判定需正确取 `limit_up/limit_down`，一字板需 `high==low` |
| `Account` / `Portfolio` | ★★ | 需处理冻结资金、T+1 冻结持仓 |
| `MatchingEngine` | ★★★ | 撮合顺序、部分成交、滑点方向（买入上滑/卖出下滑） |
| `RiskGate` | ★★ | 规则可插拔，需记录拒绝原因 |
| **`PITGuard`** | ★★★★ | **最难**：需要为每类数据定义"可见时点"，运价/财报各有滞后 |
| `Attribution` | ★★★ | 需收益分解模型，且不得反向影响信号 |

---

## 三、代码实现设计

### 3.1 包结构与依赖方向

```
do_modle/
├─ config/
│   ├─ base.yaml                # 全局：成本参数、风控阈值、数据源
│   └─ stock_601872.yaml        # 标的相关
├─ data/                        # 无内部依赖（最底层）
│   ├─ objects.py               # Bar / Quote / Instrument（数据结构）
│   ├─ provider.py              # DataProvider 抽象 + PROVIDERS 注册表
│   ├─ adapters/
│   │   ├─ akshare_src.py
│   │   ├─ baostock_src.py
│   │   └─ local_dump.py
│   ├─ cache.py                 # DataCache（Parquet 分区）
│   └─ pit.py                   # PITGuard
├─ rules/                       # 仅依赖 data/objects.py
│   ├─ cost.py                  # AShareCostModel
│   ├─ position.py              # PositionLot / PositionBook
│   ├─ portfolio.py             # Portfolio（多标的容器）
│   ├─ tradability.py           # TradabilityGate
│   ├─ corporate_action.py      # CorporateActionHandler
│   └─ account.py               # Account
├─ core/                        # 依赖 data/ + rules/
│   ├─ event.py                 # EventBus / EventType
│   ├─ clock.py                 # Clock
│   ├─ matching.py              # MatchingEngine
│   ├─ ledger.py                # Ledger
│   └─ recorder.py              # Recorder
├─ strategy/                    # 依赖 data/ + rules/ + core/
│   ├─ base.py                  # Strategy 抽象
│   ├─ factors/
│   │   ├─ registry.py          # FactorRegistry
│   │   └─ library.py           # 因子实现
│   ├─ signals.py               # SignalGenerator
│   └─ sizing.py                # PositionSizer
├─ execution/                   # 依赖 core/ + rules/
│   ├─ risk.py                  # RiskGate + RiskRule
│   ├─ oms.py                   # OrderManager
│   └─ gateways/
│       ├─ base.py              # ExecutionGateway 抽象
│       ├─ backtest.py
│       └─ live.py              # Phase 5
├─ evaluation/                  # 依赖 core/（禁止被 strategy 引用）
│   ├─ perf.py                  # quantstats 封装
│   ├─ attribution.py
│   ├─ walkforward.py
│   ├─ montecarlo.py
│   └─ pitfalls.py              # 10 项自检
├─ app/
│   ├─ cli.py
│   └─ daily_job.py
└─ tests/
    ├─ test_rules_*.py
    ├─ test_pit.py
    └─ test_architecture.py     # ★ 依赖方向自动校验
```

**依赖方向（单向，严格禁止反向）：**

```
data ──► rules ──► core ──► strategy ──► app
                     │          │
                     └──► execution ──► app
                     └──► evaluation ──► app
```

**四条 import 禁令（写进 `test_architecture.py` 自动断言）：**

| 禁令 | 理由 |
|---|---|
| `rules` 不得 import `data`（除 `data/objects.py`） | 规则层只接收已解析数值，保证可独立单测 |
| `rules` / `core` 不得 import `strategy` | 引擎不感知策略 |
| `strategy` 不得 import `evaluation` | **归因 ≠ 预测**（原报告原则 3） |
| 任何模块不得 import `app` | 应用层是叶子 |

### 3.2 关键数据结构

```python
# data/objects.py
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    board: Literal["MAIN", "GEM", "STAR", "BSE"]   # 决定涨跌幅
    lot_size: int = 100
    tick_size: float = 0.01
    is_st: bool = False

    @property
    def limit_ratio(self) -> float:
        if self.is_st:
            return 0.05
        return {"MAIN": 0.10, "GEM": 0.20, "STAR": 0.20, "BSE": 0.30}[self.board]

@dataclass(frozen=True)
class Bar:
    symbol: str
    dt: datetime
    freq: Literal["1d", "60m", "15m", "5m"]
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    prev_close: float                 # ★ 涨跌停基准
    limit_up: float | None = None
    limit_down: float | None = None
    is_suspended: bool = False

    @property
    def is_one_word_board(self) -> bool:
        """一字板：无法成交"""
        return self.high == self.low
```

```python
# rules/position.py
from collections import deque
from dataclasses import dataclass
from datetime import date

@dataclass
class PositionLot:
    open_date: date
    quantity: int
    cost_price: float

class PositionBook:
    """A股单标的持仓簿：FIFO 队列表达 T+1，无全局状态"""
    def __init__(self) -> None:
        self._lots: deque[PositionLot] = deque()
        self._today: date | None = None
        self._frozen: int = 0            # 挂单冻结

    def before_trading(self, trading_date: date) -> None:
        self._today = trading_date
        self._frozen = 0

    @property
    def total(self) -> int: ...
    @property
    def today_bought(self) -> int:
        return sum(l.quantity for l in self._lots if l.open_date == self._today)
    @property
    def sellable(self) -> int:
        """T+1 可卖量"""
        return self.total - self.today_bought - self._frozen

    def buy(self, qty: int, price: float) -> None: ...
    def sell(self, qty: int, price: float) -> list[tuple[date, int, float]]:
        """FIFO 平仓；qty > sellable 时抛 T1Violation"""
    def apply_corporate_action(self, ratio: float, cash_per_share: float) -> float:
        """送转 + 分红，返回现金流入"""
```

```python
# rules/cost.py
@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005     # 仅卖出
    transfer_rate: float = 0.00001
    slippage: float = 0.0005

class AShareCostModel:
    def buy_cost(self, amount: float) -> float: ...
    def sell_cost(self, amount: float) -> float: ...
    def apply_slippage(self, price: float, side: str) -> float:
        """买入上滑、卖出下滑——方向不可写反"""
    def round_trip(self, amount: float) -> float: ...
```

```python
# core/event.py
class EventType:
    BEFORE_TRADING = "BEFORE_TRADING"
    BAR = "BAR"
    AFTER_TRADING = "AFTER_TRADING"
    ORDER = "ORDER"
    TRADE = "TRADE"

@dataclass(frozen=True)
class Event:
    type: str
    payload: object
```

### 3.3 关键接口（抽象基类）

```python
# data/provider.py
class DataProvider(ABC):
    @abstractmethod
    def get_bars(self, symbol: str, freq: str,
                 start: date, end: date,
                 adjust: Literal["none", "qfq", "hfq"] = "qfq") -> list[Bar]: ...

    @abstractmethod
    def get_instrument(self, symbol: str) -> Instrument: ...

    @abstractmethod
    def get_calendar(self, start: date, end: date) -> list[date]: ...

PROVIDERS: dict[str, type[DataProvider]] = {}

def register(name: str):
    def deco(cls): PROVIDERS[name] = cls; return cls
    return deco
```

```python
# data/pit.py  ★ 本项目最关键的组件
class PITGuard:
    """时点守卫：任何数据读取必须声明 as_of，晚于 as_of 的不可见"""

    LAG_RULES = {
        "financial_report": lambda pub: pub + timedelta(days=1),
        "bdti": lambda pub: pub + timedelta(days=2),   # 运价指数发布滞后
        "bdi":  lambda pub: pub + timedelta(days=1),
        "daily_bar": lambda pub: pub + timedelta(hours=18),
    }

    def visible(self, data_dt: datetime, as_of: datetime, kind: str) -> bool:
        return self.LAG_RULES[kind](data_dt) <= as_of

    def filter(self, rows: list, as_of: datetime, kind: str) -> list: ...
```

```python
# strategy/base.py
class Strategy(ABC):
    def __init__(self, ctx: Context) -> None: ...
    def on_before_trading(self, dt: date) -> None: ...
    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None: ...
    def on_after_trading(self, dt: date) -> None: ...

@dataclass(frozen=True)
class Signal:
    symbol: str
    dt: datetime
    target_weight: float        # [0, 1]，long-only
    reason: str                 # 归因用，不参与决策
```

```python
# strategy/factors/registry.py
@dataclass(frozen=True)
class FactorSpec:
    name: str
    func: Callable[[pd.DataFrame], pd.Series]
    inputs: tuple[str, ...]
    cross_sectional: bool = False       # 阶段一全部 False
    pit_kind: str = "daily_bar"

class FactorRegistry:
    def register(self, spec: FactorSpec) -> None: ...
    def compute(self, names: list[str], data: pd.DataFrame,
                as_of: datetime) -> pd.DataFrame: ...
```

```python
# execution/risk.py
class RiskRule(ABC):
    @abstractmethod
    def check(self, order: OrderRequest, ctx: Context) -> RiskVerdict: ...

@dataclass(frozen=True)
class RiskVerdict:
    passed: bool
    reason: str = ""

class RiskGate:
    """回测与实盘共用同一实例；任何下单路径必须经过此处"""
    def __init__(self, rules: list[RiskRule]) -> None: ...
    def check(self, order: OrderRequest, ctx: Context) -> RiskVerdict: ...
```

```python
# execution/gateways/base.py
class ExecutionGateway(ABC):
    @abstractmethod
    def send_order(self, order: OrderRequest) -> str: ...
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
    @abstractmethod
    def query_position(self, symbol: str) -> int: ...
    @abstractmethod
    def query_cash(self) -> float: ...
```

### 3.4 各模块职责边界（输入 / 输出 / **不做什么**）

| 模块 | 输入 | 输出 | **明确不做** |
|---|---|---|---|
| `DataProvider` | symbol, 区间, 频率 | `list[Bar]` | 不做复权计算（由适配器负责）、不做缓存 |
| `PITGuard` | 原始行 + `as_of` | 过滤后的行 | 不做策略判断、不修改数据 |
| `PositionBook` | 买卖指令 | 持仓批次、可卖量 | 不做成本计算、不判断涨跌停 |
| `TradabilityGate` | Bar + OrderRequest | `bool` + 原因 | 不做资金校验 |
| `AShareCostModel` | 成交金额 | 费用明细 | 不做撮合 |
| `MatchingEngine` | 订单列表 + Bar | `list[Trade]` | **不做风控**（风控在 RiskGate） |
| `Ledger` | Trade | 更新 Account / PositionBook | 不做撮合、不做风控 |
| `RiskGate` | OrderRequest + Context | `RiskVerdict` | 不做下单、不做撮合 |
| `OrderManager` | OrderRequest | order_id + 状态机 | 不做风控 |
| `SignalGenerator` | 因子矩阵 | `Signal` | 不做股数换算、不做风控 |
| `PositionSizer` | `Signal` | `OrderRequest` | 不做风控 |
| `Attribution` | Recorder 快照 | 收益分解表 | **不得回写 strategy** |
| `PerfReport` | 净值序列 + 基准 | HTML tearsheet | 不做归因 |

**边界原则**：每个模块只做一件事，**风控只有 `RiskGate` 一处、撮合只有 `MatchingEngine` 一处、账务只有 `Ledger` 一处**。任何"顺手也校验一下"的实现都视为设计缺陷。

---

## 四、模块间的依赖与调用链

### 4.1 一个回测交易日的完整调用链

```
[Clock] 推进到 T 日
   │
   ├─► EventBus.publish(BEFORE_TRADING)
   │        └─► Ledger.on_before_trading()
   │              ├─ PositionBook.before_trading(T)   # 结转昨仓，清冻结
   │              └─ Account.rollover()
   │        └─► Strategy.on_before_trading(T)
   │
   ├─► EventBus.publish(BAR, bar_T)
   │        └─► Strategy.on_bar(bar_T)
   │              ├─ FactorRegistry.compute(names, data, as_of=T)
   │              │     └─ PITGuard.filter(..., as_of=T)      ← 防前视
   │              ├─ SignalGenerator.generate(factors) ──► Signal
   │              └─ PositionSizer.size(signal, account) ──► OrderRequest
   │
   │        └─► RiskGate.check(order, ctx)                    ← 唯一风控点
   │              ├─ TradabilityGate（涨跌停/停牌）
   │              ├─ PositionBook.sellable（T+1）
   │              ├─ Account.cash（资金）
   │              └─ 自定义规则（仓位上限/换手上限/熔断）
   │              └─► 拒绝 → 记录 RiskVerdict.reason，终止
   │
   │        └─► OrderManager.submit(order)
   │              └─► MatchingEngine.match(orders, bar_{T+1})  ← 次日开盘成交
   │                    ├─ AShareCostModel.apply_slippage()
   │                    └─► list[Trade]
   │                          └─► Ledger.on_trade(trade)
   │                                ├─ PositionBook.buy/sell
   │                                ├─ Account.update_cash
   │                                └─ Recorder.snapshot()
   │
   └─► EventBus.publish(AFTER_TRADING)
            └─► Ledger.settle()  ──► 日终净值
            └─► Recorder.flush()

[离线] PerfReport(净值序列) ──► HTML tearsheet
[离线] Attribution(Recorder 快照) ──► 收益分解
```

**链路上的三个"唯一性"约束**（对应 §3.4 的边界原则）：

- 风控只在 `RiskGate.check` 发生一次
- 撮合只在 `MatchingEngine.match` 发生一次，且**发生在信号日的下一根 bar**
- 账务只在 `Ledger.on_trade` 更新一次

### 4.2 依赖强度矩阵（谁被谁依赖）

| 模块 | 被依赖数 | 说明 |
|---|---|---|
| `data/objects.py` | 6 | 全系统共享的数据契约 |
| `rules/` | 5 | core / strategy / execution / app / tests |
| `core/` | 4 | strategy / execution / evaluation / app |
| `data/provider.py` + `pit.py` | 4 | core / strategy / app / tests |
| `strategy/` | 1 | 仅 app |
| `execution/` | 1 | 仅 app |
| `evaluation/` | 1 | 仅 app |

**结论**：`rules/` 与 `data/objects.py` 位于依赖图根部，**必须先做且必须做对**。

---

## 五、实现步骤与优先级

### 5.1 优先级表（按"前置依赖少 + 被依赖多 + 决定可信度"排序）

| 优先级 | 内容 | 前置 | 交付物 | 验收门槛 |
|---|---|---|---|---|
| **P0** | 包骨架 + 配置加载 + `test_architecture.py` | — | 可导入的空包 | 4 条 import 禁令的自动断言通过 |
| **P1** | `rules/` 全套（cost / position / tradability / corporate_action / account / portfolio） | P0 | 规则层 + 单测 | **T+1、涨跌停、成本、除权 4 类测试全绿**；往返成本率误差 < 1bp |
| **P2** | `data/objects.py` + `PITGuard` + 1 个适配器 + `DataCache` | P0 | 数据层 | 同一 `as_of` 两次读取结果一致；`as_of` 调早一天时未来数据不可见 |
| **P3** | `core/` 最小闭环（EventBus / Clock / MatchingEngine / Ledger / Recorder） | P1, P2 | 可跑回测 | **同输入两次运行净值曲线完全一致** |
| **P4** | `evaluation/perf.py` + `pitfalls.py` | P3 | HTML tearsheet | 输出 30+ 指标；10 项陷阱清单逐条可执行 |
| **P5** | `strategy/`（factors / signals / sizing） | P3 | 因子库 + 时序 IC | 因子无前视（`as_of` 校验通过） |
| **P6** | `execution/risk.py` + `oms.py` + `gateways/backtest.py` | P3 | 风控 + OMS | 涨停买单被拒、T+1 超卖被拒、超资金被拒 |
| **P7** | `evaluation/walkforward.py` + `montecarlo.py` | P5 | 鲁棒性报告 | 参数热力图能识别"宽稳定区" |
| **P8** | `gateways/live.py` + `app/daily_job.py` | P6 | 实盘/纸面通道 | 纸面交易逐笔对账一致 |

**关键顺序理由**：

- **P1 必须最先做**：无前置依赖、被 5 个模块依赖、且直接决定回测可信度。原报告结论一致。
- **P2 紧随 P1**：`PITGuard` 与规则层是"可信度双地基"，晚做会导致 P5 的因子全部重写。
- **P4 早于 P5**：先有评估能力，再写策略。**否则写出的因子没有标尺**——这是前六轮踩坑的结构性原因。
- **P5/P6 可并行**：两者只共同依赖 P3。

### 5.2 最小可运行闭环（MVP）—— 建议的第一个里程碑

```
P0 → P1 → P2 → P3 → P4
```

策略层用一个 **`BuyAndHoldStrategy`（始终 target_weight=1.0）** 充当冒烟测试。

**为什么这是最佳的第一个里程碑**：B&H 策略有**解析解**——回测净值应当等于"首日买入并持有"的收益，**唯一差异只能来自成本与滑点**。这给了规则层一个可精确验证的靶子：

> 若 B&H 回测净值与"手工持有"的差异 ≠ 已知成本，则**规则层或撮合器一定有 bug**。

这比"跑一个复杂策略看收益"可靠得多，且能在 P4 阶段就产出第一个**确定的**结论。

### 5.3 每阶段的强制纪律

1. **先写失败测试，再写实现**（fail-to-pass）
2. **每个阶段产出可复现的实验记录**（数据区间 / 参数 / 结果 / 结论）
3. **结论为"不可用"同样是一次成功交付**，必须归档
4. **不得跨阶段抢跑**：P4 完成前不得写 P5 的因子（否则没有标尺）

---

## 六、需要你拍板的 5 个决策点

| # | 决策 | 选项 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | **数据源** | akshare / baostock / tushare(积分) / 本地通达信导出 / Futu | 决定 P2 的适配器实现；akshare 接口不稳定但覆盖广，baostock 免费稳定但字段少 | **baostock 打底（历史日线+复权）+ akshare 补运价/宏观**；适配器隔离，可随时换 |
| 2 | **回测频率** | 日频 / 60min / 15min | 决定 `BarGenerator` 是否需要；A股 T+1 下分钟频的收益主要来自择时精度 | **先日频**。T+1 制度下日内信号只能用于"次日开盘执行"，分钟频边际价值低 |
| 3 | **akquant（MIT，Rust+Python）是否采用** | 直接依赖其引擎 / 只作参考 / 暂不评估 | 我**未读过其源码**，无法判断其规则层是否可与自建规则层解耦 | **列为 P3 的决策点**：先用自建最小引擎跑通闭环，再评估是否替换。评估标准见下 |
| 4 | **实盘通道** | 券商 miniQMT / Ptrade / 纯人工执行 | 决定 P8 的实现方式 | **阶段一~三默认纯人工执行**（系统出信号），不引入通道风险 |
| 5 | **税务口径** | 个人 / 机构 | 决定 `TaxModel` 是否实现（增值税+附加税+亏损跨月抵扣） | **个人口径，阶段一不实现**，仅留空接口 |

**决策 3 的评估标准**（若考虑采用 akquant）：

- [ ] 其规则层（T+1 / 涨跌停 / 成本）能否独立调用，不依赖其引擎全局状态？
- [ ] 其数据层是否可替换为自建 `DataProvider`？
- [ ] 是否支持 `as_of` 时点语义（防前视）？
- [ ] 能否插入自建 `RiskGate` 作为唯一下单前闸门？

四项全部为"是"才值得替换；否则**参考其 Rust 侧的性能方案即可**。

---

## 七、与原报告的差异说明（三处修正）

| # | 原报告 | 本设计 | 理由 |
|---|---|---|---|
| 1 | vnpy 列为"Phase 0 直接依赖，`pip install vnpy`" | **推迟到 Phase 5**，前四阶段只借鉴其事件模型与数据对象设计 | vnpy 价值集中在实盘 gateway；全套安装会引入 GUI/数据库等无用依赖 |
| 2 | `CapitalGainsTaxMixin` 列为"最高价值"项 | **降级为"接口预留、阶段一不实现"** | 增值税+附加税是**机构口径**；个人账户股票转让免征，实现它没有收益 |
| 3 | Phase 2「回测引擎重构」 | 提前插入 **PITGuard 与 evaluation** | 先有"防前视"与"评估标尺"，再写策略。否则会重复"写完因子才发现无法验证"的循环 |

**未变的两条最高优先级**（与原报告一致）：

1. **自建 A 股规则层**（T+1 + 成本 + 涨跌停）
2. **采用 vectorbt-skills 的鲁棒性检验标准**

<!--APPEND-3-->


