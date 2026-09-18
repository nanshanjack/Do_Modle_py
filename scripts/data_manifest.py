"""生成/校验数据指纹清单 ``data_store/MANIFEST.md``

## 为什么需要它

``data_store/market.sqlite`` 是**二进制**（11 MB），每次同步都会重写：

* 提交它 → 二进制 diff 无意义，仓库快速膨胀
* 完全不记录 → 无法验证「重跑同步后数据是否与产生结论时一致」

**折中方案**：不提交 sqlite，但提交一份**指纹清单**（行数 + 日期区间 + SHA256）。
任何人重跑同步后，用 ``--verify`` 就能确认数据是否一致。

用法::

    .\\py.cmd scripts\\data_manifest.py            # 生成清单
    .\\py.cmd scripts\\data_manifest.py --verify   # 校验当前 sqlite 与清单是否一致
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "data_store" / "MANIFEST.md"

# 表 → 用于日期区间的列（None 表示该表无日期列）
TABLE_DATE_COL: dict[str, str | None] = {
    "daily_bar_none": "date",
    "daily_bar_hfq": "date",
    "calendar": "date",
    "instrument": "list_date",
    "dividend": "ex_date",
    "forecast": "pub_date",
    "shipping_index": "date",
    "fin_profit": "statDate",
    "fin_operation": "statDate",
    "fin_growth": "statDate",
    "fin_balance": "statDate",
    "fin_cashflow": "statDate",
    "fin_dupont": "statDate",
}


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def collect(db: Path) -> list[dict]:
    out: list[dict] = []
    con = sqlite3.connect(db)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for t in tables:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            cols = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
            date_col = TABLE_DATE_COL.get(t)
            lo = hi = "—"
            if date_col and date_col in cols:
                r = con.execute(
                    f'SELECT MIN("{date_col}"), MAX("{date_col}") FROM "{t}"'
                ).fetchone()
                if r and r[0]:
                    lo, hi = str(r[0])[:10], str(r[1])[:10]
            syms = "—"
            if "symbol" in cols:
                syms = str(
                    con.execute(f'SELECT COUNT(DISTINCT symbol) FROM "{t}"').fetchone()[0]
                )
            out.append(
                {"table": t, "rows": n, "start": lo, "end": hi, "symbols": syms}
            )
    finally:
        con.close()
    return out


def render(db: Path, stats: list[dict], digest: str, size: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 数据指纹清单",
        "",
        f"> 生成时间：{now}",
        f"> 文件：`{db.name}`",
        f"> 大小：{size:,} 字节（{size / 1048576:.2f} MB）",
        f"> **SHA256**：`{digest}`",
        "",
        "**本文件已提交到 git；`market.sqlite` 未提交**（见 `.gitignore`）。",
        "重跑同步后可用 `scripts/data_manifest.py --verify` 校验是否一致。",
        "",
        "## 重新生成数据",
        "",
        "```powershell",
        ".\\py.cmd scripts\\sync_data.py --universe shipping --start 2016-01-01 --end 2026-09-01",
        ".\\py.cmd scripts\\sync_financial.py",
        ".\\py.cmd scripts\\sync_shipping.py",
        "```",
        "",
        "## 表统计",
        "",
        "| 表 | 行数 | 起始 | 结束 | 标的数 |",
        "|---|---:|---|---|---:|",
    ]
    for s in stats:
        lines.append(
            f"| `{s['table']}` | {s['rows']:,} | {s['start']} | {s['end']} | {s['symbols']} |"
        )
    lines += [
        "",
        "## 数据源",
        "",
        "| 表 | 来源 |",
        "|---|---|",
        "| `daily_bar_none` / `daily_bar_hfq` / `calendar` / `instrument` / `dividend` / `forecast` | baostock |",
        "| `fin_*`（6 张） | baostock 财务接口（`query_profit_data` 等） |",
        "| `shipping_index` | 东方财富公开 API（`datacenter-web.eastmoney.com`） |",
        "",
        "## PIT 处理",
        "",
        "| 数据 | 可见时点 |",
        "|---|---|",
        "| 日线 Bar | 当日 15:00 |",
        "| 分红送转 | 除权日 09:00 |",
        "| **业绩预告/快报** | **公告日 + 1 天 09:00** |",
        "| **财务季报** | **公告日 + 1 天**（无公告日时退回「报告期 + 法定滞后」） |",
        "| **航运运价指数** | **数据日期 + 2 天**（东财转载有延迟） |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data_store/market.sqlite")
    parser.add_argument("--verify", action="store_true", help="校验而非生成")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[FAIL] 数据文件不存在：{db}")
        return 1

    size = db.stat().st_size
    digest = sha256(db)

    if args.verify:
        if not MANIFEST.exists():
            print(f"[FAIL] 清单不存在：{MANIFEST}")
            return 1
        text = MANIFEST.read_text(encoding="utf-8")
        import re

        m = re.search(r"\*\*SHA256\*\*：`([0-9a-f]{64})`", text)
        if not m:
            print("[FAIL] 清单中未找到 SHA256")
            return 1
        expected = m.group(1)
        ok = expected == digest
        print(f"清单 SHA256：{expected}")
        print(f"当前 SHA256：{digest}")
        print()
        if ok:
            print("[PASS] 数据与清单一致")
            return 0
        print("[FAIL] 数据与清单**不一致** —— 可能重跑过同步。")
        print("       若结论基于旧数据，需重新验证；或重新生成清单（会改变溯源）。")
        return 1

    stats = collect(db)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(render(db, stats, digest, size), encoding="utf-8")

    total = sum(s["rows"] for s in stats)
    print("=" * 72)
    print("数据指纹清单已生成")
    print("=" * 72)
    print(f"文件   {db}")
    print(f"大小   {size:,} 字节（{size / 1048576:.2f} MB）")
    print(f"SHA256 {digest}")
    print(f"表数   {len(stats)}   总行数 {total:,}")
    print()
    for s in stats:
        print(f"  {s['table']:<18}{s['rows']:>8,} 行   {s['start']} ~ {s['end']}")
    print()
    print(f"清单已写入：{MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
