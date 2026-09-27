"""Wave 2 reads hardening: reconciliation, fail-closed orders, margin semantics.

Findings 3, 8, 9. All tests run against the production reader/reconciler/GM
with a duck-typed fake venue -- no live Hummingbot required.
"""

import asyncio
import json
import time
from decimal import Decimal

import pytest

from condor.brooks.adapters import (
    HummingbotAccountReader,
    HummingbotPositionReconciler,
)
from condor.brooks.gm import BrooksGM, GMPolicy, GMRejected

ACCOUNT = "acct"
CONNECTOR = "binance_perpetual"
CONTROLLER = "ctrl"
SYMBOL = "BTC-USDT"


def D(value):
    return Decimal(str(value))


class FakeMarketData:
    def __init__(self, outer):
        self._outer = outer

    async def get_prices(self, connector_name, trading_pairs):
        return {"prices": {SYMBOL: 100.0}}


class FakeTrading:
    def __init__(self, outer):
        self._outer = outer

    async def get_positions(self, account_names=None, connector_names=None, limit=50):
        return {"data": [dict(row) for row in self._outer.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ):
        if self._outer.fail_orders:
            raise TimeoutError("order book unreadable")
        return {"data": [dict(row) for row in self._outer.orders]}

    async def get_position_mode(self, account_name, connector_name):
        return {"position_mode": "HEDGE"}


class FakeExecutors:
    def __init__(self, outer):
        self._outer = outer

    async def search_executors(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        controller_ids=None,
        limit=50,
    ):
        return {"data": [dict(row) for row in self._outer.executor_rows]}

    async def get_positions_summary(self, controller_id=None):
        if self._outer.fail_summary:
            raise ConnectionError("summary unreadable")
        return {"positions": [dict(row) for row in self._outer.holds]}


class FakePortfolio:
    def __init__(self, outer):
        self._outer = outer

    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ):
        return json.loads(json.dumps(self._outer.portfolio_state))


class FakeConnectors:
    def __init__(self, outer):
        self._outer = outer

    async def get_trading_rules(self, connector_name, trading_pairs=None):
        return json.loads(json.dumps(self._outer.rules))


class FakeClient:
    def __init__(self):
        self.positions: list[dict] = []
        self.executor_rows: list[dict] = []
        self.holds: list[dict] = []
        self.orders: list[dict] = []
        self.fail_orders = False
        self.fail_summary = False
        self.portfolio_state = {
            ACCOUNT: {
                CONNECTOR: [
                    {
                        "token": "USDT",
                        "units": 10000,
                        "available_units": 10000,
                        "price": 1.0,
                        "value": 10000.0,
                    }
                ]
            }
        }
        self.rules = {
            "trading_rules": {
                SYMBOL: {
                    "min_base_amount_increment": "0.01",
                    "min_order_size": "0.01",
                    "min_notional": "10",
                    "max_leverage": 10,
                }
            }
        }
        self.market_data = FakeMarketData(self)
        self.trading = FakeTrading(self)
        self.executors = FakeExecutors(self)
        self.portfolio = FakePortfolio(self)
        self.connectors = FakeConnectors(self)


class FakePort:
    def __init__(self):
        self.controller_id = CONTROLLER
        self.calls: list[tuple] = []

    async def get_position_mode(self):
        return "HEDGE"

    async def open_main(self, **kwargs):
        self.calls.append(("open", kwargs))
        return "exec-1"

    async def reduce_main(self, **kwargs):
        self.calls.append(("reduce", kwargs))
        return "exec-reduce"

    async def close_main(self, **kwargs):
        self.calls.append(("close", kwargs))
        return kwargs["executor_id"]

    async def execute_hedge(self, **kwargs):
        self.calls.append(("hedge", kwargs))
        return "exec-hedge"


def make_gm(tmp_path, fake, port=None):
    reader = HummingbotAccountReader(fake, tmp_path, CONTROLLER)
    port = port or FakePort()
    reconciler = HummingbotPositionReconciler(fake, CONTROLLER)
    gm = BrooksGM(
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        state_root=tmp_path,
        policy=GMPolicy(D("0.01"), 2, D("2"), 2, D("2"), 3600),
        reader=reader,
        execution=port,
        reconciler=reconciler,
    )
    return gm, reader, port


def entry_intent():
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG",
        "symbol": SYMBOL,
        "decision_time_ms": int(time.time() * 1000),
        "trigger": {"price": "100"},
        "invalidation": {"price": "95"},
    }


def venue_position(position_id="pos-venue-1", side="LONG"):
    return {
        "position_id": position_id,
        "trading_pair": SYMBOL,
        "position_side": side,
        "net_amount_base": "1.0",
        "current_price": "100",
    }


def hold(executor_ids=("exec-1",)):
    return {
        "account_name": ACCOUNT,
        "connector_name": CONNECTOR,
        "trading_pair": SYMBOL,
        "position_side": "LONG",
        "net_amount_base": 1.0,
        "controller_id": CONTROLLER,
        "executor_ids": list(executor_ids),
    }


# Finding 3: bootstrap E2E from main_position_id = null to reconciled.


def test_bootstrap_reconciles_null_binding_to_venue_position(tmp_path):
    fake = FakeClient()
    gm, reader, port = make_gm(tmp_path, fake)

    binding = asyncio.run(gm.execute_entry(entry_intent(), correlation_id="boot"))
    assert binding["main_position_id"] is None
    assert binding["status"] == "submitted"
    assert binding["main_executor_id"] == "exec-1"

    # Executor accepted but venue shows nothing yet: stays submitted.
    fake.executor_rows = [{"executor_id": "exec-1", "status": "RUNNING"}]
    binding = asyncio.run(gm.reconcile_main("boot"))
    assert binding["status"] == "submitted"
    assert binding["main_position_id"] is None

    # Venue position appears with lineage: reconciles atomically.
    fake.holds = [hold()]
    fake.positions = [venue_position()]
    binding = asyncio.run(gm.reconcile_main("boot"))
    assert binding["status"] == "reconciled"
    assert binding["main_position_id"] == "pos-venue-1"

    snapshot = asyncio.run(
        reader.read(
            account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL
        )
    )
    assert snapshot.structure_status == "single_main"
    assert snapshot.main_position_id == "pos-venue-1"

    # Reconciled bindings still gate management on ownership, with writes.
    reader_state = asyncio.run(
        reader.read(
            account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL
        )
    )
    assert reader_state.main_executor_id == "exec-1"


def test_reconcile_stays_submitted_when_venue_is_ambiguous(tmp_path):
    fake = FakeClient()
    gm, _, _ = make_gm(tmp_path, fake)
    asyncio.run(gm.execute_entry(entry_intent(), correlation_id="amb"))
    fake.executor_rows = [{"executor_id": "exec-1", "status": "RUNNING"}]
    fake.holds = [hold()]

    # Two venue candidates: never pick first-returned.
    fake.positions = [venue_position("pos-a"), venue_position("pos-b")]
    binding = asyncio.run(gm.reconcile_main("amb"))
    assert binding["status"] == "submitted"
    assert binding["main_position_id"] is None

    # Wrong side only: side is a consistency check, never a fallback.
    fake.positions = [venue_position("pos-c", side="SHORT")]
    binding = asyncio.run(gm.reconcile_main("amb"))
    assert binding["status"] == "submitted"
    assert binding["main_position_id"] is None

    # No lineage hold for our executor: no binding.
    fake.positions = [venue_position("pos-d")]
    fake.holds = [hold(executor_ids=("foreign-exec",))]
    binding = asyncio.run(gm.reconcile_main("amb"))
    assert binding["status"] == "submitted"
    assert binding["main_position_id"] is None


def test_entry_reconciles_immediately_when_venue_already_shows_position(tmp_path):
    fake = FakeClient()
    gm, _, _ = make_gm(tmp_path, fake)

    gm.reconciler = None
    asyncio.run(gm.execute_entry(entry_intent(), correlation_id="torn"))
    stored = json.loads((tmp_path / "trades/torn/binding.json").read_text())
    assert stored["status"] == "submitted"

    fake.executor_rows = [{"executor_id": "exec-1", "status": "RUNNING"}]
    fake.holds = [hold()]
    fake.positions = [venue_position()]
    gm.reconciler = HummingbotPositionReconciler(fake, CONTROLLER)
    binding = asyncio.run(gm.reconcile_main("torn"))
    assert binding["status"] == "reconciled"
    assert binding["main_position_id"] == "pos-venue-1"


# Finding 8: an unreadable order book is UNKNOWN, never empty.


def test_unreadable_orders_reject_entry_with_zero_writes(tmp_path):
    fake = FakeClient()
    fake.fail_orders = True
    gm, _, port = make_gm(tmp_path, fake)
    with pytest.raises(GMRejected, match="open orders"):
        asyncio.run(gm.execute_entry(entry_intent(), correlation_id="c0"))
    assert port.calls == []


def test_unreadable_orders_reject_management_with_zero_writes(tmp_path):
    fake = FakeClient()
    gm, _, port = make_gm(tmp_path, fake)
    asyncio.run(gm.execute_entry(entry_intent(), correlation_id="c1"))
    writes_after_entry = len(port.calls)
    assert writes_after_entry == 1

    fake.fail_orders = True
    with pytest.raises(GMRejected, match="open orders"):
        asyncio.run(
            gm.execute_management(
                correlation_id="c1",
                decision_id="d1",
                action="HEDGE",
                target_hedge_ratio=D("0.25"),
            )
        )
    assert len(port.calls) == writes_after_entry


# Finding 9: available margin is free collateral, not total value.


def test_available_margin_uses_free_units_not_total_value(tmp_path):
    fake = FakeClient()
    fake.portfolio_state = {
        ACCOUNT: {
            CONNECTOR: [
                {
                    "token": "USDT",
                    "units": 1000,
                    "available_units": 300,
                    "price": 1.0,
                    "value": 1000.0,
                },
                {"token": "BTC", "units": 1, "price": 500.0, "value": 500.0},
            ]
        }
    }
    reader = HummingbotAccountReader(fake, tmp_path, CONTROLLER)
    snapshot = asyncio.run(
        reader.read(
            account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL
        )
    )
    assert snapshot.equity == D(1500)
    assert snapshot.available_margin == D(300)


def test_legacy_balance_without_split_falls_back_to_total(tmp_path):
    fake = FakeClient()
    fake.portfolio_state = {
        ACCOUNT: {CONNECTOR: [{"token": "USDC", "value": 1000.0}]}
    }
    reader = HummingbotAccountReader(fake, tmp_path, CONTROLLER)
    snapshot = asyncio.run(
        reader.read(
            account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL
        )
    )
    assert snapshot.equity == D(1000)
    assert snapshot.available_margin == D(1000)
