"""Observe Brooks-owned venue state and publish changes without placing orders.

The snapshot provider must return only trades with an explicit Brooks binding.  In
particular, this watcher never assigns MAIN or HEDGE ownership from position side,
size, or ordering in a venue response.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError("watcher state must be a mapping or Pydantic model")


def _quantity(value: Any) -> str:
    try:
        amount = Decimal(str(value if value is not None else 0))
    except InvalidOperation as exc:
        raise ValueError("invalid position quantity") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("position quantity must be finite and nonnegative")
    return format(amount.normalize(), "f")


def _position(value: Any) -> dict[str, str]:
    data = _mapping(value)
    position = {
        "id": str(data.get("position_id") or data.get("id") or ""),
        "side": str(data.get("side") or "").upper(),
        "qty": _quantity(data.get("qty", data.get("quantity", data.get("size", 0)))),
    }
    if Decimal(position["qty"]) > 0 and (
        not position["id"] or position["side"] not in {"LONG", "SHORT"}
    ):
        raise ValueError("active position requires explicit ownership id and side")
    return position


def _orders(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value or []:
        data = _mapping(item)
        result.append(
            {
                "id": str(data.get("order_id") or data.get("id") or ""),
                "status": str(data.get("status") or "").upper(),
                "filled_qty": _quantity(
                    data.get("filled_qty", data.get("filled_quantity", 0))
                ),
            }
        )
    return sorted(result, key=lambda item: (item["id"], item["status"]))


def _executors(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value or []:
        data = _mapping(item)
        result.append(
            {
                "id": str(data.get("executor_id") or data.get("id") or ""),
                "status": str(data.get("status") or "").upper(),
            }
        )
    return sorted(result, key=lambda item: item["id"])


def normalized_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable fields that can change management decisions."""
    data = _mapping(snapshot)
    correlation_id = data.get("correlation_id")
    symbol = data.get("symbol")
    if not correlation_id or not symbol:
        raise ValueError("bound snapshot requires correlation_id and symbol")
    fills = [_mapping(item) for item in data.get("recent_fills", data.get("fills", []))]
    cursor = data.get("fills_cursor")
    if cursor is None and fills:
        last = fills[-1]
        cursor = last.get("cursor", last.get("fill_id", last.get("id")))
    return {
        "correlation_id": str(correlation_id),
        "symbol": str(symbol),
        "main": _position(data.get("main", data.get("main_position"))),
        "hedge": _position(data.get("hedge", data.get("hedge_position"))),
        "executors": _executors(data.get("executors", [])),
        "open_orders": _orders(data.get("open_orders", [])),
        "fills_cursor": str(cursor) if cursor is not None else "",
        "recent_fills": fills,
    }


def position_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Stable hash across reordered collections and equivalent decimal quantities."""
    state = normalized_snapshot(snapshot)
    state.pop("recent_fills")
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _transition(
    before: dict[str, Any] | None, after: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    old = before or {
        "main": {"qty": "0", "side": "", "id": ""},
        "hedge": {"qty": "0", "side": "", "id": ""},
        "open_orders": [],
        "executors": [],
        "fills_cursor": "",
        "recent_fills": [],
    }
    for name, prefix in (("main", "POSITION"), ("hedge", "HEDGE")):
        previous, current = old[name], after[name]
        was_open = Decimal(previous["qty"]) > 0
        is_open = Decimal(current["qty"]) > 0
        if not was_open and is_open:
            event_type = f"{prefix}_OPENED"
        elif was_open and not is_open:
            event_type = f"{prefix}_{'CLOSED' if prefix == 'POSITION' else 'REMOVED'}"
        elif is_open and current != previous:
            event_type = f"{prefix}_CHANGED"
        else:
            event_type = None
        if event_type:
            events.append((event_type, {"previous": previous, "current": current}))
    if (
        old["open_orders"] != after["open_orders"]
        or old["executors"] != after["executors"]
    ):
        events.append(
            (
                "ORDER_CHANGED",
                {"open_orders": after["open_orders"], "executors": after["executors"]},
            )
        )
    if before is not None and old["fills_cursor"] != after["fills_cursor"]:
        events.append(
            (
                "FILL",
                {
                    "previous_cursor": old["fills_cursor"],
                    "fills_cursor": after["fills_cursor"],
                    "recent_fills": after["recent_fills"],
                },
            )
        )
    return events


class PositionWatcher:
    """Poll a read-only provider of Brooks-bound snapshots.

    ``publish`` may be an EventBus with a ``publish`` method or a callback.
    ``poll`` is idempotent for unchanged snapshots; a process restart can seed
    the last fingerprints from small durable cursors via ``initial_snapshots``.
    """

    def __init__(
        self,
        get_bound_snapshots: Callable[
            [], Iterable[Mapping[str, Any]] | Awaitable[Iterable[Mapping[str, Any]]]
        ],
        publish: Any,
        *,
        initial_snapshots: Iterable[Mapping[str, Any]] = (),
        on_snapshot: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.get_bound_snapshots = get_bound_snapshots
        self.publish = publish
        self.on_snapshot = on_snapshot
        self._previous: dict[str, dict[str, Any]] = {}
        self._fingerprints: dict[str, str] = {}
        for snapshot in initial_snapshots:
            state = normalized_snapshot(snapshot)
            key = state["correlation_id"]
            self._previous[key] = state
            self._fingerprints[key] = position_fingerprint(snapshot)

    async def poll(self) -> list[Any]:
        snapshots = self.get_bound_snapshots()
        if inspect.isawaitable(snapshots):
            snapshots = await snapshots
        emitted: list[Any] = []
        seen: set[str] = set()
        for snapshot in snapshots:
            state = normalized_snapshot(snapshot)
            key = state["correlation_id"]
            if key in seen:
                raise ValueError(f"duplicate Brooks binding: {key}")
            seen.add(key)
            # Lifecycle reconciliation must also run for an unchanged first
            # flat snapshot after restart; transitions alone cannot recover it.
            if self.on_snapshot is not None:
                result = self.on_snapshot(snapshot)
                if inspect.isawaitable(result):
                    await result
            fingerprint = position_fingerprint(snapshot)
            if self._fingerprints.get(key) == fingerprint:
                continue
            for event_type, payload in _transition(self._previous.get(key), state):
                envelope = {
                    "schema": "condor.brooks.event.v1",
                    "event_id": str(uuid4()),
                    "type": event_type,
                    "created_at_ms": int(time.time() * 1000),
                    "symbol": state["symbol"],
                    "correlation_id": key,
                    "causation_id": None,
                    "payload": payload,
                }
                callback = getattr(self.publish, "publish", self.publish)
                if hasattr(self.publish, "publish"):
                    from condor.brooks.events import BrooksEvent, EventType

                    event = BrooksEvent(
                        type=EventType(event_type),
                        symbol=state["symbol"],
                        payload=payload,
                        correlation_id=key,
                        event_id=envelope["event_id"],
                        created_at_ms=envelope["created_at_ms"],
                    )
                else:
                    event = envelope
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
                emitted.append(event)
            self._previous[key] = state
            self._fingerprints[key] = fingerprint
        return emitted

    async def run(self, stop: asyncio.Event, *, frequency_sec: float = 10) -> None:
        """Run as a supervisor-owned task until cancellation or stop."""
        if frequency_sec <= 0:
            raise ValueError("watcher frequency must be positive")
        while not stop.is_set():
            await self.poll()
            try:
                await asyncio.wait_for(stop.wait(), timeout=frequency_sec)
            except TimeoutError:
                pass
