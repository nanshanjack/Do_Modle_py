"""一键验证脚本 —— 环境 / 单测 / 数据源连通性 / 字段核对。

用法::

    PY="C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    "$PY" scripts/verify.py                  # 全量验证
    "$PY" scripts/verify.py --skip-network   # 只跑单测（不联网）

退出码：0 = 全部关键项通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import importlib.util
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BAOSTOCK_HOST = "public-api.baostock.com"
BAOSTOCK_PORT = 10030
SYMBOL = "sh.601872"

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"

_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    print(f"{status} {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------- 1. 环境


VENV_PYTHON = r"C:\Users\Administrator\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"


def check_environment() -> None:
    section("1. 环境检查")

    print(f"{INFO} 当前解释器: {sys.executable}")
    expected = Path(VENV_PYTHON)
    if expected.exists() and Path(sys.executable).resolve() != expected.resolve():
        print(f"{INFO} 项目隔离环境: {VENV_PYTHON}")
        print("       若下面有依赖缺失，说明用错了 Python —— 请改用上面的隔离环境，")
        print("       或直接执行  .\\py.cmd scripts\\verify.py")

    v = sys.version_info
    if v >= (3, 11):
        record(PASS, "Python 版本", f"{v.major}.{v.minor}.{v.micro}")
    else:
        record(FAIL, "Python 版本", f"{v.major}.{v.minor} 过低（需 >= 3.11）")

    required = {
        "yaml": "配置加载",
        "pytest": "单测",
        "baostock": "数据源（实机同步必需）",
        "pandas": "baostock 依赖",
    }
    missing: list[str] = []
    for mod, why in required.items():
        found = importlib.util.find_spec(mod) is not None
        if found:
            record(PASS, f"依赖 {mod}", why)
        else:
            missing.append(mod)
            if mod in ("baostock", "pandas"):
                record(WARN, f"依赖 {mod} 缺失", f"{why}")
            else:
                record(FAIL, f"依赖 {mod} 缺失", why)

    if missing:
        print()
        print(f"{INFO} 修复方式（任选其一）：")
        print(f'       1) 用项目隔离环境:  & "{VENV_PYTHON}" scripts\\verify.py')
        print("       2) 或用启动器:      .\\py.cmd scripts\\verify.py")
        print(f'       3) 或安装依赖:      & "{sys.executable}" -m pip install '
              + " ".join(missing))


# ---------------------------------------------------------------- 2. 单测


def run_tests() -> None:
    section("2. 单元测试（离线，不需要网络）")

    if importlib.util.find_spec("pytest") is None:
        record(
            FAIL,
            "无法运行单测",
            f"当前解释器 {sys.executable} 没有 pytest；请用项目隔离环境（见上方修复方式）",
        )
        return

    # 注意：pyproject.toml 的 addopts 已含 "-q"，此处不要再传 -q，
    # 否则变成 -qq 会压掉统计行
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
    summary = "(无输出)"
    for ln in reversed(lines):
        if "passed" in ln or "failed" in ln or "error" in ln:
            summary = ln
            break
    if proc.returncode == 0:
        record(PASS, "全部单测通过", summary)
    else:
        record(FAIL, "单测失败", summary)
        print(proc.stdout[-3000:])


# ---------------------------------------------------------- 3. 数据源连通


def check_port(host: str, port: int, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        return False, f"DNS 解析失败: {exc}"
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True, f"{host} -> {ip}:{port} 可连通"
    except OSError as exc:
        return False, f"{host} -> {ip}:{port} {type(exc).__name__}（端口被阻断）"
    finally:
        s.close()


def check_network() -> None:
    section("3. baostock 连通性")

    ok, detail = check_port(BAOSTOCK_HOST, BAOSTOCK_PORT)
    record(PASS if ok else FAIL, "TCP 端口 10030", detail)
    if not ok:
        record(
            INFO,
            "处置建议",
            "端口不通 → 换数据源（akshare HTTPS / tushare / 本地通达信导出）；"
            "注意 akshare 日线不含 preclose 与 tradestatus",
        )
        return

    if importlib.util.find_spec("baostock") is None:
        record(WARN, "跳过登录", "未安装 baostock")
        return

    from do_modle.data.adapters.baostock_src import BaostockProvider

    provider = BaostockProvider()
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "baostock 登录", f"{type(exc).__name__}: {exc}")
        return
    record(PASS, "baostock 登录", "会话建立成功")

    try:
        check_fields(provider)
    finally:
        provider.disconnect()


def check_fields(provider) -> None:
    """核对 4 项关键字段假设。"""
    from do_modle.objects import AdjustFlag

    print()
    print("--- 字段核对（这是本次验证的核心目的）---")

    # 标的元数据
    try:
        inst = provider.get_instrument(SYMBOL)
        record(PASS, "get_instrument", f"{inst.symbol} {inst.name} board={inst.board.value}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "get_instrument", f"{type(exc).__name__}: {exc}")

    start, end = date(2024, 1, 2), date(2024, 3, 1)

    # 交易日历
    try:
        cal = provider.get_calendar(start, end)
        record(PASS, "get_calendar", f"{len(cal)} 个交易日")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "get_calendar", f"{type(exc).__name__}: {exc}")

    # 日线（不复权 / 后复权）
    bars_none = bars_hfq = []
    try:
        bars_none = provider.get_bars(SYMBOL, "1d", start, end, adjust=AdjustFlag.NONE)
        record(PASS, "get_bars(none)", f"{len(bars_none)} 根")
        if bars_none:
            b = bars_none[0]
            print(
                f"       首根: {b.dt} O={b.open} H={b.high} L={b.low} C={b.close} "
                f"prev_close={b.prev_close} vol={b.volume} "
                f"suspended={b.is_suspended} st={b.is_st}"
            )
            record(
                PASS if b.prev_close > 0 else FAIL,
                "字段核对 preclose",
                f"prev_close={b.prev_close}（涨跌停基准，必须 > 0）",
            )
            record(
                PASS,
                "字段核对 volume 单位",
                f"volume={b.volume}；请与行情软件对比：若软件显示的是『手』，需 ×100",
            )
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "get_bars(none)", f"{type(exc).__name__}: {exc}")

    try:
        bars_hfq = provider.get_bars(SYMBOL, "1d", start, end, adjust=AdjustFlag.HFQ)
        record(PASS, "get_bars(hfq)", f"{len(bars_hfq)} 根")
        if bars_none and bars_hfq:
            same = abs(bars_hfq[0].close - bars_none[0].close) < 1e-9
            record(
                FAIL if same else PASS,
                "字段核对 adjustflag",
                f"hfq close={bars_hfq[0].close} vs none close={bars_none[0].close}"
                + ("（相同 → adjustflag 未生效！）" if same else "（不同 → 复权生效）"),
            )
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "get_bars(hfq)", f"{type(exc).__name__}: {exc}")

    # 停牌名单（未在 baostock 顶层导出，经 resolve_baostock_func 从子模块解析）
    try:
        got = provider.get_suspended(end)
        record(PASS, "get_suspended", f"{len(got)} 只")
        try:
            fields = provider.probe_list_fields("suspended", end)
            print(f"      get_suspended 真实字段: {fields}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        record(WARN, "get_suspended", f"{type(exc).__name__}: {exc}")

    # ST 名单：服务端实测不支持，单标的用 Bar.is_st
    try:
        got = provider.get_risk_warning(end)
        record(PASS, "get_risk_warning", f"{len(got)} 只")
    except Exception as exc:  # noqa: BLE001
        record(
            INFO,
            "get_risk_warning 不可用（已知）",
            f"{exc}；单标的用 Bar.is_st 即可，无需此接口",
        )

    # 分红字段探测（关键：字段名未经实机验证）
    print()
    print("--- 分红字段探测（_FIELD_CANDIDATES 是否命中）---")
    try:
        fields = provider.probe_dividend_fields(SYMBOL, "2023")
        from do_modle.data.adapters.baostock_src import _FIELD_CANDIDATES

        flat = set(fields)
        for semantic, candidates in _FIELD_CANDIDATES.items():
            hit = [c for c in candidates if c in flat]
            if hit:
                record(PASS, f"分红字段 {semantic}", f"命中 {hit[0]}")
            else:
                record(
                    FAIL,
                    f"分红字段 {semantic}",
                    f"候选 {list(candidates)} 均未命中 → 需更新 _FIELD_CANDIDATES",
                )
        print(f"      真实字段: {fields}")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "分红字段探测", f"{type(exc).__name__}: {exc}")

    try:
        divs = provider.get_dividends(SYMBOL, date(2016, 1, 1), date(2026, 9, 1))
        record(PASS, "get_dividends", f"{len(divs)} 条")
        for ca in divs[:5]:
            print(f"       {ca.ex_date} 送转={ca.share_ratio} 现金={ca.cash_per_share}")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "get_dividends", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 汇总


def summarize() -> int:
    section("汇总")
    passed = sum(1 for s, _, _ in _results if s == PASS)
    failed = sum(1 for s, _, _ in _results if s == FAIL)
    warned = sum(1 for s, _, _ in _results if s == WARN)
    print(f"通过 {passed} · 失败 {failed} · 警告 {warned}")
    if failed:
        print()
        print("失败项：")
        for status, name, detail in _results:
            if status == FAIL:
                print(f"  - {name}: {detail}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="一键验证")
    parser.add_argument("--skip-network", action="store_true", help="跳过联网检查")
    args = parser.parse_args()

    print("=" * 72)
    print("A股量化交易系统 · 一键验证")
    print(f"项目根目录: {ROOT}")
    print("=" * 72)

    check_environment()
    run_tests()
    if args.skip_network:
        section("3. baostock 连通性")
        record(INFO, "已跳过", "--skip-network")
    else:
        check_network()
    return summarize()


if __name__ == "__main__":
    raise SystemExit(main())
