"""航运运价指数抓取 —— 东方财富公开 API（**无需 akshare**）

## 背景：设计文档 §2.3 的错误已纠正

§2.3 曾断言「**硬限制：akshare 没有 BDTI（原油运输指数）**」，并据此把运价数据排除出阶段一。
**该结论是错误的。** 实际核查 akshare 源码 `akshare/economic/macro_china.py`：

```python
def macro_china_bdti_index() -> pd.DataFrame:
    \"\"\"原油运输指数  https://data.eastmoney.com/cjsj/hyzs_list_EMI00107668.html\"\"\"
```

**实测可得区间**（2026-09-18 验证）：

| 指数 | 最旧数据 | 覆盖 |
|---|---|---|
| **BDTI 原油运输** | **2001-12-27** | **25 年** |
| BDI 波罗的海干散货 | 1988-10-19 | 38 年 |
| BCTI 成品油运输 | 2001-12-27 | 25 年 |
| BCI / BPI / BSI | 同期 | 可用 |

**完全覆盖 2016–2026 的回测区间。**

## PIT 处理

数据源给的是 ``REPORT_DATE``（**数据日期**），不是发布日。保守处理：

    可见时点 = 数据日期 + LAG_DAYS（默认 2 天，见 data.pit.KIND_SHIPPING_INDEX）

理由：BDTI 由波罗的海交易所于伦敦时间当日发布，东方财富转载存在延迟；
且 A 股开盘早于伦敦，**T 日的 BDTI 不可能在 T 日 A 股开盘前被用到**。

用法::

    .\\py.cmd scripts\\sync_shipping.py            # 抓全部 6 个指数
    .\\py.cmd scripts\\sync_shipping.py --check    # 只探测不落库
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from do_modle.data.cache import DataCache  # noqa: E402

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PAGE_SIZE = 500

# 名称 → 东财 INDICATOR_ID
INDICES: dict[str, str] = {
    "BDTI": "EMI00107668",  # 原油运输指数（招商轮船 VLCC 船队直接相关）
    "BCTI": "EMI00107669",  # 成品油运输指数
    "BDI": "EMI00107664",  # 波罗的海干散货指数
    "BCI": "EMI00107666",  # 海岬型
    "BPI": "EMI00107665",  # 巴拿马型
    "BSI": "EMI00107667",  # 超灵便型
}


def fetch_page(indicator_id: str, page: int, *, retries: int = 3) -> tuple[list[dict], int]:
    """取一页，返回 ``(数据行, 总页数)``。带重试。"""
    params = {
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "pageSize": str(PAGE_SIZE),
        "pageNumber": str(page),
        "reportName": "RPT_INDUSTRY_INDEX",
        "columns": "REPORT_DATE,INDICATOR_VALUE,CHANGE_RATE",
        "filter": f'(INDICATOR_ID="{indicator_id}")',
        "source": "WEB",
        "client": "WEB",
    }
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(API, params=params, timeout=25)
            j = r.json()
            res = j.get("result") or {}
            return list(res.get("data") or []), int(res.get("pages") or 0)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"取页失败 page={page}: {type(last_err).__name__}: {last_err}")


def fetch_index(name: str, indicator_id: str, *, verbose: bool = True) -> list[dict]:
    """抓一个指数的全部历史。"""
    rows, pages = fetch_page(indicator_id, 1)
    if not rows:
        if verbose:
            print(f"  {name:<6} ❌ 无数据")
        return []
    out = list(rows)
    for p in range(2, pages + 1):
        more, _ = fetch_page(indicator_id, p)
        if not more:
            break
        out.extend(more)
        time.sleep(0.15)  # 轻量限流，避免被拒
    return out


def to_rows(name: str, raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        d = str(r.get("REPORT_DATE") or "")[:10]
        if not d:
            continue
        try:
            dt = date.fromisoformat(d)
        except ValueError:
            continue
        val = r.get("INDICATOR_VALUE")
        try:
            value = float(val) if val not in (None, "", "-") else None
        except (TypeError, ValueError):
            value = None
        if value is None:
            continue
        chg = r.get("CHANGE_RATE")
        try:
            change = float(chg) if chg not in (None, "", "-") else None
        except (TypeError, ValueError):
            change = None
        out.append(
            {"index_name": name, "date": dt.isoformat(), "value": value, "change_rate": change}
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="data_store")
    parser.add_argument("--check", action="store_true", help="只探测不落库")
    parser.add_argument("--indices", default=None, help="逗号分隔，默认全部")
    args = parser.parse_args()

    names = (
        [s.strip().upper() for s in args.indices.split(",") if s.strip()]
        if args.indices
        else list(INDICES)
    )

    print("=" * 84)
    print("航运运价指数抓取（东方财富公开 API）")
    print("=" * 84)
    print(f"指数：{', '.join(names)}")
    print(f"数据源：{API}")
    print()

    all_rows: list[dict] = []
    for name in names:
        iid = INDICES.get(name)
        if iid is None:
            print(f"  {name:<6} ❌ 未知指数")
            continue
        t0 = time.time()
        raw = fetch_index(name, iid, verbose=not args.check)
        rows = to_rows(name, raw)
        if not rows:
            continue
        all_rows.extend(rows)
        ds = [r["date"] for r in rows]
        print(
            f"  {name:<6} ✅ {len(rows):>5} 行   {min(ds)} ~ {max(ds)}   "
            f"{time.time()-t0:.1f}s"
        )

    if not all_rows:
        print("\n[FAIL] 未取到任何数据")
        return 1

    if args.check:
        print(f"\n[--check] 共 {len(all_rows)} 行，未落库")
        return 0

    db = Path(args.store) / "market.sqlite"
    with DataCache(db) as cache:
        n = cache.upsert("shipping_index", all_rows, ("index_name", "date"))
    print(f"\n落库 shipping_index：{n} 行")
    print(f"缓存：{db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
