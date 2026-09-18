# P4-0 · akquant 对账探针报告

> 生成时间：2026-09-17 23:50:42
> akquant 版本：0.3.61
> 成本参数：佣金 0.0100%（免五）、印花税 0.0500%、过户费 0.00100%、滑点 0.0500%

**探针目的**：源码已确认 akquant 默认撮合器不读 `Bar.extra`、不做涨跌停/停牌判定。本报告用**实证**确认，并据此确定对账范围。

## 场景 A · 涨停买单（preclose=10.0, limit_up=11.0）

- 自建引擎：成交 0 笔，拒单 [{'reason': 'LIMIT_UP'}]
- akquant ：成交 0 笔；订单 1 条：{'id': '256be36d-b656-41eb-a1b9-70f305386224', 'symbol': 'sh.601872', 'side': 'buy', 'order_type': 'market', 'quantity': 100.0, 'filled_quantity': 100.0, 'limit_price': nan, 'stop_price': nan, 'avg_price': 11.0055, 'commission': 0.1210605, 'status': 'filled', 'time_in_force': 'gtc', 'created_at': Timestamp('2026-09-14 00:00:00+0800', tz='Asia/Shanghai'), 'updated_at': Timestamp('2026-09-15 00:00:00+0800', tz='Asia/Shanghai'), 'tag': '', 'reject_reason': '', 'position_effect': 'open', 'reduce_only': False, 'created_at_iso': '2026-09-13T16:00:00Z', 'updated_at_iso': '2026-09-14T16:00:00Z', 'owner_strategy_id': '_default', 'filled_value': 1100.55, 'duration': Timedelta('1 days 00:00:00')}；拒单原因={'reject_reason': {}, 'count': {}, 'ratio': {}}

**结论（实测）**：自建拒绝（`LIMIT_UP`），**akquant 成交了**（`status='filled'`, `avg_price=11.0055`）。

→ **实证确认**：akquant 默认撮合器**不做涨跌停判定**，涨停一字板照样成交。与源码分析一致。

→ **对账影响**：涨跌停**不可对账**。必须由自建引擎先过滤，只把通过的订单喂给 akquant。

## 场景 B · 停牌买单（volume=0）

- 自建引擎：成交 0 笔，拒单 [{'reason': 'SUSPENDED'}]
- akquant ：成交 0 笔；订单 1 条：{'id': '76f3881a-d385-4544-9a9b-c9a7375ca0f1', 'symbol': 'sh.601872', 'side': 'buy', 'order_type': 'market', 'quantity': 100.0, 'filled_quantity': 0.0, 'limit_price': nan, 'stop_price': nan, 'avg_price': nan, 'commission': 0.0, 'status': 'rejected', 'time_in_force': 'gtc', 'created_at': Timestamp('2026-09-14 00:00:00+0800', tz='Asia/Shanghai'), 'updated_at': Timestamp('2026-09-15 00:00:00+0800', tz='Asia/Shanghai'), 'tag': '', 'reject_reason': 'Symbol sh.601872 not tradable (zero volume, suspension) at execution timestamp 1789401600000000000', 'position_effect': 'open', 'reduce_only': False, 'created_at_iso': '2026-09-13T16:00:00Z', 'updated_at_iso': '2026-09-14T16:00:00Z', 'owner_strategy_id': '_default', 'filled_value': 0.0, 'duration': Timedelta('1 days 00:00:00')}；拒单原因={'reject_reason': {0: 'Symbol sh.601872 not tradable (zero volume, suspension) at execution timestamp 1789401600000000000'}, 'count': {0: 1}, 'ratio': {0: 1.0}}

**结论（实测）—— ⚠️ 修正了此前的源码推断**：akquant **也拒绝了**，拒因 `not tradable (zero volume, suspension)`。

→ 我此前从源码推断『akquant 不做停牌判定』是**不准确的**。它确实没有读 `Bar.extra`，但**用 `volume == 0` 判定停牌**。

→ **对账影响**：停牌**可以对账**（两边都会拒），但**判据不同**：
  自建用 `tradestatus==0`（数据源字段），akquant 用 `volume==0`。
  若数据源在停牌日仍给出非零 volume，两边会分歧。

## 场景 C · 超额订单（买 5000 股，volume=10000，10% 上限 → 1000 股）

- 自建引擎：[{'quantity': 1000, 'price': 10.01, 'commission': 1.0010000000000001, 'stamp_tax': 0.0, 'transfer_fee': 0.10010000000000001}]
- akquant ：成交 0 笔；订单 1 条：{'id': '28d4521a-def3-4914-8882-a2bdbc200fec', 'symbol': 'sh.601872', 'side': 'buy', 'order_type': 'market', 'quantity': 5000.0, 'filled_quantity': 5000.0, 'limit_price': nan, 'stop_price': nan, 'avg_price': 10.08504, 'commission': 5.546772, 'status': 'filled', 'time_in_force': 'gtc', 'created_at': Timestamp('2026-09-14 00:00:00+0800', tz='Asia/Shanghai'), 'updated_at': Timestamp('2026-09-16 00:00:00+0800', tz='Asia/Shanghai'), 'tag': '', 'reject_reason': '', 'position_effect': 'open', 'reduce_only': False, 'created_at_iso': '2026-09-13T16:00:00Z', 'updated_at_iso': '2026-09-15T16:00:00Z', 'owner_strategy_id': '_default', 'filled_value': 50425.2, 'duration': Timedelta('2 days 00:00:00')}；拒单原因={'reject_reason': {}, 'count': {}, 'ratio': {}}

**结论（实测）—— ⚠️ 两边语义不同**：自建裁剪到 **1000 股**并撤销剩余；akquant **5000 股全部成交**（`filled_quantity=5000`），但 `updated_at` 落到第 3 天、`duration=2 days` —— 它是**跨多日部分成交 + 顺延**，不是当日裁剪。

→ **对账影响**：流动性约束**语义不一致**，不可直接对账。自建侧应把『已按流动性裁剪后的订单』喂给 akquant，并让 akquant 的 `volume_limit_pct` 不再二次生效（或对齐为同口径）。

## 场景 D · T+1 是否生效（观测 `available_position`）

- 自建引擎：总持仓 100，**可卖量 0**，当日卖出 → 拒单 ['T1_VIOLATION']

- akquant (t_plus_one=True)：
    2026-09-13  position=0.0  available_position=0.0
    2026-09-14  position=100.0  available_position=0.0
    2026-09-15  position=0.0  available_position=0.0
    → T+1 证据（持仓>0 但可用=0 的 bar）: ✅ 有 1 个
- akquant (t_plus_one=False)：
    2026-09-13  position=0.0  available_position=0.0
    2026-09-14  position=100.0  available_position=100.0
    2026-09-15  position=0.0  available_position=0.0
    → T+1 证据（持仓>0 但可用=0 的 bar）: ❌ 无

**结论（实测）**：对比 `t_plus_one=True` 与 `False` 两组的 `available_position` —— 若 True 组出现「持仓 > 0 但可用 = 0」，说明 **akquant 的 T+1 真生效**；若两组完全一致，则 `t_plus_one` 未起作用。

→ **对账要求（确定的事实）**：函数签名 `t_plus_one: bool = False` —— **默认关闭**，对账时必须显式传 `True`。

## 场景 E · 正常订单（买 1000 股 @ 开盘 20.0）

- 自建引擎：[{'quantity': 1000, 'price': 20.01, 'commission': 2.001, 'stamp_tax': 0.0, 'transfer_fee': 0.20010000000000003}]
- akquant ：成交 0 笔；订单 1 条：{'id': '49332133-2f55-418f-8c69-7bb25bb75911', 'symbol': 'sh.601872', 'side': 'buy', 'order_type': 'market', 'quantity': 1000.0, 'filled_quantity': 1000.0, 'limit_price': nan, 'stop_price': nan, 'avg_price': 20.01, 'commission': 2.2011, 'status': 'filled', 'time_in_force': 'gtc', 'created_at': Timestamp('2026-09-14 00:00:00+0800', tz='Asia/Shanghai'), 'updated_at': Timestamp('2026-09-15 00:00:00+0800', tz='Asia/Shanghai'), 'tag': '', 'reject_reason': '', 'position_effect': 'open', 'reduce_only': False, 'created_at_iso': '2026-09-13T16:00:00Z', 'updated_at_iso': '2026-09-14T16:00:00Z', 'owner_strategy_id': '_default', 'filled_value': 20010.0, 'duration': Timedelta('1 days 00:00:00')}；拒单原因={'reject_reason': {}, 'count': {}, 'ratio': {}}

**结论（实测）—— ✅ 成本模型完全对账**：

| 项 | 自建引擎 | akquant | 判定 |
|---|---|---|---|
| 成交价 | 20.01 | 20.01 | ✅ 一致（含 1 tick 滑点） |
| 佣金（自建） | 2.001 | — | — |
| 过户费（自建） | 0.20010000000000003 | — | — |
| 佣金+过户费 | 2.201100 | 2.2011 | ✅ 一致 |

→ akquant 把**过户费并入 `commission` 字段**：2.001 + 0.20010000000000003 = 2.201100 = 2.2011

→ **对账影响**：成交价与费用总额**可对账**。注意字段口径差异——akquant 的 `commission` 含过户费。

## 对账范围结论（实测修正版）

⚠️ **本表已按实测行为修正**，与 `A股量化交易系统_实施方案设计.md` §11.1 的
源码推断版有 3 处不同。

| 规则 | 自建引擎 | akquant 实测 | 可对账？ |
|---|---|---|---|
| 佣金 / 印花税 / 过户费 | ✅ | ✅（过户费并入 `commission`） | ✅ **可对账** |
| 滑点 | ✅ 至少 1 tick | ✅ 百分比策略可配 | ✅ 可对账（需对齐参数） |
| 撮合价格基准 | 次日开盘 | 次日开盘（`t_plus_one=True`） | ✅ 可对账 |
| tick 对齐 | ✅ | ✅ `validate_tick_size` | ✅ 可对账 |
| 手数（100 股） | ✅ | ✅ `check_lot_size` | ✅ 可对账 |
| 资金校验 | ✅ | ✅ | ✅ 可对账 |
| **T+1** | ✅ 强制 | ⚠️ **`t_plus_one` 默认 False** | ✅ 可对账（须显式开启） |
| **停牌** | ✅ `tradestatus==0` | ✅ **`volume==0`** | ⚠️ 判据不同 |
| **涨跌停** | ✅ 拒绝 | ❌ **不做判定，照常成交** | ❌ **不可对账** |
| **一字板** | ✅ 拒绝 | ❌ 不做判定 | ❌ **不可对账** |
| **流动性约束** | 当日裁剪 + 撤销剩余 | **跨日部分成交 + 顺延** | ❌ **语义不同** |

### 结论

1. **可对账子集 7 项**：成本 / 滑点 / 价格基准 / tick 对齐 / 手数 / 资金 / T+1
2. **不可对账 3 项**：涨跌停、一字板、流动性约束 → **必须喂已裁剪订单**
3. **停牌判据不同**：自建 `tradestatus`，akquant `volume==0`
   → 若数据源停牌日给出非零 volume，两边会分歧
4. **akquant 的 `t_plus_one` 默认 False** —— 对账时必须显式传 `True`，
   否则 T+1 不生效，会产生大量假差异
