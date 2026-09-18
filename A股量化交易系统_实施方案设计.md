# A股量化交易系统 · 实施方案设计（决策已定稿）

> 上游：`开源项目可复用模块分析.md`、`A股量化交易系统_架构设计.md`、`A股量化交易系统_实现设计.md`
> 本文是**动手前的最后一份设计**。实施阶段不得偏离本文；偏离需先改本文。
> 阶段一标的：601872.SH 招商轮船

---

## 零、三项决策已定稿

| # | 决策 | 结论 | 落地影响 |
|---|---|---|---|
| 1 | 数据源 | **baostock 打底 + akshare 补充** | 已完成实机核验（§2.1）；`BaostockAdapter` 为首个适配器 |
| 2 | 回测频率 | **日频（`freq="1d"`）** | 不需要 `BarGenerator`；撮合只用 `open/close/high/low` |
| 3 | akquant | **读完源码后再评估** → 见 §1 | **不采用其规则层**；**新增用途：第三方对账引擎** |

---

## 一、akquant 源码评估报告

> 评估对象：`akfamily/akquant` v0.3.61（MIT），已实读源码与官方文档

### 1.1 逐条对照四项评估标准

| 标准 | 结论 | 证据（可复核） |
|---|---|---|
| ① 规则层能否**独立调用**，不依赖引擎全局状态 | **否** | T+1 实现在 Rust 内核：`src/execution/stock.rs` 的 `StockMatcher` + `Position{total, available}`。Python 侧 `python/akquant/risk.py` 的 `apply_risk_config(engine, config)` **必须传入 `engine`**，规则挂在 `engine.risk_manager` 上，无法脱离引擎使用 |
| ② 数据层能否替换为自建 `DataProvider` | **是** | 有 `python/akquant/data.py` / `feed_adapter.py`，接受 DataFrame / 自定义 feed |
| ③ 是否支持 `as_of` 时点语义（防前视） | **否** | 全仓库代码搜索 `as_of` → **命中 0** |
| ④ 能否插入自建 `RiskGate` 作为**唯一下单前闸门** | **部分** | `RiskManager` 在 Rust 内核，Python 侧只能**配置**其内置规则（`max_position_pct` / `sector_concentration` / `max_account_drawdown` / `max_daily_loss` / `stop_loss_threshold`），**无法注入任意自定义逻辑**；自建闸门只能放在策略层前置 |

**四项中 ①③ 为否、④ 为部分 → 不满足采用条件。**

### 1.2 附加关键发现（决定性的）

**发现 A：akquant 明确不自建涨跌停规则表。** 官方文档 `docs/zh/textbook/06_stock_a.md` 原文：

> 「当前版本的 `AKQuant` 默认撮合逻辑**不会内置一套覆盖不同板块、ST 状态与历史时期变化的动态涨跌停规则表**。如果你的数据源已经计算好了 `limit_up`、`limit_down`、`can_buy`、`can_sell` 等字段，可以通过 `Bar.extra` 一并传入…」

源码印证：`python/akquant/config.py` 的 `ChinaStockConfig` **只有 `enforce_tick_size: bool = True` 一个字段**；`src/market/manager.rs` 中检索不到 0.05 / 0.10 / 0.20 / 0.30 等涨跌幅比例或板块分支。

→ **它把 A 股最核心的规则外包给了数据源。而这恰恰是我们必须自建的部分。**

**发现 B：akquant 的 T+1 与成本模型是真实现的**（这点比原报告预期更好）：

- T+1：`Position` 用 `total` / `available` 双状态表达，T 日清算后 `Available_{T+1} = Total_{T,end}`；有专门测试 `tests/test_t_plus_one.py`
- 成本：`commission_rate` / `min_commission` / `stamp_tax_rate` / `transfer_fee_rate` / `slippage` / `volume_limit_pct` 齐全
- 额外能力：`volume_limit_pct`（按 Bar 成交量的百分比限制最大成交量）——**这是"流动性约束"建模，我们的设计里还没有，值得借鉴**

**发现 C：Windows 上可 `pip install` 免 Rust 工具链。** PyPI 提供 `akquant-0.3.61-cp310-abi3-win_amd64.whl`（8.0 MB）等 6 个预编译 wheel。→ **零构建成本**，这让"对账引擎"方案变得非常划算。

### 1.3 最终结论与新增用途

| 用途 | 决定 |
|---|---|
| 用其**规则层**替代自建 | ❌ 不采用（①③ 不满足） |
| 用其作为**主回测引擎** | ❌ 不采用（缺 `as_of`；涨跌停依赖外部字段；风控不可注入） |
| **作为"第三方对账引擎"** | ✅ **采用（P4 阶段引入）** |
| 借鉴其 `volume_limit_pct` 流动性约束 | ✅ 采用（写入 §3.2 撮合规则） |

**"对账引擎"是本轮评估最有价值的产出。** 做法：把同一个策略在两套引擎上各跑一遍，比较净值曲线与成交明细：

- 差异**可被成本/滑点/流动性约束解释** → 自建规则层可信
- 差异**不可解释** → 必有一方有 bug，回测结论作废

这直接命中本项目最核心的痛点（**回测可信度**），且成本仅为 `pip install akquant`。

---

## 二、数据源方案（已实机核验）

> 核验方式：下载 `baostock-0.9.3-py3-none-any.whl` 解包读取 API 与 demo；读取 akshare 源码
> 核验时间：2026-09-17

### 2.1 baostock 能力清单（已核验，可直接依赖）

**日线接口** `query_history_k_data_plus(code, fields, start_date, end_date, frequency, adjustflag)`

已核验的 `fields` 完整列表：

```
date, code, open, high, low, close, preclose, volume, amount,
adjustflag, turn, tradestatus, pctChg, peTTM, pbMRQ, psTTM, pcfNcfTTM, isST
```

`adjustflag`：`"1"` 后复权 / `"2"` 前复权 / `"3"` 不复权（默认）

**字段 → 规则层映射（这是整个方案的地基）**

| baostock 字段 | 规则层用途 | 关键性 |
|---|---|---|
| `preclose` | **涨跌停价的唯一正确基准**（`limit_up = round(preclose × (1+r), 2)`） | ★★★ |
| `tradestatus` | **停牌判定**（0=停牌，1=正常交易） | ★★★ |
| `isST` | **涨跌幅比例**（ST → 5%） | ★★★ |
| `adjustflag` | 标记该行是复权价还是原始价，**不可混用** | ★★★ |
| `turn` | 换手率因子 | ★ |
| `peTTM / pbMRQ / psTTM` | 基本面因子（估值） | ★ |
| `pctChg` | 日涨跌幅（可用于交叉校验） | ★ |

**其他已核验接口（按用途分组）**

| 用途 | 接口 |
|---|---|
| 交易日历 | `query_trade_dates(start_date, end_date)` |
| 复权因子 | `query_adjust_factor(code, ...)` / `query_daily_adjust_factor(date)` |
| **分红送转** | `query_dividend_data(code, year, yearType)` → `CorporateActionHandler` 的数据源 |
| **停牌股名单** | `query_suspended_stocks(date)` |
| **风险警示股名单** | `query_stocks_in_risk(date)` |
| 退市股名单 | `query_terminated_stocks(date)` → 幸存者偏差处理 |
| **板块归属（按日）** | `query_gem_stocks(date)` 创业板 / `query_starst_stocks(date)` 科创板 / `query_st_stocks(date)` ST / `query_ame_stocks(date)` 主板 |
| 标的元数据 | `query_stock_basic(code, code_name)` → 上市/退市日 |
| 行业分类 | `query_stock_industry(code, date)` |
| 指数成分（按日） | `query_hs300_stocks(date)` / `query_sz50_stocks(date)` / `query_zz500_stocks(date)` |
| 财务（季频） | `query_profit_data` / `query_balance_data` / `query_cash_flow_data` / `query_growth_data` / `query_dupont_data` / `query_operation_data` |
| **业绩快报/预告** | `query_performance_express_report` / `query_forecast_report` → **带发布日，是 PIT 友好的高频基本面数据** |
| 宏观 | `query_cpi_data` / `query_ppi_data` / `query_pmi_data` / `query_money_supply_data_month` / `query_deposit_rate_data` / `query_loan_rate_data` |

> **重要**：`query_gem_stocks(date)` 等**带 `date` 参数**的接口意味着板块归属**历史时点可得** → 可以构建 `LimitRuleTable` 的 PIT 版本，避免"用今天的板块分类回测十年前"。

### 2.2 akshare 补充项（**⚠️ 本节曾含一处事实错误，2026-09-18 已纠正**）

**已核验的航运指数接口**（源码 `akshare/economic/macro_china.py`）：

| 函数 | 指数 | 数据源 |
|---|---|---|
| `macro_china_bdti_index()` | **BDTI 原油运输指数** | 东方财富 |
| `macro_china_bcti_index()` / `macro_shipping_bcti()` | BCTI 成品油运输 | 东方财富 |
| `macro_shipping_bdi()` | BDI 波罗的海干散货 | 东方财富 |
| `macro_shipping_bci()` | BCI 海岬型散货 | 东方财富 |
| `macro_shipping_bpi()` | BPI 巴拿马型散货 | 东方财富 |
| `macro_china_bsi_index()` | BSI 超灵便型 | 东方财富 |

> ### ⚠️ 纠正记录（2026-09-18）
>
> **本文档原写「硬限制：akshare 没有 BDTI（原油运输指数），只有 BCTI」——这是错误的。**
>
> 实际核查 akshare 源码，`macro_china_bdti_index()` **存在**，
> 对应东财 `INDICATOR_ID = EMI00107668`。
>
> **实测可得区间**（2026-09-18 验证，直接调东财公开 API）：
>
> | 指数 | 行数 | 区间 | 覆盖 |
> |---|---|---|---|
> | **BDTI** | **6018** | **2001-12-27 ~ 2026-09-17** | **25 年** |
> | BCTI | 6021 | 2001-12-27 ~ 2026-09-17 | 25 年 |
> | BDI | 9541 | 1988-10-19 ~ 2026-09-17 | 38 年 |
> | BCI | 6861 | 1999-04-30 ~ 2026-09-17 | 27 年 |
> | BPI | 6824 | 1998-12-31 ~ 2026-09-17 | 28 年 |
> | BSI | 5075 | 2006-01-03 ~ 2026-09-17 | 20 年 |
>
> **完全覆盖 2016–2026 的回测区间。**
>
> **更好的做法**：数据源是东财公开 API（`datacenter-web.eastmoney.com`），
> **可直接用 `requests` 调用，无需引入 akshare 这个重依赖**。
> 见 `scripts/sync_shipping.py`（已落库 40340 行）。

### 2.3 运价数据的结论（**已修订**）

~~基于三条理由，阶段一不引入运价数据：~~
~~1. 无可用源：BDTI 缺失，BDI/BCTI 与招商轮船原油主业相关性弱~~
~~2. PIT 成本高~~
~~3. 已六次验证无预测力~~

**修订后（2026-09-18）**：

| 原理由 | 复核结果 |
|---|---|
| ~~1. BDTI 无可用源~~ | ❌ **错误**。BDTI 有 25 年历史，直接可调 |
| 2. PIT 成本高 | ⚠️ **部分成立**。数据源只给 `REPORT_DATE`（数据日期），不给发布日 → 只能**保守滞后假设**（`KIND_SHIPPING_INDEX` = 数据日期 + 2 天）。但这与「财报用法定滞后」是同一类处理，**可接受** |
| 3. 已六次验证无预测力 | ⚠️ **需重新审视**。前六次验证若用的是「单标的时序 IC」，则统计功效极低（N=1，噪声下界 1.0），**不足以支撑否定结论** |

**决定**：**运价数据纳入因子库**，但必须满足：

1. **走同一套三重闸**（全空间 Bonferroni + Walk-Forward + 自相关稳健 t 值）
2. **PIT 用保守滞后**（数据日期 + 2 天）
3. **不做参数搜索**（避免过拟合）—— 只用有经济含义的构造：
   - 运价水平的分位（贵/便宜）
   - 运价变化率（趋势）
   - **运价与股价的背离**（基本面已改善但股价未反应）
   - BDTI − BDI 价差（原油 vs 干散货相对强弱）

**关于「霍尔木兹海峡日通航」**：实测几个公开追踪器（TankerMap / hormuz-traffic.com 等）
**只有 ≤1 年历史**，无法用于 2016–2026 回测。
→ **不作为 alpha 因子**，但可作**实盘风控输入**（通航量骤降 = 地缘风险信号）。

### 2.4 落盘格式与目录

```
data_store/
├─ raw/baostock/daily/{symbol}.parquet        # 不复权原始行（含 preclose/tradestatus/isST）
├─ raw/baostock/daily_qfq/{symbol}.parquet    # 前复权（仅用于计算收益）
├─ meta/calendar.parquet                       # 交易日历
├─ meta/instruments.parquet                    # 标的元数据（上市日/退市日）
├─ meta/board_membership.parquet               # 按日的板块归属（PIT）
├─ meta/suspended.parquet                      # 按日的停牌名单（PIT）
├─ meta/risk_warning.parquet                   # 按日的 ST 名单（PIT）
├─ meta/dividends.parquet                      # 分红送转
└─ meta/adjust_factors.parquet                 # 复权因子
```

**存储原则**：

- **原始不复权数据是唯一真相源**（`raw/`），复权数据是派生物（`daily_qfq/`），可随时重算
- 所有 `meta/` 表**必须带日期维度**，不得存"当前快照"
- 增量更新：按 `(symbol, date)` 主键 upsert，禁止整表覆盖

### 2.5 PIT 规则表（可见时点定义）

| 数据类型 | 数据日期含义 | 可见时点规则 | 依据 |
|---|---|---|---|
| 日线 Bar | 交易日 T | `T 日 18:00` | 收盘后结算，保守取 18:00 |
| 交易日历 | — | 立即可见 | 交易所提前公布 |
| 板块归属 / ST / 停牌名单 | 交易日 T | `T 日 09:00`（当日盘前已知） | 交易所盘前公告 |
| 复权因子 / 分红送转 | 除权日 T | `T 日 09:00` | 除权日盘前公布 |
| 业绩快报 / 预告 | **公告日** | 公告日次日 09:00 | baostock 提供公告日，保守 +1 天 |
| 财务季报 | 报告期 | **报告期 + 法定滞后**：一季报/三季报 +30 天，半年报 +60 天，年报 +120 天 | 保守覆盖法定上限 |
| 宏观（CPI/PPI/PMI） | 统计月份 | 次月中旬（保守取 +20 天） | 统计局发布节奏 |
| 运价指数（若引入） | 发布日 | 发布日 + 2 天 | 二手转载，保守 |

**实现**：`PITGuard` 用 `LAG_RULES: dict[str, Callable[[date], datetime]]` 表达上表；任何数据读取必须传 `as_of`。

---

## 三、日频回测的完整设计

### 3.1 时间轴与事件序列（精确定义）

```
T-1 日 18:00   [数据同步]  拉取 T-1 日 Bar 落库
T   日 09:00   [盘前]      读 meta 表（板块/ST/停牌/除权），构建当日规则上下文
T   日 09:30   [开盘]      ——
T   日 15:00   [收盘]      逻辑可见时点：T 日 Bar 完整可知
T   日 15:05   [信号]      on_bar(bar_T) → 因子(as_of=T 15:00) → Signal → OrderRequest
T   日 15:10   [风控]      RiskGate.check(order, ctx_T)
T+1 日 09:25   [执行前]    读 T+1 日开盘价（集合竞价结果）
T+1 日 09:30   [成交]      MatchingEngine 以 T+1 开盘价撮合
T+1 日 15:05   [结算]      Ledger.settle() → 日终净值
```

**三个必须明确的时点定义**：

| 时点 | 含义 | 用途 |
|---|---|---|
| **逻辑可见时点** | `T 日 15:00`（收盘价确定时刻） | **回测**中判断数据是否可用 |
| **实际可得时点** | `T 日 18:00 ~ 20:00`（baostock 更新后） | **实盘**中判断数据是否已到手 |
| **执行时点** | `T+1 日 09:30`（开盘） | 撮合 |

**关键洞察**：回测与实盘的**决策时刻不同**（15:00 vs 18:00），但**执行时刻相同**（T+1 开盘）。因此只要实盘能在 `T+1 09:25` 前完成计算，两者语义一致，无需对回测做任何惩罚性调整。

**⚠️ 由此派生一个必建组件：`DataReadinessCheck`。** 实盘任务在 `T+1 09:25` 前必须校验"T 日数据是否已落库"。若数据未就绪（数据源延迟），**默认动作是放弃当日交易**，而不是用陈旧数据下单。这是实盘与回测产生偏差的常见来源，必须在设计阶段就堵住。

### 3.2 撮合规则（精确定义，逐条可测）

| # | 规则 | 说明 |
|---|---|---|
| 1 | **成交价基准** | `T+1 日开盘价`（`bar_{T+1}.open`）。**禁止用 T 日收盘价成交** |
| 2 | **滑点方向** | 买入 `price × (1 + slippage)`；卖出 `price × (1 − slippage)`。**方向写反 = 回测凭空盈利** |
| 3 | **涨停不可买** | 若 `bar_{T+1}.open >= limit_up` → 买单**拒绝**（记录原因，默认不顺延） |
| 4 | **跌停不可卖** | 若 `bar_{T+1}.open <= limit_down` → 卖单**拒绝** |
| 5 | **一字板** | ⚠️ **已修正（见 §12）**：一字板**涨停**→卖单可成交、买单不可；一字板**跌停**→买单可成交、卖单不可。只有"一字板且**不在**涨跌停价位"才是双向流动性枯竭。原"一字板双向拒绝"的写法是错的 |
| 6 | **停牌** | 若 `tradestatus == 0` → 不可成交 |
| 7 | **流动性约束** | 成交量 `≤ bar_{T+1}.volume × volume_limit_pct`（默认 10%）→ **借鉴 akquant 的 `volume_limit_pct`**，超量部分不成交 |
| 8 | **最小单位** | 买入数量向下取整到 100 股整数倍；取整后为 0 则不下单 |
| 9 | **资金约束** | 现金不足 → 按可用资金**缩减数量**（可配置为"拒绝"） |
| 10 | **T+1 约束** | 卖出数量 `≤ PositionBook.sellable` |
| 11 | **部分成交** | 受规则 7 限制时产生部分成交，剩余部分默认**撤销**（不顺延） |
| 12 | **费用不足** | 卖出所得连费用都盖不住 → 拒单（防现金转负） |

**规则 3–6 的判定顺序必须是**：停牌 → 一字板 → 涨跌停 → 流动性。顺序错误会导致误判。

### 3.3 涨跌停判定算法

```
limit_up   = round_half_up(preclose × (1 + r), 2)
limit_down = round_half_up(preclose × (1 - r), 2)
```

`r` 由 `LimitRuleTable` 给出，**必须时间感知**：

| 板块 | 代码前缀 | 正常 | ST/*ST | 生效起点 |
|---|---|---|---|---|
| 主板 | 600/601/603/605/000/001/002/003 | 10% | **5%** | 长期 |
| 创业板 | 300/301 | 20% | 20% | 2020-08-24 |
| 科创板 | 688 | 20% | 20% | 2019-07-22 |
| 北交所 | 8xx/43x | 30% | 30% | 2021-11-15 |
| 创业板（历史） | 300 | 10% | 5% | 2020-08-24 之前 |
| **新股上市窗口** | — | **不设涨跌幅** | — | 主板 2023-02-17 后前 5 个交易日；创业板/科创板前 5 个交易日 |

**阶段一（601872 主板非 ST）只需 10%**，但规则表按上表完整实现——这是"接口不能砍"原则的具体落点。

**⚠️ 判定必须用【不复权价 + `preclose`】**。`preclose` 是除权后的昨收，正是涨跌停的正确基准。用前复权价算涨跌停会得到错误结果。

### 3.4 复权处理原则（本设计中最容易出错的一处）

**核心事实**：前复权（qfq）以**最新日为基准**，每次新分红都会**重算全部历史价格**；后复权（hfq）以**最早日为基准**，历史价格**永不变化**。

→ **用前复权序列做回测会引入未来信息**（2026 年的分红改变了 2016 年的价格水平）。

**因此严格分工**：

| 用途 | 使用价格 | 理由 |
|---|---|---|
| 因子计算、收益率、均线 | **后复权（`adjustflag="1"`）** | 历史不变，可增量追加，无前视 |
| 成交价、成交金额、股数、费用 | **不复权（`adjustflag="3"`）+ `preclose`** | 与真实成交一致 |
| 涨跌停判定 | **不复权 + `preclose`** | 唯一正确基准 |
| 账户市值、净值 | **不复权** | 与券商对账单口径一致 |
| 给人看"当前价格" | 前复权（仅展示，**禁止进入计算**） | — |

**落盘时三类数据必须分开存储**，且在 `Bar` 对象上用 `adjust_flag` 字段显式标记，**禁止混用**。

### 3.5 除权除息处理

**除权日 `CorporateActionHandler` 动作**（数据源：baostock `query_dividend_data`）：

| 事件 | 处理 |
|---|---|
| 送股/转增（比例 `ratio`） | 批次数量 `× (1 + ratio)`；成本价 `÷ (1 + ratio)`；总成本不变 |
| 现金分红（每股 `cash`，税前） | 现金流入 `= 持股数 × cash` |
| 配股 | **阶段一不支持**（招商轮船无配股预期；如出现则标记该段回测不可用） |

**已知简化（登记入风险册）**：现金分红按**税前**计入。个人红利税实行差异化征收（持股 >1 年免征，1 个月~1 年减按 50% 计入，<1 个月全额计入），且**在卖出时补缴**，与持有期挂钩。精确建模会显著增加归因复杂度，阶段一按税前计入，并在报告中显式标注。

### 3.6 成本计算顺序（固定顺序，不可调换）

**买入**：

```
成交金额 = 数量 × 成交价(含滑点)
佣金     = max(成交金额 × 0.00025, 5.0)      ← 最低 5 元
过户费   = 成交金额 × 0.00001
现金流出 = 成交金额 + 佣金 + 过户费
```

**卖出**：

```
成交金额 = 数量 × 成交价(含滑点)
佣金     = max(成交金额 × 0.00025, 5.0)
印花税   = 成交金额 × 0.0005                 ← 仅卖出方
过户费   = 成交金额 × 0.00001
现金流入 = 成交金额 - 佣金 - 印花税 - 过户费
```

**往返成本率**（不含滑点）≈ `0.00025×2 + 0.0005 + 0.00001×2 = 0.111%`；含滑点 `0.05%×2` → **≈ 0.211%**。

**费用必须逐项落库**（`Trade.commission / stamp_tax / transfer_fee / slippage_cost`），禁止只记一个总额——否则 P4 的成本归因无法做。

---

## 四、模块详细设计

> 每个模块给出：**职责 / 接口 / 核心算法 / 测试用例**。测试用例即验收标准。

### 4.1 P1 · `rules/` — A股规则层（最高优先级）

#### 4.1.1 `rules/cost.py`

```python
@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.00025
    min_commission:  float = 5.0
    stamp_tax_rate:  float = 0.0005      # 仅卖出
    transfer_rate:   float = 0.00001
    slippage:        float = 0.0005

@dataclass(frozen=True)
class FeeDetail:
    amount: float          # 成交金额（含滑点）
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage_cost: float   # 滑点造成的金额差，单独记录
    total: float

class AShareCostModel:
    def __init__(self, cfg: CostConfig) -> None: ...
    def apply_slippage(self, price: float, side: Side) -> float: ...
    def buy(self, qty: int, price: float) -> FeeDetail: ...
    def sell(self, qty: int, price: float) -> FeeDetail: ...
    def round_trip_rate(self, amount: float) -> float: ...
```

**核心算法**：见 §3.6 的固定顺序。`slippage_cost = qty × |含滑点价 − 原始价|`，必须单独累计（用于 P4 的成本归因）。

**测试用例**

| # | 用例 | 期望 |
|---|---|---|
| 1 | 买入 100 股 @10 元 | 佣金 = 5.0（触底，非 0.25），过户费 = 0.01 |
| 2 | 买入 10000 股 @10 元 | 佣金 = 25.0（0.00025×100000），过户费 = 1.0 |
| 3 | 卖出 100 股 @10 元 | 含印花税 0.5；佣金触底 5.0 |
| 4 | 往返成本率 @100000 元 | ≈ 0.00111（误差 < 1e-4） |
| 5 | 滑点方向 | 买入价 > 原价；卖出价 < 原价（**方向反了就 fail**） |
| 6 | 小资金 1000 元往返 | 成本率 > 1%（验证最低佣金对小资金的侵蚀） |

#### 4.1.2 `rules/position.py`

```python
@dataclass
class PositionLot:
    open_date: date
    quantity: int
    cost_price: float          # 不复权实际成交价

class PositionBook:
    def __init__(self) -> None: ...
    def before_trading(self, d: date) -> None: ...      # 结转昨仓、清冻结
    @property
    def total(self) -> int: ...
    @property
    def today_bought(self) -> int: ...
    @property
    def sellable(self) -> int: ...                       # total - today_bought - frozen
    def freeze(self, qty: int) -> None: ...
    def unfreeze(self, qty: int) -> None: ...
    def buy(self, d: date, qty: int, price: float) -> None: ...
    def sell(self, d: date, qty: int, price: float) -> list[tuple[date, int, float]]: ...
    def apply_corporate_action(self, ratio: float) -> None: ...
```

**核心算法**（FIFO 平仓，思路借鉴 rqalpha，代码自写）：

```
sell(qty):
    if qty > sellable: raise T1Violation
    while qty > 0:
        lot = lots[0]
        take = min(lot.quantity, qty)
        details.append((lot.open_date, take, lot.cost_price))
        lot.quantity -= take; qty -= take
        if lot.quantity == 0: lots.popleft()
    return details

buy(d, qty, price):
    if lots and lots[-1].open_date == d:
        # 同日加仓：加权平均成本
        w = (lots[-1].quantity * lots[-1].cost_price + qty * price) / (lots[-1].quantity + qty)
        lots[-1].quantity += qty; lots[-1].cost_price = w
    else:
        lots.append(PositionLot(d, qty, price))
```

**测试用例**

| # | 用例 | 期望 |
|---|---|---|
| 1 | T 日买入 1000 → 当日卖 100 | 抛 `T1Violation`（sellable=0） |
| 2 | T 日买 1000，T+1 卖 600 | 成功，剩 400，FIFO 明细正确 |
| 3 | T 日买 1000，T+1 买 500，T+1 卖 1200 | 成功（sellable=1000），剩 300 为 T+1 批次 |
| 4 | T 日买 500，T+1 买 500，T+1 卖 700 | FIFO：先平 T 日 500，再平 T+1 的 200 |
| 5 | 部分平仓明细 | `len(details)` 与批次拆分一致 |
| 6 | 冻结后 sellable | `total - today_bought - frozen` |
| 7 | 同日加仓成本 | 加权平均，非简单相加 |
| 8 | 全部卖出后再卖 | 抛 `T1Violation` |

#### 4.1.3 `rules/tradability.py`

```python
class LimitRuleTable:
    def get_ratio(self, inst: Instrument, d: date) -> float | None: ...   # None = 不设涨跌幅
    def is_new_listing_window(self, inst: Instrument, d: date) -> bool: ...

class TradabilityGate:
    def __init__(self, table: LimitRuleTable) -> None: ...
    def check(self, bar: Bar, side: Side, qty: int,
              pos: PositionBook) -> Verdict: ...
```

**判定顺序（必须严格按此序）**：

```
1. bar.is_suspended              → 拒绝 "停牌"
2. bar.high == bar.low           → 拒绝 "一字板"
3. side==BUY  and bar.open >= limit_up   → 拒绝 "涨停无法买入"
   side==SELL and bar.open <= limit_down → 拒绝 "跌停无法卖出"
4. qty > bar.volume * volume_limit_pct   → 部分成交（裁剪数量）
5. 否则 → 通过
```

**测试用例**

| # | 用例 | 期望 |
|---|---|---|
| 1 | 主板非 ST，`preclose=10` | `limit_up=11.00`, `limit_down=9.00` |
| 2 | 主板 ST | `limit_up=10.50` |
| 3 | 创业板 2021 年 | `limit_up=12.00`（20%） |
| 4 | 创业板 2019 年 | `limit_up=11.00`（10%，历史规则） |
| 5 | 涨停价开盘 + 买单 | 拒绝，原因含"涨停" |
| 6 | 涨停价开盘 + 卖单 | **通过**（涨停可以卖） |
| 7 | 一字板 | 买卖均拒绝 |
| 8 | `tradestatus=0` | 拒绝"停牌" |
| 9 | 成交量约束 | 数量被裁剪到 `volume × 10%` |
| 10 | 四舍五入 | `preclose=10.03` → `limit_up=11.03`（非 11.033） |

#### 4.1.4 `rules/account.py` 与 `rules/portfolio.py`

```python
class Account:
    cash: float
    frozen: float
    def rollover(self) -> None: ...                 # 日初：卖出资金解冻
    def on_trade(self, t: Trade) -> None: ...
    def settle(self, portfolio: Portfolio, px: dict[str, float]) -> AccountSnapshot: ...

class Portfolio:
    books: dict[str, PositionBook]                   # 即使阶段一只有 1 个 key
    def book(self, symbol: str) -> PositionBook: ...
    def market_value(self, px: dict[str, float]) -> float: ...
```

**盈亏双口径**（借鉴 rqalpha）：

- `trading_pnl`：当日成交部分的浮动盈亏
- `position_pnl`：昨仓部分的浮动盈亏

**测试用例**：日初结转、卖出资金解冻、`market_value` 正确、`trading_pnl + position_pnl == 总盈亏`。

#### 4.1.5 `rules/corporate_action.py`

```python
class CorporateActionHandler:
    def apply(self, book: PositionBook, ca: CorporateAction) -> float:
        """返回现金分红流入；同时调整批次数量与成本"""
```

**测试用例**：10 送 3 → 数量 ×1.3、成本 ÷1.3、总成本不变；每股派 0.5 元 → 现金流入 = 股数 × 0.5；**除权日涨跌停用 `preclose` 而非复权价**。

---

### 4.2 P2 · `data/` — 数据层

#### 4.2.1 `data/objects.py`

`Instrument`（含 `limit_ratio` 推导）、`Bar`（含 `preclose / limit_up / limit_down / tradestatus / is_st / adjust_flag`）。字段定义见前文实现设计文档 §3.2，此处不再重复。

**新增约束**：`Bar` 增加 `adjust_flag: Literal["none","qfq","hfq"]`，**不同 `adjust_flag` 的 Bar 禁止在同一个计算上下文中混用**（由 `test_architecture.py` 断言）。

#### 4.2.2 `data/provider.py`

```python
class DataProvider(ABC):
    def get_bars(self, symbol, freq, start, end, adjust) -> list[Bar]: ...
    def get_instrument(self, symbol) -> Instrument: ...
    def get_calendar(self, start, end) -> list[date]: ...
    def get_board_membership(self, symbol, d) -> Board: ...
    def get_suspended(self, d) -> set[str]: ...
    def get_risk_warning(self, d) -> set[str]: ...
    def get_dividends(self, symbol, start, end) -> list[CorporateAction]: ...

PROVIDERS: dict[str, type[DataProvider]] = {}
```

#### 4.2.3 `data/adapters/baostock_src.py`

**已核验的字段映射**（§2.1）：

| baostock | Bar 字段 | 转换 |
|---|---|---|
| `date` | `dt` | → `datetime`（+15:00） |
| `open/high/low/close` | 同名 | `float` |
| `preclose` | `prev_close` | `float`，**涨跌停基准** |
| `volume` | `volume` | 股（baostock 单位：股） |
| `amount` | `amount` | 元 |
| `tradestatus` | `is_suspended` | `"0"` → `True` |
| `isST` | `is_st` | `"1"` → `True` |
| `adjustflag` | `adjust_flag` | `"1"`→hfq / `"2"`→qfq / `"3"`→none |

`limit_up/limit_down` **不由适配器计算**，由 `LimitRuleTable` 在构建上下文时填充（职责分离）。

**登录会话**：baostock 需 `login()` / `logout()`，适配器用上下文管理器封装，**失败重试 3 次，超时 30s**。

#### 4.2.4 `data/cache.py` 与 `data/pit.py`

```python
class DataCache:
    def upsert(self, table: str, rows: list[dict], keys: tuple[str, ...]) -> int: ...
    def read(self, table: str, **filters) -> pd.DataFrame: ...

class PITGuard:
    LAG_RULES: dict[str, Callable[[date], datetime]]   # 见 §2.5
    def visible(self, data_dt: date, as_of: datetime, kind: str) -> bool: ...
    def filter(self, rows, as_of: datetime, kind: str) -> list: ...
    def assert_no_lookahead(self, rows, as_of: datetime, kind: str) -> None:
        """开发期强制断言：若发现未来数据，直接抛异常而非静默过滤"""
```

**测试用例**

| # | 用例 | 期望 |
|---|---|---|
| 1 | `as_of = T 15:00`，读取 T 日 Bar | 可见 |
| 2 | `as_of = T 14:00`，读取 T 日 Bar | **不可见** |
| 3 | `as_of = T`，读取 T+1 财务报告 | 不可见 |
| 4 | 财报滞后 | 年报在报告期 +120 天内不可见 |
| 5 | 业绩快报（有公告日） | 公告日 +1 天可见 |
| 6 | `assert_no_lookahead` 触发 | 抛异常（不是静默过滤） |
| 7 | `upsert` 幂等 | 重复写入不产生重复行 |
| 8 | 增量更新 | 历史行不被修改 |

---

### 4.3 P3 · `core/` — 引擎层

```python
class EventBus:
    def subscribe(self, etype: str, handler: Callable[[Event], None]) -> None: ...
    def publish(self, ev: Event) -> None: ...

class Clock:
    def __init__(self, calendar: list[date], mode: Literal["backtest","live"]) -> None: ...
    def advance(self) -> date | None: ...
    @property
    def now(self) -> datetime: ...

class MatchingEngine:
    def __init__(self, cost: AShareCostModel, gate: TradabilityGate,
                 volume_limit_pct: float = 0.10) -> None: ...
    def match(self, orders: list[OrderRequest], bar: Bar,
              book: PositionBook, cash: float) -> list[Trade]: ...

class Ledger:
    def on_before_trading(self, d: date) -> None: ...
    def on_trade(self, t: Trade) -> None: ...
    def settle(self, d: date) -> AccountSnapshot: ...

class Recorder:
    def snapshot(self, d: date, **state) -> None: ...
    def flush(self) -> None: ...
```

**`MatchingEngine.match` 算法**：

```
for order in orders (按 submit_seq 排序):
    v = gate.check(bar, order.side, order.qty, book)
    if not v.passed: record_reject(order, v.reason); continue
    qty = v.qty                      # 可能被流动性裁剪
    px  = cost.apply_slippage(bar.open, order.side)
    fee = cost.buy/sell(qty, px)
    if order.side == BUY:
        qty = min(qty, floor(available_cash_after_fee / px / 100) * 100)
        if qty == 0: record_reject(order, "资金不足"); continue
    else:
        qty = min(qty, book.sellable)
    trades.append(Trade(...))
```

**测试用例**

| # | 用例 | 期望 |
|---|---|---|
| 1 | 确定性 | 同输入两次运行，净值序列**逐点相等** |
| 2 | 成交价 | 等于 `bar.open × (1±slippage)`，**不是 close** |
| 3 | 资金不足 | 数量被缩减到 100 的倍数，或拒单（可配置） |
| 4 | 冻结与解冻 | 未成交订单撤销后资金/持仓解冻 |
| 5 | 结算 | `cash + market_value == total` |
| 6 | 涨停日买单 | 不产生 Trade，产生 Reject 记录 |

---

## 四（续）、P4–P8 模块设计

### 4.4 P4 · `evaluation/` — 评估层（**必须早于策略层**）

#### 4.4.1 `evaluation/perf.py`

```python
class PerfReport:
    def __init__(self, nav: pd.Series, benchmark: pd.Series) -> None: ...
    def metrics(self) -> dict[str, float]: ...      # 封装 quantstats
    def html(self, path: str) -> None: ...
```

**强制要求**：`benchmark` 为**必填参数**，禁止只报绝对收益。招商轮船的基准 = 自身 B&H；同时报告 `query_hs300_stocks` 与中证航运指数（若可得）。

#### 4.4.2 `evaluation/pitfalls.py` — 10 项自检清单（可执行断言）

| # | 检查项 | 断言实现 |
|---|---|---|
| 1 | 前视偏差 | 全流程 `PITGuard.assert_no_lookahead` 无异常 |
| 2 | 未来函数 | 成交价 ≠ 信号日收盘价；`MatchingEngine` 用的是 `bar_{T+1}.open` |
| 3 | 幸存者偏差 | 标的在回测区间内**未退市**；`query_terminated_stocks` 校验 |
| 4 | 样本内外混用 | 参数只在样本内选，样本外只读一次（由配置标记记录） |
| 5 | 多重检验 | 记录试过的参数组合数 `N`，显著性阈值按 `N` 校正 |
| 6 | 成本低估 | 滑点 ≠ 0；往返成本率 ≈ 0.211% |
| 7 | 数据窥探 | 因子列表中不含"已知结论"（如 BDTI 无预测力）本身 |
| 8 | 回撤期外推 | 回测区间含 ≥1 次完整油运周期（≥4 年） |
| 9 | 不可交易性 | 涨停/停牌日的订单全部被拒且有记录 |
| 10 | 参数敏感性 | 参数热力图存在宽稳定区（非孤立尖峰） |

#### 4.4.3 `evaluation/reconcile.py` — **akquant 对账引擎**（本轮新增）

```python
class ReconcileEngine:
    """把同一策略在自建引擎与 akquant 上各跑一遍，比较净值与成交"""
    def run_both(self, strategy_cfg, data) -> tuple[Result, Result]: ...
    def compare(self) -> ReconcileReport: ...
```

**判定标准**（⚠️ **本节已被 §11.1 修正**：akquant 默认撮合器**不做涨跌停/停牌判定**，因此对账必须"喂已裁剪订单"）：

| 差异幅度 | 结论 |
|---|---|
| 8 项语义一致规则内差异 < 0.5% 且成交笔数一致 | ✅ 自建规则层可信 |
| 差异可被成本/滑点/流动性约束解释 | ⚠️ 可接受，需记录差异来源 |
| 差异不可解释 | ❌ **回测结论作废，先修 bug** |

**对账范围**：仅限 §11.1 表中标 ✅ 的 **8 项**（成本 / 滑点 / 价格基准 / tick 对齐 / 手数 / 成交量约束 / 资金持仓 / T+1）。
**涨跌停、停牌、一字板不在对账范围**——以自建引擎为唯一权威。

**依赖**：`pip install akquant`（Windows 有预编译 wheel，免 Rust 工具链）。
**前置**：必须先完成 **P4-0 对账探针**（见 §6 与 §11.1）。

---

### 4.5 P5 · `strategy/` — 策略层

```python
class FactorRegistry:
    def register(self, spec: FactorSpec) -> None: ...
    def compute(self, names: list[str], data: pd.DataFrame,
                as_of: datetime) -> pd.DataFrame: ...

class SignalGenerator:
    def generate(self, factors: pd.DataFrame, ctx: Context) -> Signal | None: ...

class PositionSizer:
    def size(self, signal: Signal, account: Account,
             book: PositionBook, bar: Bar) -> OrderRequest | None: ...
```

**阶段一因子清单（全部 PIT 清晰、来自 baostock）**

| 类别 | 因子 | 数据来源 | PIT |
|---|---|---|---|
| 动量 | 20/60/120 日收益率 | hfq 收盘价 | 15:00 |
| 趋势 | 收盘价 / MA20、MA60 | hfq | 15:00 |
| 波动 | 20 日已实现波动率 | hfq 收益率 | 15:00 |
| 量能 | 换手率 `turn` 及其分位 | baostock `turn` | 15:00 |
| 量价 | 量价背离（价涨量缩） | hfq + volume | 15:00 |
| 估值 | `pbMRQ` / `peTTM` 历史分位 | baostock | 财报滞后 |
| 季节性 | 月份哑变量、油运旺季（Q4–Q1） | 日历 | 立即可见 |
| 事件 | 业绩快报/预告超预期 | `query_performance_express_report` | 公告日+1 |

**`PositionSizer` 输出规则**：目标仓位 → 目标股数 → 与当前持仓差额 → 拆成买/卖单 → **向下取整到 100 股**。取整后差额 < 100 股则不下单（避免高频微调被成本吃掉）。

**测试用例**：因子无前视（`as_of` 校验）；同因子两次计算结果一致；`PositionSizer` 在 T+1 约束下不产生不可执行的卖单。

---

### 4.6 P6 · `execution/` — 执行层

```python
class RiskRule(ABC):
    def check(self, order: OrderRequest, ctx: Context) -> RiskVerdict: ...

class RiskGate:
    def __init__(self, rules: list[RiskRule]) -> None: ...
    def check(self, order, ctx) -> RiskVerdict: ...
```

**阶段一风控规则**：

| 规则 | 阈值（可配） | 拒绝原因 |
|---|---|---|
| 单笔金额上限 | ≤ 总资产 100% | `MAX_ORDER_VALUE` |
| 单标的仓位上限 | ≤ 100%（单标的阶段形同虚设，但接口保留） | `MAX_POSITION_PCT` |
| 可卖量 | ≤ `PositionBook.sellable` | `T1_VIOLATION` |
| 资金 | ≤ 可用现金 | `INSUFFICIENT_CASH` |
| 涨跌停/停牌 | `TradabilityGate` | `NOT_TRADABLE` |
| 最小单位 | 100 股整数倍 | `LOT_SIZE` |
| 日换手上限 | ≤ 总资产 30% / 日 | `DAILY_TURNOVER_LIMIT` |
| 连续亏损熔断 | 连续 5 日亏损 → 停止开仓 | `CONSECUTIVE_LOSS_CIRCUIT` |

**`OrderManager` 状态机**：`CREATED → SUBMITTED → PARTIAL → FILLED / REJECTED / CANCELLED`。**每个状态转换必须落库**。

---

### 4.7 P7 · 鲁棒性验证

```python
class WalkForward:
    def run(self, strategy_factory, data, n_splits: int) -> WFEReport: ...
    # WFE = 样本外收益 / 样本内收益，> 0.5 才算通过

class MonteCarlo:
    def shuffle_trades(self, n: int = 1000) -> Distribution: ...
    def inject_noise(self, sigma: float, n: int = 1000) -> Distribution: ...
    def delay_execution(self, days: int) -> Distribution: ...   # 执行延迟 1/2/3 日
    def param_sensitivity(self, grid: dict) -> pd.DataFrame: ...
```

**执行延迟测试尤其重要**：本设计假设 `T+1` 开盘成交。若把延迟改为 `T+2`、`T+3` 后收益崩塌，说明策略依赖短期价格延续 → 实际不可用。这是"跳空吃掉收益"教训的工程化版本。

---

### 4.8 P8 · `app/` — 应用层

```python
# app/cli.py
do-modle sync    --symbol 601872.SH --start 2015-01-01   # 数据同步
do-modle backtest --config config/stock_601872.yaml       # 回测
do-modle report   --run-id <id>                           # 生成报告
do-modle reconcile --run-id <id>                          # akquant 对账
do-modle signal   --date 2026-09-17                       # 出信号（不下单）

# app/daily_job.py
def daily_job(d: date) -> None:
    1. DataReadinessCheck(d)          # 数据未就绪 → 中止（默认放弃当日交易）
    2. 构建当日规则上下文（板块/ST/停牌/除权）
    3. 计算因子 → 信号 → 目标仓位
    4. RiskGate.check
    5. 输出信号（阶段一~三：人工执行，不自动下单）
    6. 落库 + 生成日报
```

---

## 五、配置设计

`config/base.yaml`：

```yaml
data:
  provider: baostock
  store: ./data_store
  start: "2015-01-01"
  price_for_factor: hfq          # 因子用后复权
  price_for_account: none        # 账户用不复权

cost:
  commission_rate: 0.00025
  min_commission: 5.0
  stamp_tax_rate: 0.0005
  transfer_rate: 0.00001
  slippage: 0.0005

matching:
  execution_price: next_open     # 禁止改为 same_close
  volume_limit_pct: 0.10
  partial_fill_policy: cancel    # cancel | carry_over
  insufficient_cash_policy: shrink   # shrink | reject

pit:
  daily_bar: "15:00"
  financial_report_lag_days: {q1: 30, q3: 30, h1: 60, annual: 120}
  express_report_lag_days: 1

risk:
  max_order_value_pct: 1.0
  max_position_pct: 1.0
  daily_turnover_limit_pct: 0.30
  consecutive_loss_circuit: 5
```

`config/stock_601872.yaml`：

```yaml
symbol: "sh.601872"          # baostock 格式
name: 招商轮船
board: MAIN
benchmark: self_buy_and_hold
backtest:
  start: "2016-01-01"
  end:   "2026-09-01"
  initial_cash: 200000
  in_sample_end: "2021-12-31"    # 样本内/外切分，只允许看一次样本外
```

---

## 六、实施步骤（每步做什么 / 产出 / 验收 / 规模）

> 规模用代码量级标注（S < 200 行，M 200–500 行，L > 500 行），**不用时间估计**。

### P0 · 骨架与环境（S）

| 项 | 内容 |
|---|---|
| 做什么 | 创建包结构；`config/` 加载器；日志；`test_architecture.py`（4 条 import 禁令断言）；隔离环境 `~/.workbuddy-ai/binaries/python/envs/default` |
| 产出 | 可导入的空包 + 配置加载器 + 架构断言测试 |
| 验收 | `pytest tests/test_architecture.py` 全绿；`do-modle --version` 可运行 |
| 依赖安装 | `baostock==0.9.3`、`pandas`、`numpy`、`pyarrow`、`pyyaml`、`quantstats`、`pytest` |

### P1 · `rules/` 规则层（L）—— **不可跳过**

| 项 | 内容 |
|---|---|
| 做什么 | `cost.py` / `position.py` / `tradability.py` / `corporate_action.py` / `account.py` / `portfolio.py` + 全部单测 |
| 产出 | 零外部依赖的规则层（只 import `data/objects.py`） |
| 验收 | **§4.1 全部 30+ 测试用例通过**；往返成本率误差 < 1e-4 |
| 硬门槛 | 涨停不可买、T+1 超卖被拒、除权数量正确 —— 三项任一失败则**禁止进入 P2** |

### P2 · `data/` 数据层（L）

| 项 | 内容 |
|---|---|
| 做什么 | `objects.py` / `provider.py` / `adapters/baostock_src.py` / `cache.py` / `pit.py` |
| 产出 | 601872 的完整历史数据落库（不复权 + 后复权 + meta 表） |
| 验收 | §4.2 全部 8 个 PIT 用例通过；`as_of` 调早一天时未来数据不可见；增量 upsert 幂等 |
| 首次实机任务 | `do-modle sync --symbol sh.601872 --start 2016-01-01` 跑通并落库 |

### P3 · `core/` 引擎层（L）

| 项 | 内容 |
|---|---|
| 做什么 | `event.py` / `clock.py` / `matching.py` / `ledger.py` / `recorder.py` |
| 产出 | 可跑完整日频回测的引擎 |
| 验收 | §4.3 全部 6 个用例通过；**同输入两次运行净值逐点相等** |

### P4-0 · 对账探针（S）—— **必须在 P4 前完成**

| 项 | 内容 |
|---|---|
| 做什么 | 构造 5 个边界场景（涨停买单 / 停牌买单 / 超额订单 / T+1 当日回转 / 正常订单），分别在自建引擎与 akquant 上运行，**记录实际行为** |
| 产出 | `probe_report.md` |
| 验收 | 5 个场景的两引擎行为已记录；据此确认对账范围（§11.1） |
| 为什么必须前置 | akquant 默认撮合器**已核验不做涨跌停/停牌判定**。探针用于确认对账子集边界，避免把"规则语义不一致"误判为 bug |

### P4 · 评估层 + **MVP 里程碑**（M）

| 项 | 内容 |
|---|---|
| 做什么 | `perf.py`（quantstats 封装）/ `pitfalls.py`（10 项断言）/ `reconcile.py`（akquant 对账，喂已裁剪订单） |
| 策略 | 用 `BuyAndHoldStrategy`（`target_weight = 1.0`）做冒烟测试 |
| **核心验收** | **B&H 回测净值与"手工持有"的差异，必须能被已知成本完全解释（差异 < 1bp）** |
| 二次验收 | akquant 对账：净值差异 < 0.5% 或差异可解释 |
| 意义 | **这是第一个能产出"确定结论"的里程碑。** 到此为止，系统已具备"可信地证伪一个策略"的能力 |

> **P4 的 B&H 校验是本方案设计的核心技巧。** B&H 有解析解，任何偏差都只能是 bug。若跳过这一步直接写因子，就是在不确定的地基上盖楼——这正是前六轮的结构性问题。

### P5 · `strategy/` 策略层（M）

| 项 | 内容 |
|---|---|
| 做什么 | `FactorRegistry` + 因子库（动量/趋势/波动/量能/估值/季节性/事件）+ `SignalGenerator` + `PositionSizer`；**新增研究轨 `research/`：航运板块截面 IC（N≥10，见 §11.2）** |
| 验收 | 每个因子通过 `as_of` 无前视校验；**产出 IC_IR（非仅时序 IC）**；`PositionSizer` 不产生不可执行卖单、**不主动拆单** |
| 纪律 | **不得引入运价数据**（§2.3）；**不得 import `evaluation`**；**研究轨不得进入交易链路** |

### P6 · `execution/` 执行层（M）

| 项 | 内容 |
|---|---|
| 做什么 | `RiskGate` + 8 条风控规则 + `OrderManager` 状态机 + `BacktestGateway` |
| 验收 | 涨停买单被拒、T+1 超卖被拒、超资金被拒、日换手上限触发、熔断触发 —— 全部有拒绝记录 |

### P7 · 鲁棒性验证（M）

| 项 | 内容 |
|---|---|
| 做什么 | `WalkForward`（WFE）+ `MonteCarlo`（打乱/噪声/**执行延迟**/参数敏感性） |
| 验收 | 参数热力图存在**宽稳定区**；执行延迟 1/2/3 日的收益衰减曲线已产出 |
| 输出 | 一份"策略可用性判定报告"，含明确的可用/不可用结论 |

### P8 · 应用层与实盘（M）

| 项 | 内容 |
|---|---|
| 做什么 | `cli.py` / `daily_job.py`（含 `DataReadinessCheck`）/ `live` 通道（按券商条件二选一） |
| 验收 | 纸面交易 ≥1 个月，逐笔对账一致 |
| 默认 | **阶段一~三默认"系统出信号、人工执行"**，不开启自动下单 |

---

## 七、验收总表（逐项打勾）

| # | 门槛 | 归属 | 状态 |
|---|---|---|---|
| 1 | 4 条 import 禁令自动断言通过 | P0 | ☐ |
| 2 | T+1 超卖被拒 | P1 | ☐ |
| 3 | 涨停不可买 / 跌停不可卖 | P1 | ☐ |
| 4 | 停牌、一字板被拒 | P1 | ☐ |
| 5 | 往返成本率 ≈ 0.211%（含滑点） | P1 | ☐ |
| 6 | 除权后数量与成本正确 | P1 | ☐ |
| 7 | `as_of` 调早一天时未来数据不可见 | P2 | ☐ |
| 8 | 增量 upsert 幂等，历史行不被改 | P2 | ☐ |
| 9 | 同输入两次运行净值逐点相等 | P3 | ☐ |
| 10 | 成交价 = `bar_{T+1}.open × (1±slippage)` | P3 | ☐ |
| 11 | **B&H 回测与手工持有的差异 < 1bp** | P4 | ☐ |
| 12 | akquant 对账差异 < 0.5% 或可解释 | P4 | ☐ |
| 13 | 10 项陷阱清单全部通过 | P4 | ☐ |
| 14 | 每个因子通过无前视校验 | P5 | ☐ |
| 15 | 8 条风控规则全部可触发并有记录 | P6 | ☐ |
| 16 | 参数热力图存在宽稳定区 | P7 | ☐ |
| 17 | 执行延迟 1/2/3 日衰减曲线已产出 | P7 | ☐ |
| 18 | 纸面交易逐笔对账一致 | P8 | ☐ |
| 19 | 换 `symbol` 不改代码可跑通 | P8 | ☐ |

**第 11 与第 19 项是本方案的两根支柱**：前者保证**回测可信**，后者保证**架构可扩展**。

---

## 八、风险登记册

| # | 风险 | 影响 | 缓解措施 | 状态 |
|---|---|---|---|---|
| 1 | **策略无预测力**（已六次验证） | 系统建成后仍无可用策略 | 设计目标改为"证伪效率"；用 Monte Carlo + 执行延迟测试提高证据强度 | **已知，接受** |
| 2 | 单标的统计功效不足（`t ≈ SR×√年数`，SR<0.45 时 20 年样本无法显著） | 结论无法达到统计显著 | 增加独立样本维度（跨参数/跨窗口/跨市场状态）；不把样本内表现当证据 | **已知，接受** |
| 3 | 前复权数据引入未来信息 | 回测虚高 | **回测强制用后复权**（§3.4）；三类价格分离存储 | 已缓解 |
| 4 | baostock 服务中断 / 接口变更 | 数据管道断裂 | 适配器隔离 + 本地 Parquet 缓存；P2 阶段即落库 | 已缓解 |
| 5 | akshare 接口不稳定 | 补充数据不可用 | akshare 只用于非关键路径；运价数据已排除 | 已缓解 |
| 6 | **BDTI 无可用数据源** | 无法验证原油运价因子 | 阶段一不引入运价因子（§2.3） | **已决策排除** |
| 7 | 红利税未精确建模 | 分红收益略高估 | 按税前计入 + 报告显式标注 | 已接受（登记） |
| 8 | 配股不支持 | 若发生配股则该段回测不可用 | 检测到配股即标记该区间不可用 | 已接受 |
| 9 | 实盘数据延迟导致用陈旧数据下单 | 实盘与回测偏差 | `DataReadinessCheck`：未就绪则**放弃当日交易** | 已缓解 |
| 10 | 招商轮船单一标的的事件风险（制裁/油价冲击/地缘） | 跳空，止损失效 | 设计上不依赖止损；用仓位上限控制暴露 | 已接受 |
| 11 | 过度拟合（试了 N 组参数后取最优） | 虚假显著 | `pitfalls.py` 第 5 项按 N 校正；只接受宽稳定区 | 已缓解 |
| 12 | 程序化交易合规 | 监管风险 | 默认不自动下单；保留完整策略与订单记录 | 已缓解 |
| 13 | akquant 版本演进导致对账不可用 | 对账引擎失效 | 对账是**可选增强**，不是主链路；锁定版本 | 已缓解 |

---

## 九、实施前仍待确认的两个问题

| # | 问题 | 需要你决定 |
|---|---|---|
| 1 | **样本内/外切分点** | 配置中暂定 `in_sample_end = 2021-12-31`（样本外 2022–2026，约 4.7 年，含 2022 油运上行周期）。是否接受？ |
| 2 | **初始资金规模** | 配置中暂定 20 万元。资金规模影响最小佣金侵蚀与 100 股取整误差——**小资金（<5 万）的成本率会显著恶化**，需要按实际资金重算。 |

**这两个问题不阻塞 P0–P1**（规则层与资金规模无关），但**阻塞 P4 的 B&H 校验**，需在 P4 前确认。

---

## 十、本方案的三条不可让渡原则

1. **P1 规则层完成前，禁止写任何因子。** 在不确定的地基上盖楼是前六轮的失败模式。
2. **P4 的 B&H 校验未通过前，禁止进入 P5。** 回测不可信时，所有策略结论都无意义。
3. **`strategy/` 永远不得 import `evaluation/`。** 归因 ≠ 预测，这是架构级隔离，不是风格偏好。

**最终判定标准**：系统成功与否不看收益率，而看**证伪效率**——一个新想法能否在 1 天内被严格地证明为不可用。

---
---

# 十一、二轮评审修正（v2）

> 评审提出 5 个问题，全部成立。以下逐条给出**实证结论**与**修正方案**。
> 其中第 1 条已完成源码级核验，**结论与原方案相反**。

## 11.1 【修正 · 高】akquant 对账的可行性：**已核验，前提不成立**

### 核验过程与证据

| 检索 | 结果 |
|---|---|
| 全仓库搜索 `can_buy` | **4 处命中，全部在 `docs/zh/textbook/` 下**；`src/` 与 `python/` **零命中** |
| `src/execution/stock.rs` 搜索 `extra` | **6 处命中，全部是 `#[cfg(test)]` 模块里构造 Bar 时的 `extra: Default::default()`**；撮合逻辑**从不读取** `extra` |
| `src/execution/stock.rs` 搜索 `limit_up / limit_down / can_buy / suspended / tradable` | **零命中**（唯一命中是测试函数名 `test_buy_stop_trail_limit_updates...` 中的 `limit_up` 子串） |
| `src/execution/validation.rs` 全文 | 只有 `is_multiple` / `validate_tick_size` / `reject_order` / `render_reject_warning`。**无任何涨跌停、停牌校验** |

**`StockMatcher.match_order` 的完整逻辑**（源码实读）：

```rust
fn match_order(&self, order, ctx) -> Option<Event> {
    if self.enforce_tick_size {
        validate_tick_size(order, tick_size)?   // 仅校验价格对齐 tick
    }
    CommonMatcher::match_order(order, ctx, check_lot_size = true)   // 手数 + 通用撮合
}
```

→ **结论：akquant 的默认撮合内核不读取 `Bar.extra` 的 `can_buy`/`can_sell`，也不做任何涨跌停/停牌判定。** 文档原文的正确读法是"用这些字段**辅助策略判断**，或在**自定义撮合/执行扩展**中消费"——即：**归策略层或自定义扩展，不归默认撮合器**。

### 对账的"语义一致子集"（核验后确定）

| 规则 | 自建引擎 | akquant 默认撮合 | 是否可对账 |
|---|---|---|---|
| 佣金 / 印花税 / 过户费 | ✅ | ✅（内置成本模型） | ✅ **可对账** |
| 滑点 | ✅ | ✅（`slippage` 策略） | ✅ 可对账 |
| 撮合价格基准 | `next_open` | `PriceBasis` 可配 | ✅ 需对齐配置后对账 |
| `tick_size` 对齐 | ✅ | ✅（`validate_tick_size`） | ✅ 可对账 |
| 手数（100 股） | ✅ | ✅（`check_lot_size`） | ✅ 可对账 |
| 成交量约束 | ✅ | ✅（`volume_limit_pct`） | ✅ 可对账 |
| 资金 / 持仓校验 | ✅ | ✅（`CommonMatcher`） | ✅ 可对账 |
| **T+1** | ✅ | ✅（`StockInstrument.sellable_after_days: 1`） | ✅ 可对账 |
| **涨跌停** | ✅ | ❌ **无** | ❌ **不可对账** |
| **停牌** | ✅ | ❌ **无** | ❌ **不可对账** |
| **一字板** | ✅ | ❌ **无** | ❌ **不可对账** |

### 修正后的对账方案（采纳评审建议的"喂已裁剪订单"）

```
自建引擎（权威）
   ├─ TradabilityGate 过滤（涨跌停/停牌/一字板）   ← 只在此处裁决
   ├─ 输出的"可通过订单" ──┬──► 自建 MatchingEngine
   │                       └──► akquant（同一批订单，禁止再过滤）
   └─ 比较：成交价 / 成交数量 / 各项费用 / 期末净值
```

**三条硬规则**：

1. **喂给 akquant 的订单必须是自建引擎已通过 `TradabilityGate` 的订单**，akquant 侧**不再做涨跌停裁决**
2. **对账范围明确限定为上表中标 ✅ 的 8 项**；涨跌停/停牌/一字板**不在对账范围**（以自建引擎为准）
3. akquant 侧必须**显式配置** `slippage` 与 `PriceBasis` 对齐自建引擎的 `next_open` 语义，否则价格基准不一致会产生假差异

### P4-0 · 对账探针（新增，**必须在 P4 正式开始前完成**）

> ✅ **已完成**（2026-09-17）。实测结果见 `probe_report.md`，**修正了本节的 3 处源码推断**。

| 项 | 内容 |
|---|---|
| 目的 | 用实证代替推断：确认两引擎在**构造的边界场景**下的实际行为 |
| 场景 A | 涨停 Bar（`preclose=10, limit_up=11, open=high=low=11`）+ 买单 100 股 → 自建应拒绝；记录 akquant 的实际订单状态 |
| 场景 B | 停牌 Bar（`tradestatus=0` / `volume=0`）+ 买单 → 同上 |
| 场景 C | 正常 Bar + 大额订单（超 `volume × 10%`）→ 两边都应部分成交，比较成交数量 |
| 场景 D | T 日买入 + T 日卖出 → 两边都应拒绝（验证 `sellable_after_days: 1` 是否真生效） |
| 场景 E | 同一笔正常订单 → 比较成交价与五项费用是否逐项相等 |
| 产出 | `probe_report.md`：逐场景记录**两引擎的实际行为**（非预期行为） |
| 规模 | S（< 200 行） |

#### 实测结论（**修正 §11.1 的 3 处源码推断**）

| # | 源码推断 | **实测结果** | 修正 |
|---|---|---|---|
| 1 | akquant 不做**停牌**判定 | ❌ **推断有误**。akquant **会拒绝**，拒因 `not tradable (zero volume, suspension)` —— 它用 **`volume == 0`** 判定停牌 | 停牌**可对账**，但**判据不同**（自建 `tradestatus`，akquant `volume`） |
| 2 | akquant 不做**涨跌停**判定 | ✅ **确认**。涨停一字板 `status='filled'`, `avg_price=11.0055` —— **照常成交** | 涨跌停**不可对账**（原结论成立） |
| 3 | 流动性约束两边一致 | ❌ **语义不同**。自建**当日裁剪 + 撤销剩余**；akquant **跨日部分成交 + 顺延**（5000 股全部成交，`duration=2 days`） | 流动性约束**不可直接对账** |
| 4 | （新增）费用字段口径 | akquant 把**过户费并入 `commission`**：自建 `2.001 + 0.2001 = 2.2011` = akquant `2.2011` | 费用**可对账**，但须注意字段口径 |
| 5 | （新增）`t_plus_one` | 函数签名 `t_plus_one: bool = False` —— **默认关闭** | 对账时必须显式传 `True` |

**场景 E 是完美对账**：成交价 `20.01 = 20.01`，佣金+过户费 `2.2011 = 2.2011`。

#### 修正后的对账范围

| 可对账（7 项） | 不可对账（3 项） |
|---|---|
| 成本（注意过户费口径）/ 滑点 / 价格基准 / tick 对齐 / 手数 / 资金校验 / T+1（须显式开启） | **涨跌停**、**一字板**、**流动性约束** |

**方案不变**：自建引擎先跑 `TradabilityGate` 与流动性裁剪，**只把已裁剪订单喂给 akquant**，
akquant 侧 `volume_limit_pct` 设为不生效或对齐同口径。

**遗留项**：场景 D 构造不出有效的 T+1 反例（2 根 bar 且 akquant 次日执行），
需补「单根 bar 内既买又卖」或直接查 `available_position` 的场景。

> **探针的产出直接决定对账方案是否成立。** 若场景 D/E 也出现不一致，则对账降级为"**仅成本模型对账**"，甚至取消对账、改为"双引擎独立复现"（不比较，只看结论是否同向）。

---

## 11.2 【修正 · 高】统计功效：方向对，但理由需要更正

### 评审观点的准确部分与不准确部分

**✅ 正确**：跨参数、跨窗口、跨市场状态之间**未必独立**，相关性高时增加这些维度不能有效提高统计功效。**建议用截面 IC 是对的。**

**❌ 需要更正**：「截面 IC 的样本量是 标的数 × 时间，比单标的时序 IC 大一个量级」——**这个说法不成立**。

截面 IC 的做法是：每个交易日 `t` 在 `N` 只标的上计算一次秩相关，得到**长度为 T 的 IC 序列**。统计量是

```
IC_IR = mean(IC) / std(IC) × sqrt(T)
```

**样本量是 T（期数），不是 N × T。** N 的作用不是增加样本量，而是**降低每一期 IC 的抽样噪声**（零假设下 `SE(IC) ≈ 1/sqrt(N-1)`）。

### 那截面 IC 为什么确实更强？真正的机制是信噪比

| 机制 | 时序 IC（单标的） | 截面 IC |
|---|---|---|
| 市场 beta | **完全留在**收益里，淹没因子信号 | **被自然消去**（同期所有标的共享同一市场收益，秩相关不受常数项影响） |
| 行业 beta | 留在收益里 | **航运板块截面内无法消去**（整个截面就是同一行业） |
| 信噪比 | 极低 | 高（纯 alpha 对比） |
| 单期噪声 | — | `SE ≈ 1/sqrt(N-1)` |

**数值含义（T = 2400 交易日）**：

| N | 单期 IC 抽样噪声 | 达到 t=2 所需的 mean(IC) |
|---|---|---|
| 4 | 0.577 | 0.024 |
| 10 | 0.333 | 0.014 |
| 20 | 0.229 | 0.009 |
| 30 | 0.186 | 0.008 |

→ **关键推论：凑 3–5 只标的收益有限（N=4 的噪声 0.577 比 N=10 的 0.333 差一倍）。N 必须 ≥ 10 才真正降低噪声。**

### 修正后的阶段一方案：**单标的交易 + 板块截面研究（双轨）**

| 轨道 | 内容 | 说明 |
|---|---|---|
| **交易轨** | 仍**只做 601872**，执行范围不放宽 | 不改变阶段一的交付目标 |
| **研究轨** | 因子评估改用**航运板块截面 IC**（N ≥ 10） | 只为提高因子评估的统计功效，不产生交易 |

**航运板块候选池（N ≈ 12，可扩展）**：

| 代码 | 名称 | 细分 |
|---|---|---|
| 601872 | 招商轮船 | 原油 + 干散（**交易标的**） |
| 600026 | 中远海能 | 原油 + LNG |
| 601975 | 招商南油 | 成品油 |
| 600428 | 中远海特 | 特种船 |
| 600798 | 宁波海运 | 干散 |
| 601919 | 中远海控 | 集运 |
| 603565 | 中谷物流 | 内贸集运 |
| 601083 | 锦江航运 | 集运 |
| 603209 | 兴通股份 | 液体化学品 |
| 001205 | 盛航股份 | 液体化学品 |
| 000520 | 长航凤凰 | 干散 |
| 603162 | 海通发展 | 干散 |

**架构影响：零。** `Portfolio.books` 已是 `dict[str, PositionBook]`；`DataProvider.get_bars(symbol, ...)` 已带 `symbol`。研究轨只是"多读几个 symbol 的数据、算截面 IC"，不进入交易链路。

**报告口径**：主指标 **IC_IR**；辅助 时序 IC。**只报时序 IC 视为不合格。**

> **注意板块截面的固有局限**：航运板块截面**无法剔除行业 beta**（整个截面就是同一行业）。要剔除行业 beta 需用全市场截面，但那与阶段一的单标的交易目标偏离。阶段一只做行业截面，全市场截面列为 P7 可选。

---

## 11.3 【修正 · 中】样本内/外切分：改为"留出集 + Walk-Forward"双轨

**评审成立**：2022 年是油运上行周期（俄乌冲突后运价飙升），若样本外一开始就是大牛市，无法区分"策略有效"与"赶上了好行情"。

**更深一层的问题**：**单次 in/out 切分本身就只是一个样本**，统计功效极低——这与 §11.2 是同一个病。

### 修正方案

| 用途 | 方案 |
|---|---|
| **最终留出集**（只允许看一次） | `2022-01-01 ~ 2026-09-01`，保留原切分 |
| **主要证据来源** | **Walk-Forward 滚动段**（多个样本外段 → 多个独立观测，而非一个） |
| **留出集内部分段报告** | ① `2022-01 ~ 2023-12` 上行期 ② `2024-01 ~ 2026-09` 回落/震荡期 |
| **保守口径** | 额外报告"**剔除 2022 年**"的样本外结果 |
| **市场状态分层** | 按招商轮船自身 60 日动量正/负分层，分别报告 |

**新增验收项**：报告必须同时给出 ① 全样本外 ② 上行段 ③ 回落段 ④ 剔除 2022 —— **四组数字**。只报一组视为不合格。

**样本内区间** `2016-01-01 ~ 2021-12-31` 保留（含 2016 低点、2018 贸易战、2020 疫情油价负值、2021 集运牛市，覆盖多种状态）。

---

## 11.4 【修正 · 中】资金规模与最低佣金：临界点是"单笔 2 万元"

**评审的担忧方向正确，但临界点算错了位置。**

最低佣金临界点与**资金总量无关**，只与**单笔成交金额**有关：

```
临界金额 = min_commission / commission_rate = 5.0 / 0.00025 = 20,000 元
```

→ **任何单笔成交金额 < 2 万元，实际佣金率就高于 0.025%。**

### 用真实价格重算（评审假设股价 6 元，实际不是）

招商轮船近期约 **19.86 元**（2026-09-16 收盘）。100 股 ≈ 1,986 元。

| 单笔手数 | 成交金额 | 佣金 | 实际佣金率 | 往返成本率 |
|---|---|---|---|---|
| 1 手 | 1,986 元 | 5.0（触底） | **0.252%** | **≈ 0.55%** |
| 5 手 | 9,930 元 | 5.0（触底） | 0.050% | ≈ 0.30% |
| 10 手 | 19,860 元 | 5.0（临界） | 0.025% | ≈ 0.25% |
| 100 手 | 198,600 元 | 49.7 | 0.025% | ≈ 0.21% |

→ **20 万资金下单标的是安全的**：满仓约 100 手，单笔 19.9 万，佣金率正常。
→ **真正的风险是"主动拆单"和"小仓位微调"**：若拆成 5 笔各 4 万元仍 > 2 万（安全）；若拆到 < 1 万元就有问题。

### 设计修正（采纳评审建议）

| # | 修正 | 落点 |
|---|---|---|
| 1 | **`PositionSizer` 输出单一订单，禁止主动拆单** | `strategy/sizing.py` |
| 2 | 差额取整后 < 100 股则**不下单**（避免微调被成本吃掉） | `strategy/sizing.py` |
| 3 | **`RiskGate` 新增警告规则**：单笔金额 < 2 万元 → 记录 `MIN_COMMISSION_WARNING`（允许通过，但落库并在报告中标红） | `execution/risk.py` |
| 4 | 资金不足时按可用资金缩减数量（`shrink`），缩减后为 0 则不下单 | `core/matching.py` |

**`RiskGate` 规则数从 8 条增至 9 条。**

**已知简化登记**：招商轮船股价若未来大幅上涨（如 40 元），100 手 = 40 万元 > 20 万资金 → 满仓只能约 50 手，取整误差 ≤ 1 手 = 4,000 元 ≈ 2% 仓位。可接受，但需在报告中记录实际取整误差。

---

## 11.5 【修正 · 中】纸面交易的执行语义：硬性对账改为三项拆分

**评审成立**：若纸面交易用实时行情成交，与回测的 `T+1 开盘价` 必然有偏差，**"逐笔对账一致"不可能达成**；若用 `T+1 开盘价`，则只是回测的重复，验证不了执行环节。

### 修正后的纸面交易设计

| 环节 | 做法 |
|---|---|
| **信号生成** | 用**实时/当日数据**（`DataReadinessCheck` 通过后） |
| **成交模拟** | 用 **`T+1 开盘价`** + 自建 `MatchingEngine`（与回测同一套代码） |
| **人工执行** | 人工按信号下单，**记录实际成交价、成交时间、实际股数** |

### 验收门槛改为三项（原来的一项拆成三项）

| # | 对账项 | 门槛 | 性质 |
|---|---|---|---|
| 1 | **信号一致性** | 纸面交易信号 vs 回测同日信号 → **一致率 100%** | **硬性** |
| 2 | **模拟成交一致性** | 按 `T+1 开盘价` 模拟的成交 vs 回测成交 → **一致率 100%** | **硬性** |
| 3 | **执行偏差分析** | 人工实际成交价 vs `T+1 开盘价` → 输出滑点分布、延迟分布 | **不设门槛**，作为实盘偏差分析输入 |

**原"逐笔对账一致"的表述作废**，改为上表三项。

**新增产出**：`execution_bias_report.md` —— 人工执行的滑点分布、下单延迟分布、实际股数与目标股数的偏差分布。这份报告是**未来判断"实盘能否复现回测"的唯一依据**。

---

## 11.6 修正汇总：文档受影响的位置

| 章节 | 修正内容 | 类型 |
|---|---|---|
| §4.4.3 对账引擎 | 重写：对账范围收窄至 8 项；必须喂已裁剪订单 | **替换** |
| §6 P4 | 新增 **P4-0 对账探针**（前置，规模 S） | **新增** |
| §6 P5 | 因子评估改用**航运板块截面 IC（N≥10）**；新增研究轨 | **修改** |
| §4.5 `strategy/` | 新增 `research/` 研究轨（不进入交易链路） | **新增** |
| §4.5 `PositionSizer` | 禁止主动拆单；差额 < 100 股不下单 | **修改** |
| §4.6 `RiskGate` | 规则 8 → 9 条，新增 `MIN_COMMISSION_WARNING` | **修改** |
| §5 配置 | `backtest` 新增 `out_of_sample_segments` 分段定义 | **新增** |
| §6 P8 | 验收从 1 项拆为 3 项；新增 `execution_bias_report.md` | **替换** |
| §7 验收总表 | 新增 6 项（探针、四组样本外数字、信号一致率、模拟成交一致率、截面 IC_IR、拆单禁令） | **新增** |
| §8 风险登记册 | 新增 3 条风险（见下） | **新增** |
| §11.4 | 20 万资金结论：**安全**，但需防拆单 | 结论更新 |

### 风险登记册新增条目

| # | 风险 | 影响 | 缓解 | 状态 |
|---|---|---|---|---|
| 14 | **akquant 不实现涨跌停**，若误将其作为"独立验证"会产生假差异 | 对账结论错误 | 对账范围限定 8 项；喂已裁剪订单；P4-0 探针先行 | **已核验并缓解** |
| 15 | 航运板块截面**无法剔除行业 beta** | 截面 IC 仍含行业共同因子 | 明确口径为"行业内选股能力"；全市场截面列 P7 可选 | 已接受 |
| 16 | 主动拆单导致最低佣金侵蚀（单笔 < 2 万元） | 成本率从 0.21% 恶化至 0.55% | `PositionSizer` 禁止拆单；`RiskGate` 警告规则 | 已缓解 |

### 验收总表新增 6 项

| # | 门槛 | 归属 |
|---|---|---|
| 20 | P4-0 对账探针完成，5 个场景的实际行为已记录 | P4-0 |
| 21 | 样本外报告含**四组数字**（全样本外/上行段/回落段/剔除 2022） | P7 |
| 22 | 因子报告给出 **IC_IR**（N≥10 航运截面），非仅时序 IC | P5 |
| 23 | 纸面交易**信号一致率 100%** | P8 |
| 24 | 纸面交易**模拟成交一致率 100%** | P8 |
| 25 | 全流程无主动拆单；单笔金额 < 2 万元时有警告记录 | P6 |

---
---

# 十二、第三轮修正：用户真实费率与一字板规则

> 两项前提性修正，影响成本模型与撮合规则。**P1 代码已按此实现。**

## 12.1 用户真实费率：万五 + 免五

**用户明确前提（2026-09-17）**：佣金**万分之五**，且**免五**（无最低 5 元）。

| 项 | 原假设 | **修正后** |
|---|---|---|
| `commission_rate` | 0.00025（万 2.5） | **0.0005（万 5）** |
| `min_commission` | 5.0 | **0.0（免五）** |
| 往返成本率（不含滑点） | 0.102% | **0.152%** |
| 往返成本率（含滑点 0.1%） | 0.202% | **0.252%** |
| 单笔 2 万元临界点 | 存在 | **不存在** |

**连锁影响（已在 P1 实现中落地）**：

| 受影响项 | 变化 |
|---|---|
| §11.4 的"单笔 2 万元临界点"分析 | **作废**——免五后成本率与单笔金额无关，大小单同率 |
| `RiskGate` 的 `MIN_COMMISSION_WARNING` 规则 | **删除**（前提消失，约束随之消失）。规则数回到 8 条 |
| "禁止主动拆单"约束 | **保留**——理由从"避免最低佣金"变为"减少不必要的交易次数" |
| 换手率约束的重要性 | **上升**——单边成本从 0.051% 升至 0.076%，高换手策略被侵蚀更快 |

## 12.2 一字板规则修正（原规则错误）

**原写法**：「若 `high == low` → 全天不可成交（无论买卖）」。

**为什么错**：一字板涨停时**买盘排长队**，卖单实际上秒成；一字板跌停时**卖盘排长队**，买单可成交。原规则把两个方向都拒了，会系统性低估策略的卖出能力。

**修正后的规则**（`rules/tradability.py` 已实现，7 个用例覆盖）：

```
1. 停牌                                → 拒绝
2. 涨停(open >= limit_up) 且 买入       → 拒绝（涨停不可买）
   跌停(open <= limit_down) 且 卖出     → 拒绝（跌停不可卖）
3. 一字板 且 未处于涨跌停价位           → 拒绝（真正的双向流动性枯竭）
4. 买入数量非 lot 整数倍                → 拒绝
5. 数量 > 成交量 × volume_limit_pct     → 裁剪
```

对应测试：`test_sell_allowed_at_limit_up`、`test_buy_allowed_at_limit_down`、`test_one_word_board_at_limit_up_allows_sell`、`test_one_word_board_at_limit_down_allows_buy`、`test_one_word_board_not_at_limit_rejects_both_sides`。

## 12.3 P1 实现阶段发现的两处缺陷（补做"读 rqalpha 源码"后修正）

> 评审指出"② 移植改造类必须读源码"。补做后发现**我此前的实现基于报告的转述而非原文**，其中两处是错的。

| # | 缺陷 | 修正 |
|---|---|---|
| 1 | `position_pnl` 写成"昨仓相对**成本价**" | rqalpha 原文是相对**昨收**（`prev_close`）：`_logical_old_quantity * (last_price - prev_close)`。已拆成 `unrealized_pnl()`（累积口径）+ `daily_pnl_breakdown()`（当日口径） |
| 2 | 送转逐批次 `round()` 误差累积 | 增加 `expected_quantity` 参数，差额补到末批（同 rqalpha `handle_split`） |

**核验后确认无需改动的三处**：

| # | 分析报告原文 | 实际情况 |
|---|---|---|
| 3 | 称 rqalpha `today_closable` 为"当日可卖数量（= 总量 − 昨仓）" | **误译**。原文 `today_closable = _quantity − _old_quantity − 挂平今单`，是"可平**今**仓"（期货概念）。A股应取"昨仓可卖"，本项目实现正确 |
| 4 | 称 rqalpha `PositionQueue` 支持部分平仓 | 属实，但其 `handle_trade` 还有"队列耗尽则反手开仓"分支——A股 T+1 下超量卖出必须抛异常，本项目更严格且正确 |
| 5 | 未提及 rqalpha 的 `prev_close` 复权口径 | rqalpha 用**前复权**昨收（算收益），涨跌停判定须用**不复权**——不矛盾，印证"三类价格分离"的必要性 |

## 12.4 验收总表修正

| # | 原门槛 | 修正 |
|---|---|---|
| 25 | 全流程无主动拆单；单笔金额 < 2 万元时有警告记录 | 改为：**全流程无主动拆单**（警告规则随免五前提删除） |






