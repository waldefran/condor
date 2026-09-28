"""Shared Wave 3 E2E harness: a realistic fake Hummingbot venue + sim execution port.

Only the Hummingbot client and the ExecutionPort are faked here. Everything
else under test is production: MarketClock, EventBus, BrooksStore,
TraderConsumer, GMConsumer, BrooksGM, HummingbotAccountReader,
HummingbotPositionReconciler, build_watcher_provider, PositionWatcher,
build_pm_load_context, PositionManager and run_role. The LLM client behind
run_role is the single mocked seam (scripted FakeLLMClient).

File name deliberately lacks the ``test_`` prefix so pytest does not collect
it; scenario files import from it.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from condor.brooks import agent_runner
from condor.brooks.adapters import (
    HummingbotAccountReader,
    HummingbotPositionReconciler,
    build_pm_load_context,
    build_watcher_provider,
)
from condor.brooks.clock import MarketClock
from condor.brooks.config import MarketWakeConfig
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.gm import BrooksGM, GMPolicy
from condor.brooks.hedge import build_hedge_state
from condor.brooks.pm import PositionManager
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import GMConsumer
from condor.brooks.trader import TraderConsumer

ACCOUNT = "acct"
CONNECTOR = "binance_perpetual"
CONTROLLER = "ctrl"
SYMBOL = "BTC-USDT"
MARK = "120"
EQUITY_USDT = 2000.0


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake Hummingbot venue (duck-typed client).
# ---------------------------------------------------------------------------


class _MarketData:
    def __init__(self, outer: FakeVenue) -> None:
        self._outer = outer

    async def get_prices(self, connector_name: str, trading_pairs: list[str]) -> dict:
        return {"prices": {SYMBOL: float(self._outer.mark_price)}}


class _Trading:
    def __init__(self, outer: FakeVenue) -> None:
        self._outer = outer

    async def get_positions(
        self, account_names=None, connector_names=None, limit=1000
    ) -> dict:
        return {"data": [dict(row) for row in self._outer.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ) -> dict:
        if self._outer.fail_orders:
            raise TimeoutError("order book unreadable")
        return {"data": [dict(row) for row in self._outer.orders]}

    async def get_position_mode(self, account_name: str, connector_name: str) -> dict:
        return {"position_mode": "HEDGE"}

    async def search_orders(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        status=None,
        limit=50,
    ) -> dict:
        self._outer.search_calls.append(
            {"trading_pairs": trading_pairs, "status": status}
        )
        rows = [dict(row) for row in self._outer.fills]
        return {"data": rows, "pagination": {"next_cursor": self._outer.fills_cursor}}


class _Executors:
    def __init__(self, outer: FakeVenue) -> None:
        self._outer = outer

    async def search_executors(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        controller_ids=None,
        limit=1000,
    ) -> dict:
        return {"data": [dict(row) for row in self._outer.executor_rows]}

    async def get_positions_summary(self, controller_id=None) -> dict:
        return {"positions": [dict(row) for row in self._outer.holds]}


class _Portfolio:
    def __init__(self, outer: FakeVenue) -> None:
        self._outer = outer

    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ) -> dict:
        return json.loads(json.dumps(self._outer.portfolio_state))


class _Connectors:
    def __init__(self, outer: FakeVenue) -> None:
        self._outer = outer

    async def get_trading_rules(self, connector_name: str, trading_pairs=None) -> dict:
        return json.loads(json.dumps(self._outer.rules))


class FakeVenue:
    """Minimal realistic venue: positions, executors, holds, orders, fills."""

    def __init__(self) -> None:
        self.mark_price = MARK
        self.positions: list[dict] = []
        self.executor_rows: list[dict] = []
        self.holds: list[dict] = []
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.fills_cursor = "cur-0"
        self.search_calls: list[dict] = []
        self.fail_orders = False
        self.portfolio_state = {
            ACCOUNT: {
                CONNECTOR: [
                    {
                        "token": "USDT",
                        "units": EQUITY_USDT,
                        "available_units": EQUITY_USDT,
                        "price": 1.0,
                        "value": EQUITY_USDT,
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
        self.market_data = _MarketData(self)
        self.trading = _Trading(self)
        self.executors = _Executors(self)
        self.portfolio = _Portfolio(self)
        self.connectors = _Connectors(self)

    def bump_fills_cursor(self) -> None:
        self.fills_cursor = f"cur-{len(self.fills)}"


# ---------------------------------------------------------------------------
# Simulated ExecutionPort: venue-faithful writes into the FakeVenue.
# ---------------------------------------------------------------------------


class SimPort:
    """Fake ExecutionPort whose writes mutate the venue like fills would.

    MAIN opens are deferred: ``open_main`` registers the executor only; the
    venue position + lineage hold appear on ``commit_main`` so tests can drive
    the submitted -> reconciled transition. Hedge writes apply immediately.
    """

    def __init__(self, venue: FakeVenue) -> None:
        self.venue = venue
        self.controller_id = CONTROLLER
        self.calls: list[tuple] = []
        self._exec_seq = 0
        self._pos_seq = 0
        self._pending_main: dict | None = None
        self.hedge_position_id = "hpos-1"

    def _next_exec(self) -> str:
        self._exec_seq += 1
        return f"exec-{self._exec_seq}"

    async def get_position_mode(self) -> str:
        return "HEDGE"

    async def open_main(self, **kwargs: Any) -> str:
        self.calls.append(("open", kwargs))
        exec_id = self._next_exec()
        self.venue.executor_rows.append(
            {
                "executor_id": exec_id,
                "status": "RUNNING",
                "trading_pair": kwargs["symbol"],
            }
        )
        self._pending_main = {"executor_id": exec_id, **kwargs}
        return exec_id

    def commit_main(self) -> str:
        """Let the venue show the accepted MAIN executor (position + lineage)."""
        assert self._pending_main is not None, "no pending MAIN to commit"
        pending = self._pending_main
        self._pending_main = None
        self._pos_seq += 1
        pos_id = f"pos-{self._pos_seq}"
        side = pending["side"]
        qty = str(pending["quantity"])
        self.venue.positions.append(
            {
                "position_id": pos_id,
                "trading_pair": pending["symbol"],
                "position_side": side,
                "net_amount_base": qty,
                "current_price": self.venue.mark_price,
            }
        )
        self.venue.holds.append(
            {
                "account_name": ACCOUNT,
                "connector_name": CONNECTOR,
                "trading_pair": pending["symbol"],
                "position_side": side,
                "net_amount_base": float(qty),
                "controller_id": CONTROLLER,
                "executor_ids": [pending["executor_id"]],
            }
        )
        self._add_fill(pending["symbol"], qty, pending["executor_id"])
        return pos_id

    def _add_fill(self, symbol: str, qty: str, exec_id: str) -> None:
        self.venue.fills.append(
            {
                "client_order_id": f"fill-{exec_id}",
                "trading_pair": symbol,
                "status": "FILLED",
                "filled_amount": str(qty),
                "price": self.venue.mark_price,
            }
        )
        self.venue.bump_fills_cursor()

    def _main_row(self) -> dict:
        for row in self.venue.positions:
            if row["position_id"].startswith("pos-"):
                return row
        raise AssertionError("no MAIN venue position")

    def _hedge_row(self) -> dict | None:
        for row in self.venue.positions:
            if row["position_id"] == self.hedge_position_id:
                return row
        return None

    async def reduce_main(self, **kwargs: Any) -> str:
        self.calls.append(("reduce", kwargs))
        row = self._main_row()
        row["net_amount_base"] = str(D(row["net_amount_base"]) - D(kwargs["quantity"]))
        exec_id = self._next_exec()
        self.venue.executor_rows.append(
            {
                "executor_id": exec_id,
                "status": "RUNNING",
                "trading_pair": kwargs["symbol"],
            }
        )
        self._add_fill(kwargs["symbol"], str(kwargs["quantity"]), exec_id)
        return exec_id

    async def close_main(self, **kwargs: Any) -> str:
        self.calls.append(("close", kwargs))
        exec_id = kwargs["executor_id"]
        self.venue.positions = [
            row
            for row in self.venue.positions
            if not row["position_id"].startswith("pos-")
        ]
        self.venue.executor_rows.append({"executor_id": exec_id, "status": "CLOSED"})
        return exec_id

    async def execute_hedge(self, **kwargs: Any) -> str:
        self.calls.append(("hedge", kwargs))
        qty = D(kwargs["quantity"])
        row = self._hedge_row()
        if kwargs["position_action"] == "OPEN":
            hedge_side = "SHORT" if kwargs["side"] == "SELL" else "LONG"
            if row is None:
                self.venue.positions.append(
                    {
                        "position_id": self.hedge_position_id,
                        "trading_pair": kwargs["symbol"],
                        "position_side": hedge_side,
                        "net_amount_base": str(qty),
                        "current_price": self.venue.mark_price,
                    }
                )
            else:
                row["net_amount_base"] = str(D(row["net_amount_base"]) + qty)
        else:
            assert row is not None, "CLOSE without a hedge leg"
            remaining = D(row["net_amount_base"]) - qty
            if remaining <= 0:
                self.venue.positions = [
                    item for item in self.venue.positions if item is not row
                ]
            else:
                row["net_amount_base"] = str(remaining)
        exec_id = self._next_exec()
        self.venue.executor_rows.append(
            {
                "executor_id": exec_id,
                "status": "RUNNING",
                "trading_pair": kwargs["symbol"],
            }
        )
        self.venue.holds.append(
            {
                "account_name": ACCOUNT,
                "connector_name": CONNECTOR,
                "trading_pair": kwargs["symbol"],
                "controller_id": CONTROLLER,
                "executor_ids": [exec_id],
            }
        )
        self._add_fill(kwargs["symbol"], str(qty), exec_id)
        return exec_id

    def write_kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


# ---------------------------------------------------------------------------
# Deterministic closed-bar candles + entry intent referencing them.
# ---------------------------------------------------------------------------


_INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


class FakeCandles:
    """Closed-bar history ending exactly at ``due`` for every timeframe."""

    def __init__(self, due_ms: int) -> None:
        self.due_ms = due_ms

    def _bars(self, timeframe: str, limit: int) -> list[dict]:
        interval = _INTERVAL_MS[timeframe]
        bars = []
        for index in range(limit):
            opened = self.due_ms - (limit - index) * interval + 1
            bar = {
                "open_time_ms": opened,
                "close_time_ms": opened + interval - 1,
                "open": "110",
                "high": "112",
                "low": "108",
                "close": "111",
                "closed": True,
            }
            bars.append(bar)
        if timeframe == "15m":
            # Trigger bar: the cited M15 close the entry references.
            bars[-1].update(
                {"open": "110", "high": "120", "low": "100", "close": "115"}
            )
        return bars

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict]:
        assert symbol == SYMBOL, f"harness only serves {SYMBOL}"
        return self._bars(timeframe, limit)


def h1_due_now() -> int:
    now_ms = time.time_ns() // 1_000_000
    return (now_ms // 3_600_000) * 3_600_000 - 1


def m15_trigger_source(due_ms: int) -> dict:
    return {
        "timeframe": "M15",
        "bar_index": 119,
        "open_time_ms": due_ms - 900_000 + 1,
        "close_time_ms": due_ms,
    }


def make_entry_intent(due_ms: int, decision: str = "ENTER_LONG") -> dict:
    source = m15_trigger_source(due_ms)
    direction = "above" if decision == "ENTER_LONG" else "below"
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": decision,
        "symbol": SYMBOL,
        "decision_time_ms": due_ms,
        "market_context": None,
        "setup": {
            "type": "breakout",
            "trigger_status": "present",
            "signal_quality": "clear",
            "location_assessment": "favorable",
        },
        "decision_timeframe": "M15",
        "context_timeframes_used": ["H4", "H1", "M15"],
        "entry_mechanism": "breakout",
        "trigger": {
            "kind": "stop",
            "direction": direction,
            "reference": "bar high",
            "price_field": "high",
            "price": "120",
            "source": source,
        },
        "invalidation": {
            "reference": "bar low",
            "price_field": "low",
            "price": "100",
            "source": source,
        },
        "evidence_for": ["breakout"],
        "evidence_against": ["resistance"],
        "qualitative_confidence": "medium",
        "uncertainty": ["continuation"],
        "conditions_that_change_market_read": ["breakdown"],
    }


# ---------------------------------------------------------------------------
# Mocked LLM client (the single allowed mock: run_role boundary).
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Scripted stand-in for the LLM client; real run_role drives it."""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.prompts: list[str] = []
        self.started = False
        self.stopped = False
        self.working_dir: str | None = None

    async def start(self) -> None:
        self.started = True

    async def prompt(self, turn: str) -> str:
        self.prompts.append(turn)
        assert self._script, "LLM script exhausted"
        return self._script.pop(0)

    async def stop(self) -> None:
        self.stopped = True


def script_llm(monkeypatch: Any, script: list[str]) -> FakeLLMClient:
    fake = FakeLLMClient(script)
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: fake)
    return fake


# ---------------------------------------------------------------------------
# ManagementDecisionV2 payloads for the mocked PM.
# ---------------------------------------------------------------------------


def _evidence() -> dict:
    return {
        "observations": ["Venue MAIN leg matches the persisted binding."],
        "evidence_for": ["No unmet protection or margin condition."],
        "evidence_against": ["A fresh venue move could change the read."],
    }


def _risk() -> dict:
    return {
        "exposure_before": [f"{SYMBOL} LONG 1"],
        "exposure_after": [f"{SYMBOL} LONG 1"],
        "protection_status": "adequate",
        "costs_considered": ["Fees and funding remain applicable."],
        "uncertainty": "low",
    }


def _decision_base(action: str, t_ms: int, main_id: str) -> dict:
    return {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": t_ms,
        "action": action,
        "position_ids": [main_id],
        "reason": f"E2E scripted {action}.",
        "evidence": _evidence(),
        "risk": _risk(),
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": None,
        "market_analysis_request": None,
        "conditions_that_change_action": [
            "A fill or quantity mismatch requires reconciliation."
        ],
    }


def hold_decision(t_ms: int, main_id: str) -> dict:
    return _decision_base("HOLD", t_ms, main_id)


def reduce_decision(t_ms: int, main_id: str, fraction: str = "0.5") -> dict:
    decision = _decision_base("REDUCE", t_ms, main_id)
    decision["reduce_fraction"] = fraction
    return decision


def hedge_decision(
    t_ms: int,
    main_id: str,
    action: str,
    target: str,
    hedge_id: str | None,
) -> dict:
    decision = _decision_base(action, t_ms, main_id)
    decision["hedge_plan"] = {
        "objective": f"E2E {action} to {target}.",
        "target_hedge_ratio": target,
        "main_position_id": main_id,
        "hedge_position_id": hedge_id,
        "ratio_basis": "absolute_mark_notional",
        "expected_effect_on_exposure": "Net exposure falls to the target ratio.",
        "costs": ["Fees and funding remain applicable."],
        "unlock_condition": "Target reached.",
        "failure_condition": "Venue state diverges.",
    }
    return decision


# ---------------------------------------------------------------------------
# Whole world: real store/bus/GM/consumers/watcher/PM over the fake venue.
# ---------------------------------------------------------------------------


async def _exploding_candles(symbol: str, timeframe: str, limit: int) -> list:
    raise AssertionError("no PM market-tool read expected on this path")


class E2EWorld:
    def __init__(self, root: Path | str, venue: FakeVenue | None = None) -> None:
        self.root = Path(root)
        self.store = BrooksStore(self.root)
        self.bus = EventBus(self.store)
        self.venue = venue or FakeVenue()
        self.port = SimPort(self.venue)
        self.reader = HummingbotAccountReader(self.venue, self.root, CONTROLLER)
        self.reconciler = HummingbotPositionReconciler(self.venue, CONTROLLER)
        self.gm = BrooksGM(
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            state_root=self.root,
            policy=GMPolicy(D("0.01"), 2, D("2"), 2, D("2"), 3600),
            reader=self.reader,
            execution=self.port,
            reconciler=self.reconciler,
        )
        self.gm_consumer = GMConsumer(
            gm_factory=lambda symbol: self.gm, publish=self.bus
        )
        self.pm_now: int = 0
        self.pm_load = build_pm_load_context(
            self.venue,
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            controller_id=CONTROLLER,
            state_root=self.root,
            now_fn=lambda: self.pm_now,
        )
        self.saved_pm: list[tuple] = []
        self.pm_audits: list[tuple] = []

        async def _save(correlation_id: str, decision: Any) -> None:
            self.saved_pm.append((correlation_id, decision))

        async def _record(correlation_id: str, record: dict) -> None:
            self.pm_audits.append((correlation_id, record))

        self.pm = PositionManager(
            runner=None,
            load_context=self.pm_load,
            save_decision=_save,
            publish=self.bus,
            candle_source=_exploding_candles,
            record_market_read=_record,
            list_active_correlations=self._active_correlations,
            agent_key="e2e-key",
        )

    async def _active_correlations(self, symbol: Any) -> list[str]:
        """Binding scan over the GM root (what the supervisor default should do)."""
        trades = self.root / "trades"
        if not trades.exists():
            return []
        found = []
        for binding_path in sorted(trades.glob("*/binding.json")):
            try:
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(binding, dict):
                continue
            if symbol not in (None, "", "*") and binding.get("symbol") != symbol:
                continue
            found.append(binding_path.parent.name)
        return found

    def clock(self, candles: FakeCandles) -> MarketClock:
        return MarketClock(
            symbols=[SYMBOL],
            trader=MarketWakeConfig(timeframe="1h", wake_offset_sec=0),
            htf=MarketWakeConfig(timeframe="1d", wake_offset_sec=0),
            source=candles,
            publish=self.bus,
        )

    def trader(self, candles: FakeCandles, shadow_mode: bool) -> TraderConsumer:
        return TraderConsumer(
            agent_key="e2e-key",
            source=candles,
            store=self.store,
            events=self.bus,
            shadow_mode=shadow_mode,
        )

    def watcher(self) -> PositionWatcher:
        provider = build_watcher_provider(
            self.venue,
            account_name=ACCOUNT,
            connector_name=CONNECTOR,
            controller_id=CONTROLLER,
            symbols=[SYMBOL],
            state_root=self.root,
        )
        return PositionWatcher(provider, self.bus)

    def drain(self, queue: Any) -> list[BrooksEvent]:
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items


def open_main_position(world: E2EWorld, cid: str, due_ms: int) -> dict:
    """Full MAIN open: entry (submitted, null id) -> commit -> reconciled."""
    binding = run(world.gm.execute_entry(make_entry_intent(due_ms), correlation_id=cid))
    assert binding["main_position_id"] is None
    assert binding["status"] == "submitted"
    world.port.commit_main()
    return run(world.gm.reconcile_main(cid))


def expected_from_reader(world: E2EWorld) -> Any:
    """HedgeState a GM hedge call needs, built from a fresh production read."""
    snapshot = run(
        world.reader.read(account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL)
    )
    legs = list(snapshot.positions or [])
    mains = [leg for leg in legs if leg.ownership_role == "MAIN"]
    hedges = [leg for leg in legs if leg.ownership_role == "HEDGE"]
    return build_hedge_state(
        legs,
        main_position_id=mains[0].position_id if mains else None,
        hedge_position_id=hedges[0].position_id if hedges else None,
        # Strictly older than the GM's own fresh read, same fingerprint.
        as_of_ms=snapshot.as_of_ms - 1,
    )


def seed_reconciled_hedge(world: E2EWorld, cid: str, hedge_qty: str) -> dict:
    """Stand in for the not-yet-implemented hedge discovery reconciler.

    Creates the venue hedge leg, binds its id on the persisted binding, and
    persists the matching hedge_state.json so later INCREASE/REDUCE/REMOVE
    calls compile against the production reader. Pinned as G AP evidence in
    test_brooks_e2e_hedge.py::test_hedge_first_open_confirmation_gap.
    """
    world.port.calls.append(("seed-hedge", {"quantity": hedge_qty}))
    world.venue.positions.append(
        {
            "position_id": world.port.hedge_position_id,
            "trading_pair": SYMBOL,
            "position_side": "SHORT",
            "net_amount_base": str(hedge_qty),
            "current_price": world.venue.mark_price,
        }
    )
    binding_path = world.root / "trades" / cid / "binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["hedge_position_id"] = world.port.hedge_position_id
    binding["hedge_executor_id"] = "exec-seed-hedge"
    binding["hedge_size"] = str(hedge_qty)
    binding_path.write_text(json.dumps(binding, sort_keys=True))
    expected = expected_from_reader(world)
    (world.root / "trades" / cid / "hedge_state.json").write_text(
        json.dumps(
            {
                "schema": expected.schema,
                "structure_status": expected.structure_status,
                "unresolved": expected.unresolved,
                "main_position_id": expected.main_position_id,
                "hedge_position_id": expected.hedge_position_id,
                "symbol": expected.symbol,
                "main_side": expected.main_side,
                "hedge_side": expected.hedge_side,
                "main_size": expected.main_size,
                "hedge_size": expected.hedge_size,
                "hedge_ratio": expected.hedge_ratio,
                "ratio_basis": expected.ratio_basis,
                "net_exposure_usd": expected.net_exposure_usd,
                "gross_exposure_usd": expected.gross_exposure_usd,
                "main_mark_notional": expected.main_mark_notional,
                "hedge_mark_notional": expected.hedge_mark_notional,
                "mark_price": expected.mark_price,
                "hedge_mark_price": expected.hedge_mark_price,
                "as_of_ms": expected.as_of_ms,
                "fingerprint": expected.fingerprint,
            },
            sort_keys=True,
        )
    )
    return binding


def read_binding(world: E2EWorld, cid: str) -> dict:
    return json.loads((world.root / "trades" / cid / "binding.json").read_text())
