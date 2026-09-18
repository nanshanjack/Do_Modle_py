"""P6-d · 财务质量数据抓取（**限流友好**）

设计目标：baostock 财务接口会因高频调用被**会话级限流**（实测连续约 700 次后全部挂死，
连简单日线查询也失效）。故本脚本：

1. **每个请求之间 sleep**（默认 0.5s，可调）
2. **断点续传**：已落库的 (symbol, year, quarter) 跳过
3. **单标的失败隔离**：某标的连续失败 N 次即跳过，不阻塞其它
4. **可长时间后台运行**：适合"跑一晚上"的场景
5. **失败清单落盘**，便于事后补抓

⚠️ **使用前先确认 baostock 已恢复**（跑一次 `--check`）。

用法::

    .\\py.cmd scripts\\sync_financial.py --check          # 只检查连通性
    .\\py.cmd scripts\\sync_financial.py                  # 全量抓取（12 只 × 41 季 × 6 表）
    .\\py.cmd scripts\\sync_financial.py --sleep 1.0      # 更保守的限流
    .\\py.cmd scripts\\sync_financial.py --symbols sh.601872
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baostock as bs  # noqa: E402

from do_modle.data.cache import DataCache  # noqa: E402
from scripts.sync_data import load_universe  # noqa: E402

# 逻辑名 → (baostock 接口名, 落库表名)
# 注意：接口名不能靠 f"query_{key}_data" 拼——`cash_flow` 带下划线，与其它不一致。
FIN_APIS: dict[str, tuple[str, str]] = {
    "profit": ("query_profit_data", "fin_profit"),
    "operation": ("query_operation_data", "fin_operation"),
    "growth": ("query_growth_data", "fin_growth"),
    "balance": ("query_balance_data", "fin_balance"),
    "cashflow": ("query_cash_flow_data", "fin_cashflow"),
    "dupont": ("query_dupont_data", "fin_dupont"),
}

FAIL_LOG = ROOT / "data_store" / "financial_failures.json"


def quarters(start_year: int, end_year: int) -> list[tuple[int, int]]:
    return [(y, q) for y in range(start_year, end_year + 1) for q in (1, 2, 3, 4)]


def _drain(rs) -> list[list[str]]:
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def check_connectivity() -> bool:
    """确认 baostock 已恢复（限流解除）。"""
    print("检查 baostock 连通性…")
    try:
        lg = bs.login()
        if lg.error_code != "0":
            print(f"  ❌ 登录失败: {lg.error_code} {lg.error_msg}")
            return False
        rs = bs.query_history_k_data_plus(
            "sh.601872", "date,close",
            start_date="2024-01-02", end_date="2024-01-05",
            frequency="d", adjustflag="3",
        )
        n = len(_drain(rs))
        bs.logout()
        if n == 0:
            print("  ❌ 日线查询返回 0 行（可能仍被限流）")
            return False
        print(f"  ✅ 正常（日线返回 {n} 行）")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 异常: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--symbols", default=None, help="逗号分隔；默认全研究池")
    parser.add_argument("--universe", default="shipping")
    parser.add_argument("--sleep", type=float, default=0.5, help="每次请求后的等待秒数")
    parser.add_argument("--max-fail", type=int, default=5, help="单标的连续失败上限")
    parser.add_argument("--store", default="data_store")
    parser.add_argument("--check", action="store_true", help="只检查连通性后退出")
    args = parser.parse_args()

    if args.check:
        return 0 if check_connectivity() else 1

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else load_universe(args.universe)
    )
    qs = quarters(args.start_year, args.end_year)

    db_path = Path(args.store) / "market.sqlite"
    cache = DataCache(db_path)

    # ---- 断点续传：已落库的 (symbol, year, quarter) 跳过 ----
    done: dict[str, set[tuple[str, int, int]]] = {k: set() for k in FIN_APIS}
    for api, (_fn, table) in FIN_APIS.items():
        if table in cache.tables():
            for row in cache.read(table):
                done[api].add((str(row["symbol"]), int(row["year"]), int(row["quarter"])))

    total_tasks = len(symbols) * len(qs) * len(FIN_APIS)
    done_tasks = sum(len(v) for v in done.values())
    print("=" * 84)
    print("财务质量数据抓取（限流友好）")
    print("=" * 84)
    print(f"标的 {len(symbols)} 只   季度 {len(qs)} 个   接口 {len(FIN_APIS)} 个"
          f"   → 总任务 {total_tasks}")
    print(f"已落库 {done_tasks} → 剩余 {total_tasks - done_tasks}")
    print(f"请求间隔 {args.sleep}s → 预计耗时约 "
          f"{(total_tasks - done_tasks) * args.sleep / 60:.0f} 分钟")
    print()

    if not check_connectivity():
        print()
        print("[中止] baostock 未恢复。请稍后再试（限流通常几分钟到数小时）。")
        cache.close()
        return 1

    failures: list[dict[str, Any]] = []
    fetched: dict[str, list[dict[str, Any]]] = {k: [] for k in FIN_APIS}
    t0 = time.time()

    bs.login()
    try:
        for si, sym in enumerate(symbols, 1):
            consec_fail = 0
            for api, (fn_name, _table) in FIN_APIS.items():
                fn = getattr(bs, fn_name, None)
                if fn is None:
                    failures.append({"symbol": sym, "api": api,
                                     "error": f"baostock 无接口 {fn_name}"})
                    continue
                for (y, q) in qs:
                    if (sym, y, q) in done[api]:
                        continue
                    if consec_fail >= args.max_fail:
                        failures.append({"symbol": sym, "api": api, "year": y,
                                         "quarter": q, "error": "连续失败达上限，跳过"})
                        continue
                    try:
                        rs = fn(code=sym, year=y, quarter=q)
                        fields = list(rs.fields)
                        for row in _drain(rs):
                            rec = dict(zip(fields, row))
                            rec["symbol"] = sym
                            rec["year"] = y
                            rec["quarter"] = q
                            fetched[api].append(rec)
                        consec_fail = 0
                    except Exception as exc:  # noqa: BLE001
                        consec_fail += 1
                        failures.append({"symbol": sym, "api": api, "year": y,
                                         "quarter": q,
                                         "error": f"{type(exc).__name__}: {exc}"})
                    time.sleep(args.sleep)
            got = sum(len(v) for v in fetched.values())
            print(f"[{si:>2}/{len(symbols)}] {sym}  累计 {got} 行  "
                  f"失败 {len(failures)}  用时 {(time.time()-t0)/60:.1f} 分钟")
    finally:
        bs.logout()

    # ---- 落库 ----
    print()
    print("落库…")
    total_rows = 0
    for api, (_fn, table) in FIN_APIS.items():
        rows = fetched[api]
        if not rows:
            print(f"  {table:16s} 0 行")
            continue
        keys = ("symbol", "year", "quarter")
        n = cache.upsert(table, rows, keys)
        total_rows += n
        print(f"  {table:16s} {n:>5} 行")
    cache.close()

    # **关键校验**：跑了任务却一行未取 → 一定是 bug，不能静默通过
    attempted = total_tasks - done_tasks
    if attempted > 0 and total_rows == 0:
        print()
        print(f"[FAIL] 尝试了 {attempted} 个任务但取回 0 行 —— 脚本或接口有问题，请排查")
        print("       常见原因：接口名拼错 / getattr 返回 None / 参数不被接受")
        return 1

    if failures:
        FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
        FAIL_LOG.write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n失败清单已写入: {FAIL_LOG}（{len(failures)} 条）")
    else:
        print("\n无失败。")

    print(f"\n总用时 {(time.time()-t0)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
