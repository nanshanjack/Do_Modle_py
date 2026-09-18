# 数据指纹清单

> 生成时间：2026-09-18 11:52:30
> 文件：`market.sqlite`
> 大小：11,403,264 字节（10.88 MB）
> **SHA256**：`6dab9986531e2ba35db353a4fa6d17178df90e252aead6aed379db5cdd3cbf67`

**本文件已提交到 git；`market.sqlite` 未提交**（见 `.gitignore`）。
重跑同步后可用 `scripts/data_manifest.py --verify` 校验是否一致。

## 重新生成数据

```powershell
.\py.cmd scripts\sync_data.py --universe shipping --start 2016-01-01 --end 2026-09-01
.\py.cmd scripts\sync_financial.py
.\py.cmd scripts\sync_shipping.py
```

## 表统计

| 表 | 行数 | 起始 | 结束 | 标的数 |
|---|---:|---|---|---:|
| `calendar` | 2,591 | 2016-01-04 | 2026-09-01 | — |
| `daily_bar_hfq` | 22,703 | 2016-01-04 | 2026-09-01 | 12 |
| `daily_bar_none` | 22,703 | 2016-01-04 | 2026-09-01 | 12 |
| `dividend` | 83 | 2016-05-27 | 2026-07-30 | 11 |
| `fin_balance` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `fin_cashflow` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `fin_dupont` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `fin_growth` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `fin_operation` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `fin_profit` | 397 | 2016-03-31 | 2026-06-30 | 12 |
| `forecast` | 194 | 2016-01-23 | 2026-07-15 | 12 |
| `instrument` | 12 | 1993-10-25 | 2023-12-05 | 12 |
| `shipping_index` | 40,340 | 1988-10-19 | 2026-09-17 | — |

## 数据源

| 表 | 来源 |
|---|---|
| `daily_bar_none` / `daily_bar_hfq` / `calendar` / `instrument` / `dividend` / `forecast` | baostock |
| `fin_*`（6 张） | baostock 财务接口（`query_profit_data` 等） |
| `shipping_index` | 东方财富公开 API（`datacenter-web.eastmoney.com`） |

## PIT 处理

| 数据 | 可见时点 |
|---|---|
| 日线 Bar | 当日 15:00 |
| 分红送转 | 除权日 09:00 |
| **业绩预告/快报** | **公告日 + 1 天 09:00** |
| **财务季报** | **公告日 + 1 天**（无公告日时退回「报告期 + 法定滞后」） |
| **航运运价指数** | **数据日期 + 2 天**（东财转载有延迟） |
