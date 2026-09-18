"""P6-d · 正交信息源可得性探针

**只做只读探测，不写任何数据、不建管线。**

目标：找出与「价格 / 量能 / 估值 / 预告」正交的**新信息源**，
并确认其 **PIT 可行性**（是否有可见时点字段）。

候选源：

    A 财务质量（盈利/营运/成长/偿债/现金流/杜邦）—— baostock
    B 分红（是否含公告日）—— baostock
    C 分析师评级 —— futu-mcp（另测）
    D 行业归属 —— baostock

用法::

    .\\py.cmd scripts\\probe_alt_sources.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baostock as bs  # noqa: E402

from scripts.sync_data import load_universe  # noqa: E402

QUARTERS = [
    (y, q) for y in range(2016, 2027) for q in (1, 2, 3, 4) if (y, q) <= (2026, 2)
]

FIN_APIS = {
    "profit": "query_profit_data",
    "operation": "query_operation_data",
    "growth": "query_growth_data",
    "balance": "query_balance_data",
    "cashflow": "query_cash_flow_data",
    "dupont": "query_dupont_data",
}


def _drain(rs) -> list[list[str]]:
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def probe_financial(symbol: str) -> dict[str, Any]:
    """探财务接口：字段 + 数据量 + 是否有可见时点线索。"""
    out: dict[str, Any] = {}
    for key, fn_name in FIN_APIS.items():
        fn = getattr(bs, fn_name, None)
        if fn is None:
            out[key] = {"error": "接口不存在"}
            continue
        total, fields, sample = 0, [], None
        try:
            for y, q in QUARTERS:
                rs = fn(code=symbol, year=y, quarter=q)
                rows = _drain(rs)
                if rows:
                    if not fields:
                        fields = list(rs.fields)
                        sample = rows[0]
                    total += len(rows)
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out[key] = {"rows": total, "fields": fields, "sample": sample}
    return out


def probe_dividend_fields(symbol: str) -> dict[str, Any]:
    """探分红接口：是否有**公告日**（决定 PIT 可行性）。"""
    rs = bs.query_dividend_data(code=symbol, year="2020", yearType="report")
    rows = _drain(rs)
    return {"fields": list(rs.fields), "rows": rows[:2]}


def probe_industry(symbol: str) -> dict[str, Any]:
    rs = bs.query_stock_industry(code=symbol)
    rows = _drain(rs)
    return {"fields": list(rs.fields), "rows": rows}


def main() -> int:
    symbols = load_universe("shipping")
    bs.login()
    try:
        print("=" * 96)
        print("P6-d 正交信息源探针（只读）")
        print("=" * 96)

        # ---- A. 财务质量 ----
        print()
        print("## A. 财务质量接口（baostock）")
        print()
        for sym in symbols[:3]:
            print(f"--- {sym} ---")
            res = probe_financial(sym)
            for key, info in res.items():
                if "error" in info:
                    print(f"  {key:<10} ❌ {info['error']}")
                    continue
                has_pub = [
                    f for f in info["fields"]
                    if any(t in f.lower() for t in ("pub", "date", "stat"))
                ]
                print(
                    f"  {key:<10} {info['rows']:>4} 行   "
                    f"PIT 相关字段: {has_pub or '（无！）'}"
                )
                if info["sample"]:
                    print(f"             样例: {info['sample']}")
            print()

        # ---- B. 分红公告日 ----
        print("## B. 分红接口字段（是否有公告日）")
        print()
        div = probe_dividend_fields("sh.601872")
        print(f"  fields: {div['fields']}")
        for r in div["rows"]:
            print(f"  样例: {r}")
        pub_like = [f for f in div["fields"] if any(t in f.lower() for t in ("pub", "ann", "date"))]
        print(f"  → 疑似日期字段: {pub_like}")
        print()

        # ---- C. 行业归属 ----
        print("## C. 行业归属")
        print()
        ind = probe_industry("sh.601872")
        print(f"  fields: {ind['fields']}")
        for r in ind["rows"]:
            print(f"  样例: {r}")
        print()

        # ---- D. 抽样数据量统计（**只抽 3 只**，避免 2952 次查询超时）----
        print("## D. 抽样财务数据量（3 只 × 41 季，外推全池）")
        print()
        totals = {k: 0 for k in FIN_APIS}
        covered = {k: set() for k in FIN_APIS}
        sample_syms = symbols[:3]
        for sym in sample_syms:
            for key, fn_name in FIN_APIS.items():
                fn = getattr(bs, fn_name)
                for y, q in QUARTERS:
                    try:
                        rows = _drain(fn(code=sym, year=y, quarter=q))
                    except Exception:  # noqa: BLE001
                        continue
                    if rows:
                        totals[key] += len(rows)
                        covered[key].add(sym)
        for key, n in totals.items():
            print(
                f"  {key:<10} {n:>5} 行 / {len(sample_syms)} 只   "
                f"覆盖 {len(covered[key])}/{len(sample_syms)}   "
                f"外推全池 ≈ {int(n / len(sample_syms) * len(symbols))} 行"
            )
        print()
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
