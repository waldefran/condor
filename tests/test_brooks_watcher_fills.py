"""Wave 2 Finding 12: the watcher carries real fills with a reliable cursor.

Decision (A): recent fills are real FILLED venue orders read through
``trading.search_orders``; the cursor comes from the venue pagination and falls
back to the last fill id. A fills read that fails degrades to empty fills only
-- position/executor/order transitions still wake consumers. No fill is ever
invented. These tests pin that contract, including that FILL-driven wakes fire
when fills advance and never silently stop the position wakes when they do not.
"""

import asyncio
import json

from condor.brooks.adapters import build_watcher_provider
from condor.brooks.position_watcher import PositionWatcher

ACCOUNT = "acct"
CONNECTOR = "binance_perpetual"
CONTROLLER = "ctrl"
SYMBOL = "BTC-USDT"


class FakeTrading:
    def __init__(self, outer):
        self._outer = outer

    async def get_positions(self, account_names=None, connector_names=None, limit=50):
        return {"data": [dict(row) for row in self._outer.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ):
        return {"data": [dict(row) for row in self._outer.orders]}

    async def search_orders(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        status=None,
        limit=50,
    ):
        self._outer.search_calls.append(
            {"trading_pairs": trading_pairs, "status": status}
        )
        if self._outer.fail_fills:
            raise TimeoutError("fills unreadable")
        result: dict = {"data": [dict(row) for row in self._outer.fills]}
        if self._outer.fills_cursor is not None:
            result["pagination"] = {"next_cursor": self._outer.fills_cursor}
        return result


class FakeExecutors:
    def __init__(self, outer):
        self._outer = outer

    async def search_executors(self, **kwargs):
        return {"data": []}


class FakeClient:
    def __init__(self, *, with_search=True):
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.fills_cursor: str | None = None
        self.fail_fills = False
        self.search_calls: list[dict] = []
        self.trading = FakeTrading(self)
        if not with_search:
            self.trading.search_orders = None  # type: ignore[attr-defined]
        self.executors = FakeExecutors(self)


def write_binding(root, correlation_id="trade-1"):
    trades = root / "trades" / correlation_id
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": correlation_id,
                "account_name": ACCOUNT,
                "connector_name": CONNECTOR,
                "controller_id": CONTROLLER,
                "symbol": SYMBOL,
                "main_position_id": "p1",
                "main_executor_id": "e1",
                "main_side": "LONG",
                "status": "submitted",
            }
        )
    )


def provider_for(fake, root):
    return build_watcher_provider(
        fake,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
        symbols=[SYMBOL],
        state_root=root,
    )


def fill(order_id, qty="0.5"):
    return {
        "client_order_id": order_id,
        "trading_pair": SYMBOL,
        "status": "FILLED",
        "filled_amount": qty,
        "price": "100",
    }


def test_provider_carries_real_fills_with_venue_cursor(tmp_path):
    write_binding(tmp_path)
    fake = FakeClient()
    fake.fills = [fill("ord-1")]
    fake.fills_cursor = "cur-9"
    snapshots = asyncio.run(provider_for(fake, tmp_path)())
    assert len(snapshots) == 1
    assert snapshots[0]["fills_cursor"] == "cur-9"
    assert [item["fill_id"] for item in snapshots[0]["recent_fills"]] == ["ord-1"]
    assert fake.search_calls and fake.search_calls[0]["status"] == "FILLED"


def test_fill_wake_fires_when_cursor_advances(tmp_path):
    write_binding(tmp_path)
    fake = FakeClient()
    fake.fills = [fill("ord-1")]
    fake.fills_cursor = "cur-9"
    emitted: list = []
    watcher = PositionWatcher(provider_for(fake, tmp_path), emitted.append)
    asyncio.run(watcher.poll())
    assert [event["type"] for event in emitted] == []

    fake.fills = [fill("ord-1"), fill("ord-2")]
    fake.fills_cursor = "cur-10"
    asyncio.run(watcher.poll())
    assert [event["type"] for event in emitted] == ["FILL"]
    assert emitted[0]["payload"]["previous_cursor"] == "cur-9"
    assert emitted[0]["payload"]["fills_cursor"] == "cur-10"


def test_failed_fills_read_keeps_position_wakes_alive(tmp_path):
    write_binding(tmp_path)
    fake = FakeClient()
    fake.fail_fills = True
    snapshots = asyncio.run(provider_for(fake, tmp_path)())
    assert len(snapshots) == 1
    assert snapshots[0]["fills_cursor"] == ""
    assert snapshots[0]["recent_fills"] == []


def test_client_without_order_history_degrades_to_empty_fills(tmp_path):
    write_binding(tmp_path)
    fake = FakeClient(with_search=False)
    snapshots = asyncio.run(provider_for(fake, tmp_path)())
    assert len(snapshots) == 1
    assert snapshots[0]["fills_cursor"] == ""
    assert snapshots[0]["recent_fills"] == []
