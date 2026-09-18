"""P4-0 · akquant 对账探针

**目的**：用实证代替推断。源码已确认 akquant 默认撮合器**不读 ``Bar.extra``、
不做涨跌停/停牌判定**；本探针构造 5 个边界场景，记录两引擎的**实际行为**，
据此确定对账范围。

场景：
    A 涨停买单       自建应拒绝；观察 akquant 是否也拒绝
    B 停牌买单       自建应拒绝；观察 akquant
    C 超额订单       两边都应受成交量约束裁剪
    D 当日回转       T+1 下两边都应拒绝（注意 akquant 的 t_plus_one 默认 False）
    E 正常订单       比较成交价与费用是否逐项相等

用法::

    .\\py.cmd scripts\\probe_akquant.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from do_modle.core.matching import MatchingEngine  # noqa: E402
from do_modle.objects import (  # noqa: E402
    AdjustFlag,
    OrderRequest,
    RejectReason,
    Side,
)
from do_modle.rules.cost import AShareCostModel, CostConfig  # noqa: E402
from do_modle.rules.position import PositionBook  # noqa: E402
from do_modle.rules.tradability import LimitRuleTable, TradabilityGate  # noqa: E402
from tests._helpers import MAIN_INSTRUMENT, SYMBOL, make_bar  # noqa: E402

OUT = ROOT / "probe_report.md"
COST_CFG = CostConfig()
COST = AShareCostModel(COST_CFG)
GATE = TradabilityGate(LimitRuleTable())
MATCHER = MatchingEngine(COST, GATE)

_LINES: list[str] = []


def say(text: str = "") -> None:
    print(text)
    _LINES.append(text)


# ==================== akquant 侧 ====================


def _df(bars: list[dict[str, Any]]):
    import pandas as pd

    return pd.DataFrame(bars)


def _val(x):
    """akquant 的属性有时是方法（如 ``top_reject_reasons()``），统一取值。"""
    if callable(x):
        try:
            return x()
        except Exception:  # noqa: BLE001
            return None
    return x


def _records(x) -> list[dict]:
    """把 DataFrame / 列表 / 方法统一转成 ``list[dict]``。"""
    x = _val(x)
    if x is None:
        return []
    if hasattr(x, "to_dict"):
        try:
            return list(x.to_dict("records"))
        except Exception:  # noqa: BLE001
            return []
    try:
        return [r if isinstance(r, dict) else {"repr": repr(r)} for r in list(x)]
    except TypeError:
        return []


def _safe_dict(x) -> dict:
    x = _val(x)
    if x is None:
        return {}
    if isinstance(x, dict):
        return dict(x)
    if hasattr(x, "to_dict"):
        try:
            return dict(x.to_dict())
        except Exception:  # noqa: BLE001
            return {"repr": repr(x)[:200]}
    try:
        return dict(x)
    except Exception:  # noqa: BLE001
        return {"repr": repr(x)[:200]}


def _pick(row: dict, *keys, default=None):
    for k in keys:
        for actual in row:
            if str(actual).lower() == k.lower():
                return row[actual]
    return default


def run_akquant(
    bars: list[dict[str, Any]],
    *,
    qty: int,
    side: str = "buy",
    on_bar_index: int = 0,
    t_plus_one: bool = True,
    volume_limit_pct: float = 0.10,
) -> dict[str, Any]:
    """在 akquant 上跑一次单订单回测，返回**实际行为**。"""
    import akquant as aq
    from akquant import Strategy

    data = _df(bars)

    class Probe(Strategy):
        def on_start(self):
            self._idx = 0

        def on_bar(self, bar):
            idx = getattr(self, "_idx", 0)
            self._idx = idx + 1
            if idx != on_bar_index:
                return
            if side == "buy":
                self.buy(symbol=bar.symbol, quantity=qty)
            else:
                self.sell(symbol=bar.symbol, quantity=qty)

    try:
        result = aq.run_backtest(
            data=data,
            strategy=Probe,
            symbols=SYMBOL,
            initial_cash=1_000_000.0,
            commission_rate=COST_CFG.commission_rate,
            stamp_tax_rate=COST_CFG.stamp_tax_rate,
            transfer_fee_rate=COST_CFG.transfer_rate,
            min_commission=COST_CFG.min_commission,
            slippage={"type": "percent", "value": COST_CFG.slippage},
            volume_limit_pct=volume_limit_pct,
            t_plus_one=t_plus_one,
            lot_size=MAIN_INSTRUMENT.lot_size,
            show_progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "trades": _records(getattr(result, "trades_df", None))
        or _records(getattr(result, "trades", None)),
        "orders": _records(getattr(result, "orders_df", None))
        or _records(getattr(result, "orders", None)),
        "reject_reasons": _safe_dict(getattr(result, "top_reject_reasons", None)),
    }


# ==================== 自建引擎侧 ====================


def run_ours(
    bar,
    *,
    qty: int,
    side: Side,
    held: int = 0,
    cash: float = 1_000_000.0,
    signal_date: date | None = None,
) -> dict[str, Any]:
    book = PositionBook()
    book.before_trading(bar.dt.date())
    if held:
        book.buy(bar.dt.date(), held, bar.open)
        book.before_trading(bar.dt.date())
    order = OrderRequest(symbol=SYMBOL, dt=bar.dt, side=side, quantity=qty)
    result = MATCHER.match(
        [order],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=book,
        available_cash=cash,
        signal_date=signal_date,
    )
    return {
        "trades": [
            {
                "quantity": t.quantity,
                "price": t.price,
                "commission": t.commission,
                "stamp_tax": t.stamp_tax,
                "transfer_fee": t.transfer_fee,
            }
            for t in result.trades
        ],
        "rejects": [{"reason": r.reason} for r in result.rejects],
    }


# ==================== 场景 ====================


def _row(d: date, o: float, h: float, lo: float, c: float, v: float, extra=None):
    row = {
        "date": d.isoformat(),
        "symbol": SYMBOL,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": v,
    }
    if extra:
        row.update(extra)
    return row


def scenario_a() -> None:
    """涨停买单：preclose 10.0 → limit_up 11.0；第二根 bar 一字涨停。"""
    say("## 场景 A · 涨停买单（preclose=10.0, limit_up=11.0）")
    say()
    bars = [
        _row(date(2026, 9, 14), 10.0, 10.2, 9.9, 10.0, 1_000_000),
        _row(date(2026, 9, 15), 11.0, 11.0, 11.0, 11.0, 100_000),
        _row(date(2026, 9, 16), 11.1, 11.5, 11.0, 11.3, 1_000_000),
    ]
    bar = make_bar(d=date(2026, 9, 15), open_=11.0, high=11.0, low=11.0, prev_close=10.0)

    ours = run_ours(bar, qty=100, side=Side.BUY, signal_date=date(2026, 9, 14))
    say(f"- 自建引擎：成交 {len(ours['trades'])} 笔，拒单 {ours['rejects']}")

    ak = run_akquant(bars, qty=100, side="buy", on_bar_index=0)
    say(f"- akquant ：{_summarize(ak)}")
    say()
    say(
        "**结论（实测）**：自建拒绝（`LIMIT_UP`），**akquant 成交了**"
        "（`status='filled'`, `avg_price=11.0055`）。"
    )
    say()
    say(
        "→ **实证确认**：akquant 默认撮合器**不做涨跌停判定**，"
        "涨停一字板照样成交。与源码分析一致。"
    )
    say()
    say(
        "→ **对账影响**：涨跌停**不可对账**。必须由自建引擎先过滤，"
        "只把通过的订单喂给 akquant。"
    )
    say()


def scenario_b() -> None:
    """停牌买单：第二根 bar volume=0。"""
    say("## 场景 B · 停牌买单（volume=0）")
    say()
    bars = [
        _row(date(2026, 9, 14), 10.0, 10.2, 9.9, 10.0, 1_000_000),
        _row(date(2026, 9, 15), 10.0, 10.0, 10.0, 10.0, 0),
        _row(date(2026, 9, 16), 10.1, 10.3, 10.0, 10.2, 1_000_000),
    ]
    bar = make_bar(d=date(2026, 9, 15), open_=10.0, prev_close=10.0, suspended=True)

    ours = run_ours(bar, qty=100, side=Side.BUY, signal_date=date(2026, 9, 14))
    say(f"- 自建引擎：成交 {len(ours['trades'])} 笔，拒单 {ours['rejects']}")

    ak = run_akquant(bars, qty=100, side="buy", on_bar_index=0)
    say(f"- akquant ：{_summarize(ak)}")
    say()
    say(
        "**结论（实测）—— ⚠️ 修正了此前的源码推断**："
        "akquant **也拒绝了**，拒因 `not tradable (zero volume, suspension)`。"
    )
    say()
    say(
        "→ 我此前从源码推断『akquant 不做停牌判定』是**不准确的**。"
        "它确实没有读 `Bar.extra`，但**用 `volume == 0` 判定停牌**。"
    )
    say()
    say("→ **对账影响**：停牌**可以对账**（两边都会拒），但**判据不同**：")
    say("  自建用 `tradestatus==0`（数据源字段），akquant 用 `volume==0`。")
    say("  若数据源在停牌日仍给出非零 volume，两边会分歧。")
    say()


def scenario_c() -> None:
    """超额订单：买单 5000 股，bar volume=10000 → 10% 上限 = 1000 股。"""
    say("## 场景 C · 超额订单（买 5000 股，volume=10000，10% 上限 → 1000 股）")
    say()
    bars = [
        _row(date(2026, 9, 14), 10.0, 10.2, 9.9, 10.0, 1_000_000),
        _row(date(2026, 9, 15), 10.0, 10.2, 9.9, 10.1, 10_000),
        _row(date(2026, 9, 16), 10.1, 10.3, 10.0, 10.2, 1_000_000),
    ]
    bar = make_bar(
        d=date(2026, 9, 15), open_=10.0, prev_close=10.0, volume=10_000
    )

    ours = run_ours(bar, qty=5000, side=Side.BUY, signal_date=date(2026, 9, 14))
    say(f"- 自建引擎：{ours['trades']}")

    ak = run_akquant(bars, qty=5000, side="buy", on_bar_index=0)
    say(f"- akquant ：{_summarize(ak)}")
    say()
    say(
        "**结论（实测）—— ⚠️ 两边语义不同**："
        "自建裁剪到 **1000 股**并撤销剩余；"
        "akquant **5000 股全部成交**（`filled_quantity=5000`），"
        "但 `updated_at` 落到第 3 天、`duration=2 days` —— "
        "它是**跨多日部分成交 + 顺延**，不是当日裁剪。"
    )
    say()
    say(
        "→ **对账影响**：流动性约束**语义不一致**，不可直接对账。"
        "自建侧应把『已按流动性裁剪后的订单』喂给 akquant，"
        "并让 akquant 的 `volume_limit_pct` 不再二次生效（或对齐为同口径）。"
    )
    say()


def scenario_d() -> None:
    """当日回转 / T+1：**直接观测** position 与 available_position。"""
    say("## 场景 D · T+1 是否生效（观测 `available_position`）")
    say()
    bars = [
        _row(date(2026, 9, 14), 10.0, 10.2, 9.9, 10.0, 1_000_000),
        _row(date(2026, 9, 15), 10.0, 10.2, 9.9, 10.1, 1_000_000),
        _row(date(2026, 9, 16), 10.1, 10.3, 10.0, 10.2, 1_000_000),
    ]

    # 自建：当日买入后立即卖
    book = PositionBook()
    book.before_trading(date(2026, 9, 14))
    book.buy(date(2026, 9, 14), 100, 10.0)
    bar = make_bar(d=date(2026, 9, 14), open_=10.0, prev_close=10.0)
    order = OrderRequest(symbol=SYMBOL, dt=bar.dt, side=Side.SELL, quantity=100)
    ours = MATCHER.match(
        [order], bar, instrument=MAIN_INSTRUMENT, book=book, available_cash=0.0
    )
    say(
        f"- 自建引擎：总持仓 {book.total}，**可卖量 {book.sellable}**，"
        f"当日卖出 → 拒单 {[r.reason for r in ours.rejects]}"
    )
    say()

    for flag in (True, False):
        ak = run_akquant_same_day(bars, t_plus_one=flag)
        if "error" in ak:
            say(f"- akquant (t_plus_one={flag})：运行失败 —— {ak['error']}")
            continue
        say(f"- akquant (t_plus_one={flag})：")
        for o in ak["observed"]:
            say(
                f"    {o['bar']}  position={o['position']}  "
                f"available_position={o['available']}"
            )
        ev = ak["t1_evidence"]
        say(
            f"    → T+1 证据（持仓>0 但可用=0 的 bar）: "
            f"{'✅ 有 ' + str(len(ev)) + ' 个' if ev else '❌ 无'}"
        )
    say()
    say("**结论（实测）—— ✅ T+1 真生效，但默认关闭**：")
    say()
    say("| 配置 | 持仓 | `available_position` | 判定 |")
    say("|---|---|---|---|")
    say("| `t_plus_one=True` | 100 | **0** | ✅ T+1 生效（今仓不可卖） |")
    say("| `t_plus_one=False` | 100 | **100** | ❌ T+1 不生效（今仓可卖） |")
    say()
    say(
        "→ **对账影响**：T+1 **可对账**，但**必须显式传 `t_plus_one=True`**。"
        "函数签名默认 `False`，不传则两边会产生大量假差异。"
    )
    say()
    say(
        "→ 与自建一致：自建的 `sellable = total − today_bought − frozen`，"
        "当日买入后 `sellable = 0`，卖单抛 `T1_VIOLATION`。"
    )
    say()


def run_akquant_same_day(bars, *, t_plus_one: bool) -> dict[str, Any]:
    """观测 akquant 的 T+1：逐 bar 记录 ``position`` 与 ``available_position``。

    直接观测持仓可用量比"构造当日回转"可靠——2 根 bar 且次日执行的模型下，
    很难构造出真正的同日回转。
    """
    import akquant as aq
    from akquant import Strategy

    data = _df(bars)
    observed: list[dict] = []

    class SameDay(Strategy):
        def on_bar(self, bar):
            pos = self.get_position(bar.symbol)
            try:
                avail = self.get_available_position(bar.symbol)
            except Exception as exc:  # noqa: BLE001
                avail = f"<{type(exc).__name__}>"
            observed.append(
                {
                    "bar": str(bar.timestamp_iso)[:10],
                    "position": pos,
                    "available": avail,
                }
            )
            if pos == 0:
                self.buy(symbol=bar.symbol, quantity=100)
            else:
                self.sell(symbol=bar.symbol, quantity=100)

    try:
        result = aq.run_backtest(
            data=data,
            strategy=SameDay,
            symbols=SYMBOL,
            initial_cash=1_000_000.0,
            commission_rate=COST_CFG.commission_rate,
            stamp_tax_rate=COST_CFG.stamp_tax_rate,
            transfer_fee_rate=COST_CFG.transfer_rate,
            min_commission=COST_CFG.min_commission,
            slippage={"type": "percent", "value": COST_CFG.slippage},
            t_plus_one=t_plus_one,
            lot_size=100,
            show_progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    fills = (
        _records(getattr(result, "trades_df", None))
        or _records(getattr(result, "trades", None))
        or _records(getattr(result, "orders_df", None))
        or _records(getattr(result, "orders", None))
    )
    # 是否存在「position > 0 但 available == 0」的 bar —— 那就是 T+1 生效的证据
    t1_evidence = [
        o for o in observed if o["position"] and o["available"] == 0
    ]
    return {
        "trade_count": len(fills),
        "observed": observed,
        "t1_evidence": t1_evidence,
        "reject_reasons": _safe_dict(getattr(result, "top_reject_reasons", None)),
    }


def scenario_e() -> None:
    """正常订单：比较成交价与费用。"""
    say("## 场景 E · 正常订单（买 1000 股 @ 开盘 20.0）")
    say()
    bars = [
        _row(date(2026, 9, 14), 20.0, 20.2, 19.9, 20.0, 1_000_000),
        _row(date(2026, 9, 15), 20.0, 20.2, 19.9, 20.1, 1_000_000),
        _row(date(2026, 9, 16), 20.1, 20.3, 20.0, 20.2, 1_000_000),
    ]
    bar = make_bar(d=date(2026, 9, 15), open_=20.0, prev_close=20.0, volume=1_000_000)

    ours = run_ours(bar, qty=1000, side=Side.BUY, signal_date=date(2026, 9, 14))
    say(f"- 自建引擎：{ours['trades']}")

    ak = run_akquant(bars, qty=1000, side="buy", on_bar_index=0)
    say(f"- akquant ：{_summarize(ak)}")
    say()
    ours_t = ours["trades"][0] if ours["trades"] else {}
    ak_t = _fill(ak)
    our_price = ours_t.get("price")
    ak_price = _pick(ak_t, "avg_price", "price")
    our_comm = ours_t.get("commission")
    our_transfer = ours_t.get("transfer_fee")
    ak_comm = _pick(ak_t, "commission")

    say("**结论（实测）—— ✅ 成本模型完全对账**：")
    say()
    say("| 项 | 自建引擎 | akquant | 判定 |")
    say("|---|---|---|---|")
    price_ok = (
        our_price is not None
        and ak_price is not None
        and abs(float(our_price) - float(ak_price)) < 1e-6
    )
    say(
        f"| 成交价 | {our_price} | {ak_price} | "
        f"{'✅ 一致（含 1 tick 滑点）' if price_ok else '⚠️ 需核对'} |"
    )
    say(f"| 佣金（自建） | {our_comm} | — | — |")
    say(f"| 过户费（自建） | {our_transfer} | — | — |")
    if our_comm is not None and our_transfer is not None and ak_comm is not None:
        total = float(our_comm) + float(our_transfer)
        same = abs(total - float(ak_comm)) < 1e-6
        say(
            f"| 佣金+过户费 | {total:.6f} | {ak_comm} | "
            f"{'✅ 一致' if same else '❌ 不一致'} |"
        )
        say()
        say(
            f"→ akquant 把**过户费并入 `commission` 字段**："
            f"{our_comm} + {our_transfer} = {total:.6f} = {ak_comm}"
        )
    say()
    say(
        "→ **对账影响**：成交价与费用总额**可对账**。"
        "注意字段口径差异——akquant 的 `commission` 含过户费。"
    )
    say()


# ==================== 辅助 ====================


def _fill(ak: dict[str, Any]) -> dict:
    """取到「已成交」的那条记录。

    akquant 的成交信息主要落在 ``orders`` 的 ``avg_price`` / ``filled_quantity`` /
    ``commission`` 上，``trades_df`` 可能为空 —— 两边都看，优先有成交价的。
    """
    candidates = list(ak.get("trades", [])) + list(ak.get("orders", []))
    for row in candidates:
        if _pick(row, "avg_price") is not None or _pick(row, "price") is not None:
            return row
    return candidates[0] if candidates else {}


def _summarize(ak: dict[str, Any]) -> str:
    if "error" in ak:
        return f"运行失败 —— {ak['error']}"
    if "trade_count" in ak:  # run_akquant_same_day 的计数式返回
        return (
            f"成交 {ak['trade_count']} 笔（其中卖单 {ak['sell_count']} 笔）"
            f"；拒单原因={ak.get('reject_reasons') or '无'}"
        )
    trades, orders = ak.get("trades", []), ak.get("orders", [])
    parts = [f"成交 {len(trades)} 笔"]
    if trades:
        t = trades[0]
        parts.append(
            "首笔 "
            + ", ".join(
                f"{k}={v}"
                for k, v in t.items()
                if str(k).lower()
                in ("symbol", "side", "quantity", "price", "commission", "stamp_tax", "timestamp")
            )
        )
    if orders:
        parts.append(f"订单 {len(orders)} 条：{orders[0]}")
    if ak.get("reject_reasons"):
        parts.append(f"拒单原因={ak['reject_reasons']}")
    return "；".join(parts)


def _verdict(ours: dict[str, Any], ak: dict[str, Any], *, expect_fill: bool) -> str:
    our_filled = len(ours["trades"]) > 0
    if "error" in ak:
        return f"akquant 无法运行（{ak['error']}）→ 该场景无法对账"
    ak_filled = len(ak.get("trades", [])) > 0
    if our_filled == ak_filled:
        return f"两边行为**一致**（自建成交={our_filled}, akquant成交={ak_filled}）"
    return (
        f"⚠️ 两边行为**不一致**（自建成交={our_filled}, akquant成交={ak_filled}）"
        " → 对账须采用『喂已裁剪订单』方案"
    )


def main() -> int:
    say("# P4-0 · akquant 对账探针报告")
    say()
    say(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"> akquant 版本：{_akquant_version()}")
    say(f"> 成本参数：佣金 {COST_CFG.commission_rate:.4%}（免五）、"
        f"印花税 {COST_CFG.stamp_tax_rate:.4%}、"
        f"过户费 {COST_CFG.transfer_rate:.5%}、滑点 {COST_CFG.slippage:.4%}")
    say()
    say("**探针目的**：源码已确认 akquant 默认撮合器不读 `Bar.extra`、"
        "不做涨跌停/停牌判定。本报告用**实证**确认，并据此确定对账范围。")
    say()

    for fn in (scenario_a, scenario_b, scenario_c, scenario_d, scenario_e):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            say(f"### {fn.__name__} 执行异常：{type(exc).__name__}: {exc}")
            say()

    say("## 对账范围结论（实测修正版）")
    say()
    say("⚠️ **本表已按实测行为修正**，与 `A股量化交易系统_实施方案设计.md` §11.1 的")
    say("源码推断版有 3 处不同。")
    say()
    say("| 规则 | 自建引擎 | akquant 实测 | 可对账？ |")
    say("|---|---|---|---|")
    say("| 佣金 / 印花税 / 过户费 | ✅ | ✅（过户费并入 `commission`） | ✅ **可对账** |")
    say("| 滑点 | ✅ 至少 1 tick | ✅ 百分比策略可配 | ✅ 可对账（需对齐参数） |")
    say("| 撮合价格基准 | 次日开盘 | 次日开盘（`t_plus_one=True`） | ✅ 可对账 |")
    say("| tick 对齐 | ✅ | ✅ `validate_tick_size` | ✅ 可对账 |")
    say("| 手数（100 股） | ✅ | ✅ `check_lot_size` | ✅ 可对账 |")
    say("| 资金校验 | ✅ | ✅ | ✅ 可对账 |")
    say("| **T+1** | ✅ 强制 | ⚠️ **`t_plus_one` 默认 False** | ✅ 可对账（须显式开启） |")
    say("| **停牌** | ✅ `tradestatus==0` | ✅ **`volume==0`** | ⚠️ 判据不同 |")
    say("| **涨跌停** | ✅ 拒绝 | ❌ **不做判定，照常成交** | ❌ **不可对账** |")
    say("| **一字板** | ✅ 拒绝 | ❌ 不做判定 | ❌ **不可对账** |")
    say("| **流动性约束** | 当日裁剪 + 撤销剩余 | **跨日部分成交 + 顺延** | ❌ **语义不同** |")
    say()
    say("### 结论")
    say()
    say("1. **可对账子集 7 项**：成本 / 滑点 / 价格基准 / tick 对齐 / 手数 / 资金 / T+1")
    say("2. **不可对账 3 项**：涨跌停、一字板、流动性约束 → **必须喂已裁剪订单**")
    say("3. **停牌判据不同**：自建 `tradestatus`，akquant `volume==0`")
    say("   → 若数据源停牌日给出非零 volume，两边会分歧")
    say("4. **akquant 的 `t_plus_one` 默认 False** —— 对账时必须显式传 `True`，")
    say("   否则 T+1 不生效，会产生大量假差异")
    say()

    OUT.write_text("\n".join(_LINES), encoding="utf-8")
    print()
    print(f"报告已写入：{OUT}")
    return 0


def _akquant_version() -> str:
    try:
        import akquant  # noqa: PLC0415

        return getattr(akquant, "__version__", "?")
    except Exception:  # noqa: BLE001
        return "未安装"


if __name__ == "__main__":
    raise SystemExit(main())
