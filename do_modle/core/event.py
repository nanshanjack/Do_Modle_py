"""事件总线与事件类型。

事件语义借鉴 rqalpha 的 ``EVENT`` 设计（``BEFORE_TRADING`` / ``BAR`` /
``AFTER_TRADING``），实现自写。

**只做 pub/sub，不含任何业务逻辑**——业务在各自的 handler 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["EventType", "Event", "EventBus", "Handler"]


class EventType:
    """事件类型常量（用类属性而非 Enum，便于订阅时直接用字符串）。"""

    BEFORE_TRADING = "BEFORE_TRADING"
    BAR = "BAR"
    AFTER_TRADING = "AFTER_TRADING"
    ORDER = "ORDER"
    TRADE = "TRADE"
    REJECT = "REJECT"
    CORPORATE_ACTION = "CORPORATE_ACTION"

    ALL: frozenset[str] = frozenset(
        {
            BEFORE_TRADING,
            BAR,
            AFTER_TRADING,
            ORDER,
            TRADE,
            REJECT,
            CORPORATE_ACTION,
        }
    )


Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """事件载荷。``payload`` 承载具体对象（Bar / Trade / RejectedOrder …）。"""

    type: str
    payload: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """同步发布/订阅。

    设计约束：

    * 同一 handler 重复订阅**只注册一次**（避免重复执行）
    * ``publish`` 遍历 handler 快照，允许 handler 在执行中订阅/退订
    * 未注册的事件类型可正常发布（无订阅者即静默），便于渐进式接入
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._published: list[Event] = []

    # ---- 订阅 ----

    def subscribe(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> bool:
        handlers = self._handlers.get(event_type)
        if not handlers or handler not in handlers:
            return False
        handlers.remove(handler)
        return True

    def handlers_for(self, event_type: str) -> tuple[Handler, ...]:
        return tuple(self._handlers.get(event_type, ()))

    def handler_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, ()))

    def clear(self) -> None:
        self._handlers.clear()
        self._published.clear()

    # ---- 发布 ----

    def publish(self, event: Event) -> int:
        """同步调用全部订阅者，返回被调用的 handler 数。"""
        self._published.append(event)
        called = 0
        for handler in tuple(self._handlers.get(event.type, ())):
            handler(event)
            called += 1
        return called

    def emit(self, event_type: str, payload: Any = None, **meta: Any) -> int:
        """便捷发布。"""
        return self.publish(Event(event_type, payload, meta))

    # ---- 审计 ----

    @property
    def published(self) -> tuple[Event, ...]:
        """已发布事件的历史（用于调试与确定性校验）。"""
        return tuple(self._published)

    def published_of(self, event_type: str) -> tuple[Event, ...]:
        return tuple(e for e in self._published if e.type == event_type)
