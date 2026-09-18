"""``core/event.py`` 单测。"""

from __future__ import annotations

import pytest

from do_modle.core.event import Event, EventBus, EventType


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def test_subscribe_and_publish(bus: EventBus) -> None:
    seen: list[Event] = []
    bus.subscribe(EventType.BAR, seen.append)
    called = bus.emit(EventType.BAR, "payload")
    assert called == 1
    assert seen[0].payload == "payload"


def test_duplicate_subscribe_registers_once(bus: EventBus) -> None:
    """同一 handler 重复订阅只注册一次，避免重复执行。"""
    calls: list[int] = []

    def handler(event: Event) -> None:
        calls.append(1)

    bus.subscribe(EventType.BAR, handler)
    bus.subscribe(EventType.BAR, handler)
    assert bus.handler_count(EventType.BAR) == 1
    bus.emit(EventType.BAR)
    assert len(calls) == 1


def test_publish_to_multiple_handlers(bus: EventBus) -> None:
    order: list[str] = []
    bus.subscribe(EventType.BAR, lambda e: order.append("a"))
    bus.subscribe(EventType.BAR, lambda e: order.append("b"))
    assert bus.emit(EventType.BAR) == 2
    assert order == ["a", "b"]


def test_unsubscribe(bus: EventBus) -> None:
    handler = lambda e: None  # noqa: E731
    bus.subscribe(EventType.BAR, handler)
    assert bus.unsubscribe(EventType.BAR, handler) is True
    assert bus.unsubscribe(EventType.BAR, handler) is False
    assert bus.emit(EventType.BAR) == 0


def test_publish_unknown_type_is_silent(bus: EventBus) -> None:
    assert bus.emit("NEVER_SUBSCRIBED") == 0


def test_handler_can_unsubscribe_during_publish(bus: EventBus) -> None:
    """publish 遍历快照，handler 在执行中退订不会破坏迭代。"""
    calls: list[str] = []

    def once(event: Event) -> None:
        calls.append("once")
        bus.unsubscribe(EventType.BAR, once)

    bus.subscribe(EventType.BAR, once)
    bus.subscribe(EventType.BAR, lambda e: calls.append("always"))
    bus.emit(EventType.BAR)
    assert calls == ["once", "always"]


def test_clear_removes_handlers_and_history(bus: EventBus) -> None:
    bus.subscribe(EventType.BAR, lambda e: None)
    bus.emit(EventType.BAR)
    bus.clear()
    assert bus.handler_count(EventType.BAR) == 0
    assert bus.published == ()


def test_published_history_for_audit(bus: EventBus) -> None:
    bus.emit(EventType.BEFORE_TRADING)
    bus.emit(EventType.BAR, "b1")
    bus.emit(EventType.BAR, "b2")
    assert len(bus.published) == 3
    assert len(bus.published_of(EventType.BAR)) == 2


def test_handlers_for_returns_snapshot(bus: EventBus) -> None:
    handler = lambda e: None  # noqa: E731
    bus.subscribe(EventType.BAR, handler)
    assert bus.handlers_for(EventType.BAR) == (handler,)
    assert bus.handlers_for(EventType.TRADE) == ()


def test_event_type_all_covers_constants() -> None:
    assert EventType.ALL == {
        "BEFORE_TRADING",
        "BAR",
        "AFTER_TRADING",
        "ORDER",
        "TRADE",
        "REJECT",
        "CORPORATE_ACTION",
    }


def test_event_meta_defaults_empty() -> None:
    assert Event("X").meta == {}


def test_emit_passes_meta() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("X", seen.append)
    bus.emit("X", 1, tag="hello")
    assert seen[0].meta == {"tag": "hello"}
