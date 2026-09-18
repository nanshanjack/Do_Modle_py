# A 股量化交易系统 · 架构与模块设计

> 输入依据：`开源项目可复用模块分析.md`（2026-09-17）
> 阶段一标的：**601872.SH 招商轮船**（单一标的）
> 本文件为工程设计，**不构成投资建议**

---

## 一、背景与既定约束（从既有分析中提取，不再重复论证）

### 1.1 已确立的事实

| 事实 | 影响设计的含义 |
|---|---|
| 已六次验证失败：BDTI/油价无预测力、跳空吃掉收益、超额收益 t=−1.27、跑输买入持有 | 系统的**首要产出是"可信的否定结论"**，不是"能赚钱的信号" |
| A 股规则层（T+1 / 成本 / 涨跌停）完全缺失 | **Phase 1 唯一优先级**，其余全部后置 |
| rqalpha 商业用途受限 | 其代码**只能参考思路**，不可复制（`PositionQueue` / `CapitalGainsTaxMixin` 需重写） |
| vnpy (MIT) / quantstats (Apache-2.0) / vectorbt-skills (MIT) | 可**直接依赖** |
| 归因 ≠ 预测 | 架构上必须**物理隔离**"研究工具"与"交易信号"两个模块 |

### 1.2 四条设计原则（来自原报告 5.3）

1. **数据层与引擎解耦** —— 换数据源不改引擎（借鉴 rqalpha `mod/` 机制）
2. **规则层独立可测** —— T+1 / 成本 / 涨跌停各自独立单元测试
3. **归因 ≠ 预测** —— 研究层与信号层分属不同包，禁止交叉 import
4. **每步可验证** —— 沿用 fail-to-pass 文化，每个模块先写失败测试

### 1.3 阶段一的硬性范围（明确不做）

- ❌ 不做期货 / 期权 / 融资融券 / 可转债
- ❌ 不做组合优化、行业中性化、横截面选股
- ❌ 不做高频、不做 tick 级撮合（日频为主，分钟频可选）
- ❌ 不接实盘（Phase 5 之前只做 paper trading）

---

## 二、架构总览

**核心形态：分层 + 事件驱动 + 插件式数据/通道。**

```
┌─────────────────────────────────────────────────────────────┐
│  L6 应用层      CLI / 任务编排 / 报告产出                     │
├─────────────────────────────────────────────────────────────┤
│  L5 执行层      订单管理 OMS / 风控 RiskGate / 通道适配        │
│                 （回测撮合器 与 实盘网关 实现同一接口）        │
├─────────────────────────────────────────────────────────────┤
│  L4 策略层      因子库 / 信号生成 / 仓位决策（纯函数式）        │
├─────────────────────────────────────────────────────────────┤
│  L3 引擎层      事件总线 EventBus / 时钟 Clock / 撮合与账务     │
├─────────────────────────────────────────────────────────────┤
│  L2 规则层      PositionBook(T+1) / AShareCostModel / 涨跌停   │
│                 ← 自建，最高优先级，无外部依赖                  │
├─────────────────────────────────────────────────────────────┤
│  L1 数据层      DataProvider 抽象 → 适配器(akshare/baostock/   │
│                 tushare/本地通达信/Futu) + 本地缓存(PIT 存储)   │
├─────────────────────────────────────────────────────────────┤
│  L0 基础设施    配置 / 日志 / 持久化(SQLite/Parquet) / 测试     │
└─────────────────────────────────────────────────────────────┘
```

**贯穿切面（横切所有层）：**

- **Clock（时钟）**：回测用事件时间，实盘用真实时间；策略代码不得直接调用 `datetime.now()`
- **DataProvider 抽象**：策略只认接口，不认数据源
- **RiskGate（风控闸门）**：所有订单（回测与实盘）必须穿过同一闸门

**关键架构决策：回测与实盘的唯一差异应只是"撮合器实现"。** 策略、规则层、风控、账务全部复用同一套代码。这是避免"回测能跑、实盘对不上"的根本手段。

---

## 三、核心功能模块清单

### 3.1 L1 数据层

| 模块 | 职责 | 关键接口 | 依赖来源 |
|---|---|---|---|
| `DataProvider`（抽象） | 定义 `get_bars(symbol, freq, start, end)` / `get_quote` / `get_calendar` / `get_instruments` | 自写抽象基类 | 借鉴 rqalpha `mod/` 与 vnpy `BaseGateway` |
| `AkshareAdapter` | 免费快照式数据（日线、复权、指数、部分宏观） | 实现 DataProvider | akshare（MIT） |
| `BaostockAdapter` | 免费历史日线 + 前/后复权因子 | 实现 DataProvider | baostock |
| `TushareAdapter` | 财务、股东、资金流（需积分） | 实现 DataProvider | tushare pro |
| `LocalDumpAdapter` | 通达信/自建 CSV 落盘数据 | 实现 DataProvider | 自写 |
| `FutuAdapter` | 盘中行情/实时快照（已连接 MCP，可作实盘行情源） | 实现 DataProvider | futu OpenAPI |
| `DataCache` | **PIT 本地存储**（Parquet/SQLite）+ 复权因子表 + 交易日历 | 自写 | — |
| `PITGuard` | **时点校验**：任何数据读取强制带 `as_of` 时间戳，晚于 `as_of` 的数据不可见 | 自写 | **防前视偏差的核心** |

> **招商轮船专属数据需求（阶段一）**：日线行情、复权因子、停复牌记录、涨跌停价、油运/干散货运价指数（BDTI / BDI / VLCC TCE，周频且**免费源质量差、发布有滞后**）、原油价格（Brent/WTI）、公司财务（季频，**法定滞后 ≥ 45 天**）。
> 运价类数据必须显式建模"发布日期 ≠ 数据日期"，否则就是前视偏差。

### 3.2 L2 A 股规则层（★ 最高优先级，纯自建、零外部依赖）

| 模块 | 职责 | 关键点 |
|---|---|---|
| `PositionLot` / `PositionBook` | **FIFO 批次队列实现 T+1** | `sellable = total − today_bought`；支持部分平仓并返回每批明细 |
| `AShareCostModel` | 佣金(0.025%, min 5元) + 印花税(0.05%, 仅卖出) + 过户费(0.001%, 双边) + 滑点 | 单边约 0.111%，往返含滑点约 0.21% |
| `TradabilityGate` | **涨跌停可交易性**：涨停不可买、跌停不可卖、一字板识别 | 用 `high==low==limit` 判定一字板 |
| `CorporateActionHandler` | 送股/转增/分红/除权除息 → 调整批次数量与成本 | 借鉴 rqalpha `handle_split` 思路 |
| `Account` | 资金 / 持仓 / 冻结 / 盈亏，**区分当日成交盈亏与昨仓盈亏** | 借鉴 rqalpha `Account` |
| `TaxMixin`（可选） | 金融商品转让增值税 + 附加税 + 亏损跨月抵扣 | **仅机构口径需要；个人账户阶段一可不实现** |

**验收标准**：单元测试覆盖 —— 当日买入当日卖出必须被拒；涨停价买单必须被拒；跌停价卖单必须被拒；跨除权日持仓数量正确；往返成本率误差 < 1bp。

### 3.3 L3 引擎层

| 模块 | 职责 | 借鉴来源 |
|---|---|---|
| `EventBus` | 发布/订阅，事件类型：`BEFORE_TRADING` / `BAR` / `AFTER_TRADING` / `ORDER` / `TRADE` | rqalpha `core/`（参考设计） |
| `Clock` | 统一时间源，驱动回测推进；实盘模式下同步真实时间 | vnpy `EventEngine` 思路 |
| `MatchingEngine` | 回测撮合：默认 **下一根 bar 开盘价成交**（杜绝同 bar 收盘价成交的未来函数） | 自写 |
| `Ledger` | 账务：成交 → 更新 `Account` + `PositionBook`，每日结算快照 | 自写 |
| `Recorder` | 记录每个 bar 的完整状态（持仓/资金/信号/订单）供事后归因 | 自写 |

**必须遵守的撮合规则**：信号在 `t` 收盘产生 → 订单在 `t+1` 开盘（或 `t+1` 收盘）成交 → 成交价受涨跌停与滑点约束。**禁止 `t` 收盘价成交。**

### 3.4 L4 策略层

| 模块 | 职责 | 备注 |
|---|---|---|
| `FactorRegistry` | 因子注册：名称 / 公式 / 时点要求 / 数据依赖 / 所属类别 | 借鉴 qlib handler 组织方式 |
| `FactorLibrary` | 因子实现（时序为主） | 见下表 |
| `SignalGenerator` | 因子 → 信号（阈值 / 分位 / 模型输出） | 纯函数，可单测 |
| `PositionSizer` | 信号 → 目标仓位（0/0.5/1 或波动率目标） | 单标的阶段只做 `long-only` |
| `Strategy`（抽象） | `on_bar(ctx, bar)` 生命周期钩子 | 借鉴 rqalpha `AbstractStrategy` |

**阶段一因子库（时序，单标的可用）：**

| 类别 | 因子示例 | 数据源 | 时点风险 |
|---|---|---|---|
| 技术/量价 | 动量(20/60/120d)、波动率、换手率、量价背离、均线状态 | 日线 | 低 |
| 运价/周期 | BDTI 变化率、BDI 变化率、VLCC TCE 分位、运价-股价背离 | 波交所/第三方 | **高（发布滞后）** |
| 能源价格 | Brent/WTI 收益率、期限结构代理 | 公开 | 中 |
| 季节性 | 油运旺季(冬季)哑变量、月度效应 | 日历 | 低 |
| 基本面 | PB、ROE、营收增速、船队规模代理 | 财报 | **高（法定滞后 45–90 天）** |

> **单标的阶段的根本限制**：无法计算横截面 IC、无法做分组收益。评估必须改用 **时序 IC / 事件研究 / 择时超额** 三件套。

### 3.5 L5 执行层

| 模块 | 职责 | 备注 |
|---|---|---|
| `RiskGate` | 订单前风控：单笔上限、单日换手上限、仓位上限、T+1 可卖量校验、涨跌停校验、连续亏损熔断 | 回测与实盘**共用同一实例** |
| `OrderManager` | 订单生命周期：新建/部分成交/全部成交/撤销/超时 | 状态机 |
| `ExecutionGateway`（抽象） | 统一下单接口 `send_order / cancel_order / query_position` | vnpy `BaseGateway` 风格 |
| `BacktestGateway` | 回测撮合实现 | 自写 |
| `PaperGateway` | 纸面交易（实时行情 + 模拟成交） | 自写 |
| `LiveGateway` | 实盘通道（见 3.5.1） | 外部依赖 |

#### 3.5.1 A 股实盘的现实通道（关键工程约束）

vnpy 的股票网关（XTP / TORA 等）**面向机构或高净值客户，需券商单独开通**。个人账户的现实路径：

| 通道 | 说明 | 风险 |
|---|---|---|
| **券商 miniQMT（迅投）** | 多数券商可申请，提供 Python API，**个人量化最主流** | 需券商开通，有资金门槛 |
| **Ptrade（恒生）** | 部分券商提供，Python 策略托管 | 同上 |
| `easytrader` 类客户端自动化 | 模拟点击券商客户端 | **不稳定、易被风控、不建议** |
| 手工下单 | 系统只出信号，人工执行 | 阶段一至三的**默认选择** |

**结论**：阶段一~四**不依赖任何实盘通道**。Phase 5 再按券商条件二选一。

### 3.6 L6 评估与鲁棒性层（与 L4 同级，独立成包）

| 模块 | 职责 | 依赖来源 |
|---|---|---|
| `PerfReport` | Sharpe / Sortino / Calmar / 最大回撤 / 胜率 / 盈亏比 / 月度热力图 / HTML tearsheet | **quantstats（直接依赖）** |
| `Attribution` | 收益分解：市场 beta / 运价因子 / 择时贡献 / 成本拖累 | 自写（**归因，非预测**） |
| `WalkForward` | 滚动窗口 + WFE 比率 | vectorbt-skills `walk-forward.md` |
| `MonteCarlo` | 交易打乱 / 噪声注入 / 参数敏感性 / 延迟测试 | vectorbt-skills `robustness-testing.md` |
| `ParamHeatmap` | 参数热力图，只认"宽稳定区"，拒绝"孤立尖峰" | 同上 |
| `PitfallChecklist` | 10 项陷阱清单自动自检（前视/幸存者/成本/涨跌停/T+1/样本内外/多重检验…） | vectorbt-skills `pitfalls.md` |

---

## 四、模块间的数据流与协作方式

系统对外只呈现 **三条链路**，三条链路共用 L2 规则层与 L3 引擎层。

### 4.1 链路 A：研究链路（离线批量，可重复）

```
DataProvider ──► PITGuard ──► DataCache
                                  │
                                  ▼
                          FactorRegistry ──► FactorLibrary
                                  │
                                  ▼
                          SignalGenerator ──► PositionSizer
                                  │
                                  ▼
                     ┌── EventBus(BAR) ──► Strategy.on_bar
                     │            │
                     ▼            ▼
              MatchingEngine ──► Ledger ──► PositionBook / Account
                     │                            │
                     └────────► Recorder ◄────────┘
                                  │
                                  ▼
              PerfReport / Attribution / WalkForward / MonteCarlo
                                  │
                                  ▼
                     研究结论（可用 / 不可用 / 待验证）
```

**特征**：全离线、可重放、确定性。同输入必须同输出（`random_seed` 固定）。

### 4.2 链路 B：信号链路（每日 / 盘中，低延迟）

```
行情快照 (Futu / 券商 API)
      │
      ▼
 DataProvider.get_quote ──► PITGuard(as_of=now)
      │
      ▼
 Strategy.on_bar ──► SignalGenerator ──► PositionSizer
      │
      ▼
 [ 目标仓位 ] ──────────────► RiskGate ──► 订单意图
                                  │
                                  ▼
                          OrderManager ──► ExecutionGateway
                                                  │
                                     ┌────────────┴────────────┐
                                     ▼                         ▼
                              BacktestGateway           LiveGateway / 人工执行
                                     │                         │
                                     └───────────┬─────────────┘
                                                 ▼
                                         成交回报 (TRADE)
                                                 │
                                                 ▼
                                        Ledger ──► PositionBook / Account
```

**关键约束**：链路 B 中 `RiskGate` 的位置**不可移动**——任何绕过风控的路径（含手工补单脚本）视为设计缺陷。

### 4.3 链路 C：反馈与归因链路（链路 A/B 的收敛点）

```
每日结算快照 (Recorder)
        │
        ▼
   Attribution：本次盈亏来自
     ├─ 市场 beta（大盘涨跌）
     ├─ 行业/运价因子暴露
     ├─ 择时贡献（信号 vs 持有）
     └─ 成本拖累（佣金+印花税+滑点）
        │
        ▼
   是否出现"策略逻辑未预期的收益来源"？
        ├─ 是 → 标记为可疑，回到链路 A 做稳健性检验
        └─ 否 → 因子库迭代
```

> **设计红线（原则 3）**：`Attribution`（归因）与 `SignalGenerator`（预测）**禁止互相 import**。归因结果不得直接进入信号生成，只能作为人工评审输入。这是防止"用后验解释倒推策略"的架构级保障。

### 4.4 协作时序（一个交易日）

| 时刻 | 事件 | 触发模块 | 输出 |
|---|---|---|---|
| T−1 20:00 | 数据同步 + PIT 落库 | DataProvider / DataCache | 最新 bar |
| T 09:00 | 盘前任务 | EventBus `BEFORE_TRADING` | 结转昨仓、`old_quantity` 快照、生成当日可卖量 |
| T 09:25 | 集合竞价参考 | SignalGenerator | 目标仓位 |
| T 09:30–15:00 | 盘中轮询（可选，日频可跳过） | Strategy `on_bar` | 订单意图 |
| 订单产生 | 风控校验 | RiskGate | 通过 / 拒绝（记录拒绝原因） |
| 成交 | 账务更新 | Ledger → PositionBook / Account | 持仓与资金更新 |
| T 15:05 | 盘后结算 | Ledger `AFTER_TRADING` | 日终快照 + 盈亏分解 |
| T 20:00 | 归因与报告 | Attribution / PerfReport | 日报 |

---

## 五、跨模块数据契约

**所有跨层传递必须使用下述统一数据对象（借鉴 vnpy `object.py` 的思路），禁止裸 `dict` 跨层。**

```python
# 统一数据对象（示意，字段以实现为准）
@dataclass(frozen=True)
class Bar:
    symbol: str; dt: datetime; freq: str
    open: float; high: float; low: float; close: float
    volume: float; amount: float
    limit_up: float | None; limit_down: float | None   # 涨跌停价，A股必需
    is_suspended: bool                                  # 停牌标记

@dataclass(frozen=True)
class Signal:
    symbol: str; dt: datetime
    target_position: float      # 目标仓位比例 [0, 1]，long-only
    reason: str                 # 归因用，不参与决策

@dataclass(frozen=True)
class OrderRequest:
    symbol: str; dt: datetime
    side: Literal["BUY", "SELL"]
    quantity: int               # 股数，100 的整数倍
    price_type: Literal["LIMIT", "MARKET_PROXY"]
    limit_price: float | None

@dataclass(frozen=True)
class Trade:
    symbol: str; dt: datetime; order_id: str
    side: str; quantity: int; price: float
    commission: float; stamp_tax: float; transfer_fee: float; slippage: float

@dataclass
class AccountSnapshot:
    dt: date
    cash: float; frozen: float; market_value: float; total: float
    trading_pnl: float          # 当日成交部分盈亏
    position_pnl: float         # 昨仓部分盈亏
```

**契约约束：**

1. `Bar` 必须自带 `limit_up/limit_down`，否则 `TradabilityGate` 无法工作
2. `Signal` 只表达**目标仓位**，不表达"买多少股"——股数换算属于 `PositionSizer`
3. `OrderRequest.quantity` 必须为 100 的整数倍（A 股 1 手 = 100 股），由 `RiskGate` 校验
4. `Trade` 必须逐项拆出费用，禁止只记一个 `cost` 总额（否则无法做成本归因）

---

## 六、阶段一（单标的）的简化 vs. 必须预留的扩展点

**核心原则：功能可以砍，接口不能砍。** 阶段一代码量最小化，但所有抽象按多标的形态定义。

| 维度 | 阶段一做法（简化） | 必须预留的扩展点（现在就写对） |
|---|---|---|
| 标的 | 硬编码 `601872.SH` 于配置文件 | 所有函数签名**必须带 `symbol` 参数**；`Instrument` 模型包含 `lot_size / tick_size / limit_ratio / is_st / 板块` |
| 持仓 | `PositionBook` 单实例 | 用 `Portfolio` 容器包装，内部 `dict[symbol, PositionBook]`，**即使现在只有 1 个 key** |
| 数据 | 单表存储 | `DataCache` 按 `(symbol, freq, adjust)` 分区存储，天然支持扩展 |
| 因子 | 时序因子 | `FactorRegistry` 的元数据包含 `cross_sectional: bool` 字段 |
| 评估 | 时序 IC / 事件研究 | 预留 `evaluate_ic(cross_sectional)` 接口，阶段一抛 `NotImplementedError` |
| 仓位 | 0/0.5/1 三档 | `PositionSizer` 返回 `dict[symbol, weight]`，权重和 ≤ 1 |
| 风控 | 单标的上限 | `RiskGate` 规则写成"按标的聚合"的形式，而非单值判断 |
| 撮合 | 顺序撮合 | `MatchingEngine` 接收订单列表而非单笔，避免重构 |

**反例（明确禁止）**：为了"现在只有一个标的"而写 `self.position`、`def get_bar(date)`（无 symbol）、`if symbol == "601872"` 这类分支。这些是后续扩展的**真实重构成本来源**。

---

## 七、扩展到其他 A 股标的的整体思路

### 7.1 扩展的三条不可逆路径

扩展不是"加个循环"，而是三个**能力跃迁**：

```
阶段一  单标的择时        → 需要：时序信号、事件研究
阶段二  小池子(3~5只)     → 需要：仓位分配、相关性、同时持仓
阶段三  板块/行业(20~50)  → 需要：横截面因子、行业中性化、排序选股
阶段四  全市场(3000+)     → 需要：数据管道吞吐、容量约束、组合优化、交易成本冲击模型
```

**每跃迁一次，失效的东西**：

| 跃迁 | 会失效的东西 |
|---|---|
| 一 → 二 | 单标的"全仓/空仓"逻辑失效，必须引入权重分配 |
| 二 → 三 | 时序阈值信号失效，必须改为横截面排序 |
| 三 → 四 | 等权分配失效，必须引入容量与冲击成本模型；日频逐笔撮合性能不足 |

### 7.2 各层需要新增的能力

| 层 | 单标的 | 扩展后新增 |
|---|---|---|
| L1 数据 | 单标的历史 | **批量拉取 + 增量更新 + 数据质量校验（缺失/异常值/复权断裂）** |
| L2 规则 | T+1 / 成本 / 涨跌停 | **ST 股 5% 涨跌幅、科创板/创业板 20%、北交所 30%、新股上市首日无涨跌幅限制** |
| L3 引擎 | 顺序撮合 | 组合级撮合、资金分配、部分成交模拟 |
| L4 策略 | 时序信号 | **横截面因子框架、行业/风格中性化、排序选股（TopK）** |
| L5 执行 | 单笔下单 | **订单拆分（VWAP/TWAP）、冲击成本建模、批量下单限流** |
| L6 评估 | 时序 IC | **横截面 IC / IC_IR / 分层回测 / 换手率归因 / 容量分析** |

### 7.3 阶段一的"扩展友好度"验收标准

单标的版本交付时，应能通过以下检查（fail-to-pass 风格）：

1. 把配置里的 `symbol` 换成 `600519.SH`，系统**不改一行代码**即可跑完整回测
2. `Portfolio` 内加入第二个标的，`RiskGate` 与 `Ledger` **不报错**（信号可以为 0）
3. `MatchingEngine` 接收 2 笔订单时按资金约束正确拒绝其中一笔
4. `DataCache` 中同时存在两个标的的数据且互不污染

> 这 4 条是**阶段一真正的交付门槛**，比"策略是否赚钱"更能证明架构成立。

---

## 八、关键约束与陷阱清单

### 8.1 A 股制度约束（必须硬编码进规则层）

| 约束 | 内容 | 后果（若不建模） |
|---|---|---|
| T+1 | 当日买入当日不可卖 | 回测收益虚高，尤其在高频择时策略上 |
| 涨跌停 | 主板 ±10%、ST ±5%、创业板/科创板 ±20%、北交所 ±30% | 涨停买入的成交假设不成立 |
| 最小交易单位 | 买入 100 股整数倍；卖出可不足 100（零股须一次卖出） | 小资金账户无法按目标权重建仓 |
| 停牌 | 停牌期间不可交易，复牌可能跳空 | 数据缺失被误当作"价格不变" |
| 除权除息 | 必须用复权价计算收益，用不复权价计算涨跌停 | 分红日出现虚假暴跌 |
| 卖出印花税 | 0.05%，仅卖出方 | 双边成本不对称，往返约 0.111%+滑点 |
| 融券限制 | 个人券源少、成本高 | **long-only 是唯一现实约束** |

### 8.2 回测方法学陷阱（来自 `pitfalls.md`，逐条自检）

1. **前视偏差** —— 财报法定滞后 ≥45 天；运价指数发布滞后；必须用 `PITGuard`
2. **未来函数** —— 禁止用 `t` 收盘价成交 `t` 收盘产生的信号
3. **幸存者偏差** —— 单标的阶段不涉及，但**退市/更名风险**需在扩展阶段处理
4. **样本内外混用** —— 参数只能在样本内选，样本外只允许看一次
5. **多重检验** —— 试了 N 个参数组合后，最佳组合的"显著性"要按 N 打折
6. **成本低估** —— 滑点不能设为 0；招商轮船日均成交额大，滑点可设 0.05%，但**跳空缺口不能靠滑点吸收**
7. **数据窥探** —— 已知"BDTI 无预测力"这一结论本身来自历史数据，不能反过来当特征用
8. **回撤期外推** —— 单标的回测必须包含至少一次完整行业周期（油运周期约 4–6 年）
9. **不可交易性** —— 涨停/停牌日的信号必须被显式拒绝，而非假设成交
10. **参数敏感性** —— 只接受"宽稳定区"，孤立尖峰一律视为过拟合

### 8.3 单标的统计功效约束（**最关键的一条**）

近似关系：`t ≈ Sharpe × √(年数)`。

| 年化 Sharpe | 达到 t=2 所需年数 |
|---|---|
| 0.25 | **64 年** |
| 0.50 | 16 年 |
| 1.00 | 4 年 |

招商轮船 2006 年上市，至今约 20 年有效数据（含 2008、2015、2020 三轮极端行情）。

**推论**：

- 若策略年化 Sharpe < 0.45，**在可得样本内不可能达到统计显著**——t=−1.27 的历史结论与此完全一致
- 提高证据强度的**唯一合法途径**是增加独立样本维度：**跨参数、跨时间窗口、跨标的、跨市场状态**（这正是 Monte Carlo 与 walk-forward 的意义）
- **不要把"样本内表现好"当作证据**。单标的阶段的目标是**证伪效率**，不是**找到盈利策略**

### 8.4 基准选择的约束

long-only 择时策略的天然基准是 **601872 买入持有（B&H）**，而非沪深 300。理由：招商轮船是**强周期航运股**，其 beta 与运价周期高度相关，用宽基指数做基准会掩盖择时能力（或掩盖亏损）。

**必须同时报告**：① 相对 B&H 的超额；② 相对中证航运指数（若可得）的超额；③ 绝对收益。**只报其中一个视为不合格。**

### 8.5 招商轮船的标的具体约束

| 特性 | 对系统的影响 |
|---|---|
| 强周期（油运/干散货） | 策略必须包含周期位置判断，否则会在周期顶部满仓 |
| 运价数据可得性差 | BDTI/BDI 免费源多为周频、有滞后、可能修订 → **PIT 建模成本高，可能不划算** |
| 财务滞后 | 季报滞后 45–90 天，年报滞后 4 个月 |
| 单一标的事件风险 | 突发事件（制裁、油价冲击、地缘）导致跳空，止损策略在跳空面前无效 |
| 流动性好 | 滑点可设较低（0.05%），大资金容量不是阶段一瓶颈 |

### 8.6 合规约束

- **《证券市场程序化交易管理规定（试行）》**（证监会，2024-10-08 施行）：程序化交易须履行报告义务
- **沪深北《程序化交易管理实施细则》**（2025-04-03 发布，**2025-07-07 施行**）：对高频交易实施差异化监管，明确异常交易监控指标
- 个人自用、低频（日频/分钟频）、不触碰高频阈值时，合规压力较小，但**须保留完整的策略逻辑与订单记录**以备说明
- **本系统默认定位为"研究 + 信号生成"，不默认开启自动下单**，可显著降低合规风险

---

## 九、建议的代码结构

```
do_modle/
├── config/                     # 标的、成本、风控阈值（YAML）
│   └── stock_601872.yaml
├── core/                       # L3 引擎层
│   ├── event.py                # EventBus / EventType
│   ├── clock.py                # 统一时间源
│   ├── matching.py             # MatchingEngine
│   ├── ledger.py               # Ledger（账务）
│   └── recorder.py             # 状态记录
├── rules/                      # L2 A股规则层（★ 零外部依赖，纯自建）
│   ├── position.py             # PositionLot / PositionBook
│   ├── cost.py                 # AShareCostModel
│   ├── tradability.py          # TradabilityGate（涨跌停/停牌）
│   ├── corporate_action.py     # 除权除息
│   └── account.py              # Account / Portfolio
├── data/                       # L1 数据层
│   ├── provider.py             # DataProvider 抽象
│   ├── adapters/               # akshare / baostock / tushare / local / futu
│   ├── cache.py                # DataCache（Parquet/SQLite）
│   └── pit.py                  # PITGuard
├── strategy/                   # L4 策略层
│   ├── factors/                # 因子库 + FactorRegistry
│   ├── signals.py              # SignalGenerator
│   ├── sizing.py               # PositionSizer
│   └── base.py                 # Strategy 抽象
├── execution/                  # L5 执行层
│   ├── risk.py                 # RiskGate
│   ├── oms.py                  # OrderManager
│   └── gateways/               # backtest / paper / live
├── evaluation/                 # L6 评估层
│   ├── perf.py                 # quantstats 封装
│   ├── attribution.py          # 归因（与 strategy 物理隔离）
│   ├── walkforward.py
│   ├── montecarlo.py
│   └── pitfalls.py             # 10 项自检清单
├── app/                        # L6 应用层（CLI / 任务编排）
│   ├── cli.py
│   └── daily_job.py
└── tests/                      # 与 rules/ 一一对应的单元测试
```

**依赖方向（单向，禁止反向 import）：**

```
app → evaluation → strategy → core → rules → data
                     ↓          ↓       ↓
                  (禁止 import evaluation)
```

`evaluation` 不得被 `strategy` 引用；`rules` 不得引用 `data`（规则层只接收已解析的数值）。

---

## 十、实施路线与验收标准

对齐原报告 Phase 0–5，但按**模块视角**重组，明确每一步的产出与门槛。

| 阶段 | 内容 | 产出 | 验收门槛（fail-to-pass） |
|---|---|---|---|
| **P0** | 依赖引入与验证 | `quantstats` tearsheet 样例、`vnpy` 导入成功 | 能对任一策略输出 30+ 指标 |
| **P1** | **A 股规则层**（rules/） | `PositionBook` / `CostModel` / `TradabilityGate` / `Account` | **T+1、涨跌停、成本、除权四类测试全绿** |
| **P2** | 引擎层 + 数据层骨架（core/ + data/） | 事件驱动引擎 + PIT 缓存 + 撮合器 | 同一策略**两次运行结果完全一致**；切换数据源不改策略代码 |
| **P3** | 因子与信号层（strategy/） | 因子注册机制 + 11 → 30+ 因子 + 时序 IC 评估 | 因子时点校验通过（无前视） |
| **P4** | 评估与鲁棒性层（evaluation/） | walk-forward + Monte Carlo + 参数热力图 + 自检清单 | **10 项陷阱清单逐条自检通过** |
| **P5** | 实盘对接（execution/gateways/） | miniQMT 或 Ptrade 通道 + paper trading ≥1 个月 | 纸面交易成交记录与系统记录**逐笔对账一致** |

**P1 是唯一不可跳过、不可后置的阶段。** 在 P1 完成前，任何因子/模型工作都是在不确定的地基上盖楼。

### 每阶段的强制产出物

- 代码 + 单元测试（覆盖率不作为指标，**关键分支覆盖**才是指标）
- 一份可复现的实验记录（数据区间、参数、结果、结论）
- 若结论是"策略不可用"，**同样是一次成功的交付**

---

## 十一、对结果的诚实预期（必须写在设计里）

原报告已明确：**开源项目能解决"工程完备性"，不能解决"策略有效性"。**

系统建成后，最可能的结果仍然是"**策略不可用**"。但设计目标并非避免这个结论，而是：

1. 让结论**确定**，而不是"不知道是策略不行还是回测错了"
2. 让**未来的新想法能在 1 天内被严格证伪**，而不是重复 6 轮踩坑
3. 让"跑输买入持有"这类结论**可被量化、可被归因**（是成本？是择时？是跳空？）

**判定系统成功与否的标准，不是收益率，而是"证伪效率"。**

---

## 附：本设计与 `开源项目可复用模块分析.md` 的对应关系

| 原报告条目 | 本设计中的落点 |
|---|---|
| `PositionQueue` 移植方案 | §3.2 `PositionLot` / `PositionBook` |
| `AShareCostModel` | §3.2 |
| 涨跌停可交易性 | §3.2 `TradabilityGate` |
| `Account`（区分今仓昨仓盈亏） | §3.2、§5 `AccountSnapshot` |
| `CapitalGainsTaxMixin` | §3.2（标注：个人账户阶段一可不实现） |
| rqalpha 事件驱动 / Mod 机制 | §2、§3.3 |
| vnpy `BaseGateway` / `EventEngine` | §3.3、§3.5 |
| quantstats | §3.6 `PerfReport` |
| vectorbt-skills 方法论 | §3.6、§8.2 |
| qlib 因子与评估口径 | §3.4、§7.2 |
| Phase 0–5 路线 | §10 |

**最高优先级两件事（与原报告一致）**：

1. **自建 A 股规则层**（T+1 + 成本 + 涨跌停）—— 回测可信度的地基
2. **采用 vectorbt-skills 的鲁棒性检验标准** —— 判断策略可用性的标尺



