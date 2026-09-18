"""引擎层——事件总线、时钟、撮合、账务、记录。

模块划分::

    event.py      EventBus / EventType / Event
    clock.py      Clock       统一时间源（回测 / 实盘）
    matching.py   MatchingEngine  撮合（唯一撮合点）
    ledger.py     Ledger      账务（唯一账务点）
    recorder.py   Recorder    逐日状态快照
    backtest.py   BacktestEngine  编排（最小闭环）

**依赖方向**：``core`` 可依赖 ``objects`` / ``numeric`` / ``rules`` / ``data`` / ``config``，
但**不得依赖** ``strategy`` / ``execution`` / ``evaluation`` / ``app``。
策略与风控通过**可调用对象 / 结构化协议**注入，避免反向依赖。

**三个"唯一性"约束**（架构红线）：

* 撮合只在 :class:`~do_modle.core.matching.MatchingEngine` 发生
* 账务只在 :class:`~do_modle.core.ledger.Ledger` 更新
* 风控只在注入的 ``pre_trade_hook`` 里发生
"""

from __future__ import annotations

from do_modle.core.backtest import BacktestEngine, BacktestResult, EngineContext
from do_modle.core.clock import Clock
from do_modle.core.event import Event, EventBus, EventType
from do_modle.core.ledger import Ledger
from do_modle.core.matching import MatchResult, MatchingEngine, RejectedOrder
from do_modle.core.recorder import Recorder

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "Clock",
    "EngineContext",
    "Event",
    "EventBus",
    "EventType",
    "Ledger",
    "MatchResult",
    "MatchingEngine",
    "Recorder",
    "RejectedOrder",
]
