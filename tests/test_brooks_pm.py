"""Brooks watcher contract tests with no live exchange."""

from __future__ import annotations

import pytest

from condor.brooks.position_watcher import PositionWatcher, position_fingerprint


def snapshot(**changes):
    value = {
        "correlation_id": "trade-1",
        "symbol": "BTC-USDT",
        "main": {"position_id": "main-1", "side": "LONG", "qty": "1.0"},
        "hedge": {"position_id": "hedge-1", "side": "SHORT", "qty": "0"},
        "executors": [{"executor_id": "ex-1", "status": "RUNNING"}],
        "open_orders": [{"order_id": "order-1", "status": "OPEN"}],
        "fills_cursor": "fill-1",
        "recent_fills": [],
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_watcher_emits_only_fingerprint_changes():
    current = snapshot()
    published = []
    watcher = PositionWatcher(lambda: [current], published.append)

    assert [event["type"] for event in await watcher.poll()] == [
        "POSITION_OPENED",
        "ORDER_CHANGED",
    ]
    assert await watcher.poll() == []
    assert len(published) == 2
    assert published[0]["correlation_id"] == "trade-1"
    assert published[0]["schema"] == "condor.brooks.event.v1"
    assert position_fingerprint(
        snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "1.00"})
    ) == position_fingerprint(snapshot())

    current = snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "0.8"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CHANGED"]
    current = snapshot(main={"position_id": "main-1", "side": "SHORT", "qty": "0.8"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CHANGED"]
    current = snapshot(main={"position_id": "main-1", "side": "LONG", "qty": "0"})
    assert [event["type"] for event in await watcher.poll()] == ["POSITION_CLOSED"]


@pytest.mark.asyncio
async def test_watcher_tracks_hedge_order_executor_and_fill_cursor():
    current = snapshot()
    watcher = PositionWatcher(
        lambda: [current], lambda event: None, initial_snapshots=[current]
    )
    assert await watcher.poll() == []

    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0.3"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_OPENED"]
    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0.4"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_CHANGED"]
    current = snapshot(hedge={"position_id": "hedge-1", "side": "SHORT", "qty": "0"})
    assert [event["type"] for event in await watcher.poll()] == ["HEDGE_REMOVED"]
    current = snapshot(executors=[{"executor_id": "ex-1", "status": "CLOSED"}])
    assert [event["type"] for event in await watcher.poll()] == ["ORDER_CHANGED"]
    current = snapshot(fills_cursor="fill-2", recent_fills=[{"fill_id": "fill-2"}])
    events = await watcher.poll()
    assert [event["type"] for event in events] == ["ORDER_CHANGED", "FILL"]
    assert events[-1]["payload"]["fills_cursor"] == "fill-2"


@pytest.mark.asyncio
async def test_watcher_rejects_ambiguous_bindings_and_invalid_quantities():
    watcher = PositionWatcher(lambda: [snapshot(), snapshot()], lambda event: None)
    with pytest.raises(ValueError, match="duplicate Brooks binding"):
        await watcher.poll()
    with pytest.raises(ValueError, match="nonnegative"):
        position_fingerprint(snapshot(main={"side": "LONG", "qty": "-1"}))
    with pytest.raises(ValueError, match="correlation_id"):
        position_fingerprint({"symbol": "BTC-USDT"})
