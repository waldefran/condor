#!/usr/bin/env python3
"""Historical Brooks execution using real role consumers and API model calls.

Only the clock, historical candle source and execution venue are simulated.
No production module is changed, and no live execution client is constructed.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import logging
import math
from pathlib import Path
import signal
import shutil
import sys
import time
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx
from dotenv import load_dotenv

from condor.brooks import agent_runner, gm as gm_module
from condor.brooks.adapters import build_watcher_provider, read_bindings
from condor.brooks.clock import MarketClock
from condor.brooks.config import BrooksConfig, MarketWakeConfig
from condor.brooks.decision_cycles import DecisionCycleStore
from condor.brooks.contracts import MarketContextV2, TradeIntentV2
from condor.brooks.events import BrooksEvent, EventBus, EventType
from condor.brooks.gm import BrooksGM, GMPolicy
from condor.brooks.htf_analyst import ContextAnalystConsumer
from condor.brooks.pm import PM_WAKE_EVENTS, PositionManager
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.store import BrooksStore
from condor.brooks.supervisor import GMConsumer
from condor.brooks.trader import TraderConsumer
from scripts.brooks_walkforward_data import FrozenHistoricalSource
from scripts.brooks_walkforward_report import render_report
from scripts.brooks_walkforward_simulation import WalkForwardVenueAdapter
from scripts.brooks_pending_entry import PendingStopState, process_pending_stop

HOUR = 3_600_000
MINUTE = 60_000
DAY = 24 * HOUR
ACTIVE_CAPTURE: ContextVar[dict | None] = ContextVar("walkforward_capture", default=None)


def utc_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def append_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        stream.flush()


class HistoricalBus(EventBus):
    def __init__(self, store: BrooksStore, clock):
        super().__init__(store)
        self.clock = clock

    async def publish(self, event: BrooksEvent):
        return await super().publish(replace(event, created_at_ms=self.clock()))


class CapturedClient:
    def __init__(self, inner, record: dict, runner):
        self.inner, self.record, self.runner = inner, record, runner

    def __getattr__(self, key):
        return getattr(self.inner, key)

    @property
    def working_dir(self):
        return getattr(self.inner, "working_dir", None)

    @working_dir.setter
    def working_dir(self, value):
        self.inner.working_dir = value

    async def start(self):
        await self.inner.start()

    async def stop(self):
        await self.inner.stop()

    async def prompt(self, message):
        if message.startswith("Read tool ") and self.record.get("tools"):
            self.record["tools"][-1]["response_message"] = message
            status = "completed"
            try:
                value = json.loads(message.split(" result: ", 1)[1].rsplit("\nContinue.", 1)[0])
                if isinstance(value, dict) and "error" in value:
                    status = "error"
            except (ValueError, IndexError):
                status = "returned"
            self.record["tools"][-1]["status"] = status
        call = {"user_message": message, "started_at_ms": time.time_ns() // 1_000_000,
                "simulation_started_at_ms": self.runner.now, "wire_requests": []}
        self.record["calls"].append(call)
        self.runner.persist_capture(self.record)
        begun = time.monotonic()
        initial_sim_time = self.runner.now
        try:
            response = await self.inner.prompt(message)
            call["raw_response"] = response
            try:
                parsed = json.loads(response)
                if isinstance(parsed, dict) and "tool" in parsed:
                    self.record.setdefault("tools", []).append({"name": parsed["tool"],
                        "arguments": parsed.get("arguments"), "status": "requested"})
            except (TypeError, ValueError):
                pass
            return response
        except BaseException as exc:
            call["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            raise
        finally:
            elapsed = max(0, int((time.monotonic() - begun) * 1000))
            call["finished_at_ms"] = time.time_ns() // 1_000_000
            call["elapsed_ms"] = elapsed
            await self.runner.advance_to(initial_sim_time + elapsed)
            call["simulation_finished_at_ms"] = self.runner.now
            self.runner.persist_capture(self.record)


class WalkForward:
    def __init__(self, args):
        self.args = args
        self.root = args.output.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.start, self.end = utc_ms(args.start), utc_ms(args.end)
        if self.start % HOUR or self.end % HOUR or self.end <= self.start:
            raise ValueError("Replay boundaries must be ascending absolute UTC hours")
        self.symbol = args.symbol
        self.recorded_run = getattr(args, "recorded_run", None)
        self.only_trade = getattr(args, "only_trade", None)
        self.long_lock_policy = getattr(args, "long_lock_policy", False)
        self.max_unhedged_loss_r = getattr(args, "max_unhedged_loss_r", None)
        if self.max_unhedged_loss_r is not None and not self.long_lock_policy:
            raise ValueError("Unhedged loss ceiling requires --long-lock-policy")
        pm_timeout = getattr(args, "pm_timeout_sec", 60)
        if not math.isfinite(pm_timeout) or pm_timeout <= 0:
            raise ValueError("PM timeout must be finite and positive")
        if self.long_lock_policy and (self.recorded_run is None or not self.only_trade):
            raise ValueError("Long lock experiment requires --recorded-run and --only-trade")
        self.pending_entries: dict[str, PendingStopState] = {}
        self.pending_events: dict[str, dict[str, Any]] = {}
        if self.recorded_run is not None:
            self.recorded_run = self.recorded_run.resolve()
        self.now = self.start - 10 * MINUTE
        self.stop = asyncio.Event()
        self._advance_lock = asyncio.Lock()
        self.source = FrozenHistoricalSource.from_directory(args.dataset, symbol=self.symbol, decision_ms=self.end - 1)
        self.minute_bars = [json.loads(line) for line in (args.dataset / f"{self.symbol.replace('-', '_')}_1m.jsonl").read_text().splitlines()]
        self.minute_index = 0
        self.store = BrooksStore(args.state)
        self.events = HistoricalBus(self.store, lambda: self.now)
        self.event_queue = self.events.subscribe()
        self.venue = WalkForwardVenueAdapter(
            self.symbol, state_root=self.store.root, initial_equity=args.initial_equity,
            taker_fee_rate=args.fee_rate, slippage_bps=args.slippage_bps,
            amount_step="0.001", min_amount="0.001", min_notional="5", max_leverage=5,
            account_name="walkforward", connector_name="binance_perpetual_demo",
            controller_id="brooks-walkforward-10d", now_fn=lambda: self.now,
            candle_source=self.source,
            long_lock_policy=self.long_lock_policy,
            max_unhedged_loss_r=self.max_unhedged_loss_r,
            on_hedge_write=self.observe_hedge_write,
        )
        known_bar = next(bar for bar in reversed(self.minute_bars) if bar["close_time_ms"] <= self.now)
        self.venue.set_market(known_bar, decision_time_ms=self.now)
        self.policy = GMPolicy(risk_per_trade_pct=Decimal("0.0005"), max_positions=1,
            max_gross_exposure_pct=Decimal("1"), leverage=5, take_profit_r=Decimal("2"),
            time_limit_sec=86400, max_trigger_drift_pct=Decimal("0.02"),
            max_snapshot_age_ms=60000, max_intent_age_ms=7200000)
        self.gm = BrooksGM(account_name=self.venue.account_name, connector_name=self.venue.connector_name,
            state_root=self.store.root, policy=self.policy, reader=self.venue.reader,
            execution=self.venue.execution, reconciler=self.venue.reconciler)
        self.gm_consumer = GMConsumer(gm_factory=lambda symbol: self.gm, publish=self.events,
            store=self.store, agent_key=args.agent_key, candle_source=self.source, now_fn=lambda: self.now)
        self.trader = TraderConsumer(args.agent_key, self.source, self.store, self.events, shadow_mode=False)
        self.contexts = {tf: ContextAnalystConsumer(args.agent_key, self.source, self.store, self.events, timeframe=tf)
                         for tf in ("1d", "4h")}
        self.pm_interval_sec = BrooksConfig().pm.frequency_sec
        self.pm = PositionManager(load_context=self.load_pm_context, save_decision=self.save_pm,
            publish=self.events, candle_source=self.source, record_market_read=self.record_pm_read,
            list_active_correlations=self.active_correlations, agent_key=args.agent_key,
            timeout_sec=getattr(args, "pm_timeout_sec", 60))
        self.watcher = PositionWatcher(
            build_watcher_provider(
                self.venue.client,
                account_name=self.venue.account_name,
                connector_name=self.venue.connector_name,
                controller_id=self.venue.controller_id,
                symbols=[self.symbol],
                state_root=self.store.root,
            ),
            self.events,
            on_snapshot=self.gm_consumer.reconcile_bound_snapshot,
        )
        self.clock = MarketClock(symbols=[self.symbol], source=self.source, publish=self.events,
            trader=MarketWakeConfig(timeframe="1h", wake_offset_sec=2),
            htf=MarketWakeConfig(timeframe="1d", wake_offset_sec=3),
            h4=MarketWakeConfig(timeframe="4h", wake_offset_sec=3))
        self.agenda = []
        self.sequence = 0
        if self.recorded_run is None:
            for boundary in range(self.start, self.end, HOUR):
                self.enqueue(boundary + 2000, 0, "clock", timeframe="1h", decision=boundary-1)
                if boundary % (4*HOUR) == 0:
                    self.enqueue(boundary + 3000, 10, "clock", timeframe="4h", decision=boundary-1)
                if boundary % DAY == 0:
                    self.enqueue(boundary + 3000, 10, "clock", timeframe="1d", decision=boundary-1)
        else:
            self._schedule_recorded_outputs()
        pm_interval_ms = self.pm_interval_sec * 1000
        for boundary in range(self.start + pm_interval_ms, self.end, pm_interval_ms):
            self.enqueue(boundary, 20, "timer")
        self.current_round = "bootstrap"
        self.gm_results: dict[str, Any] = {}
        self.manifest = {"status": "running", "symbol": self.symbol, "model": args.agent_key,
            "start_ms": self.start, "end_ms": self.end, "start_utc": args.start, "end_exclusive_utc": args.end,
            "expected_trader_cycles": (self.end-self.start)//HOUR,
            "started_at_ms": time.time_ns()//1_000_000, "git_head": args.git_head,
            "script_sha256": {name: hashlib.sha256((REPO / "scripts" / name).read_bytes()).hexdigest()
                for name in ("brooks_walkforward.py", "brooks_walkforward_data.py", "brooks_walkforward_simulation.py", "brooks_walkforward_report.py")},
            "method": "historical replay with actual production consumers and real PydanticAI API",
            "assumptions": {"initial_equity_usdt": str(args.initial_equity), "taker_fee_rate": args.fee_rate,
                "adverse_slippage_bps": args.slippage_bps, "funding": "not modeled (zero); net results exclude funding",
                "execution": "accepted entries use existing GM/ExecutionPort MARKET semantics at latest known M1 close plus adverse slippage; pending stop entries are rejected before write, not filled as MARKET or submitted as resting stops",
                "barriers": "subsequent full M1 bars; stop first if both barriers touched; partial entry minute excluded",
                "latency": "actual model response elapsed time advances simulation; raw role inputs remain frozen",
                "risk_policy": {k: str(v) for k,v in vars(self.policy).items()},
                "pm_interval_sec": self.pm_interval_sec, "pm_timeout_sec": self.pm.timeout_sec,
                "trader_timeout_sec": self.trader.timeout_sec, "context_timeout_sec": 300,
                "venue_rules": "simulation assumptions: ETH step/min 0.001, minimum notional 5, leverage 5",
                "evaluation": "retrospective replay, not a prospective out-forward; no parameter optimization"}}
        if self.recorded_run is not None:
            source_manifest = json.loads((self.recorded_run / "run_manifest.json").read_text())
            self.manifest.update(method="recorded Trader and Context Analyst outputs, real PM PydanticAI API and deterministic GM",
                recorded_source={"path": str(self.recorded_run), "model": source_manifest.get("model"),
                    "cycles_sha256": hashlib.sha256((self.recorded_run / "cycles.jsonl").read_bytes()).hexdigest(),
                    "manifest_sha256": hashlib.sha256((self.recorded_run / "run_manifest.json").read_bytes()).hexdigest()},
                source_trader_cycles=sum(e["kind"] == "recorded_trader" for e in self.agenda),
                source_context_updates=sum(e["kind"] == "recorded_context" for e in self.agenda))
            self.manifest["expected_trader_cycles"] = self.manifest["source_trader_cycles"]
            self.manifest["script_sha256"]["brooks_pending_entry.py"] = hashlib.sha256(
                (REPO / "scripts/brooks_pending_entry.py").read_bytes()).hexdigest()
            self.manifest["assumptions"]["latency"] = "source decisions and contexts become available at their original simulation completion times; PM API latency advances simulation"
            self.manifest["assumptions"]["execution"] = (
                "recorded pending stops activate only on a complete M1 candle opened after intent availability, "
                "with strict crossing and close beyond trigger; invalidation wins same-bar ambiguity; "
                "MARKET at observed M1 close plus adverse slippage after existing GM validation; "
                "original intent is preserved, expiry uses existing max_intent_age_ms"
            )
            self.manifest["assumptions"]["pending_cancellation"] = (
                "invalidation, expiry or a newer same-symbol ENTER cancels a pending candidate; "
                "NO_TRADE does not cancel an earlier candidate"
            )
        if self.only_trade:
            self.manifest["only_trade"] = self.only_trade
        if self.long_lock_policy:
            self.manifest["script_sha256"]["brooks_long_lock_policy.py"] = hashlib.sha256(
                (REPO / "scripts/brooks_long_lock_policy.py").read_bytes()).hexdigest()
            self.manifest["assumptions"]["long_exit_policy"] = (
                "lock_and_wait_nonnegative_net: no automatic LONG stop or time-limit exit; " +
                ("observed closed M1 combined net loss at 5 original R requests full hedge through existing GM; "
                 if self.max_unhedged_loss_r == "5" else
                 "observed closed M1 stop touch requests full hedge through existing GM; ") +
                "MAIN close/reduction blocked if projected MAIN+HEDGE liquidation net is negative; "
                "SHORT MAIN barriers unchanged; management horizon is finite"
            )
            self.manifest["assumptions"]["max_unhedged_loss_r"] = self.max_unhedged_loss_r
        checkpoint = self.args.state / "walkforward_checkpoint.json"
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text())
            if state["git_head"] != args.git_head or state["start_ms"] != self.start or state["end_ms"] != self.end:
                raise ValueError("Checkpoint identity differs from this replay")
            if (state.get("only_trade") != self.only_trade
                or state.get("long_lock_policy", False) != self.long_lock_policy):
                raise ValueError("Checkpoint selected trade or exit policy differs")
            if state.get("max_unhedged_loss_r") != self.max_unhedged_loss_r:
                raise ValueError("Checkpoint unhedged R limit differs")
            self.now, self.minute_index = state["now"], state["minute_index"]
            self.agenda, self.sequence = state["agenda"], state["sequence"]
            self.manifest = state["manifest"]
            self.venue.load_checkpoint(self.args.state / "venue_checkpoint.json")
            self.watcher = PositionWatcher(
                self.watcher.get_bound_snapshots,
                self.events,
                initial_snapshots=state.get("watcher_snapshots", []),
                on_snapshot=self.gm_consumer.reconcile_bound_snapshot,
            )
            self.gm_results = state.get("gm_results", {})
            self.pending_entries = {cid: PendingStopState.model_validate(value)
                for cid, value in state.get("pending_entries", {}).items()}
            self.pending_events = state.get("pending_events", {})

    def _schedule_recorded_outputs(self):
        """Ingest accepted source role outputs at their historical completion times."""
        source = self.recorded_run
        assert source is not None
        cycles_file = source / "cycles.jsonl"
        if not cycles_file.is_file():
            raise ValueError(f"Recorded source lacks cycles.jsonl: {source}")
        for line_no, line in enumerate(cycles_file.read_text().splitlines(), 1):
            row = json.loads(line)
            if getattr(self, "only_trade", None) and row["correlation_id"] != self.only_trade:
                continue
            decision = row["decision_time_ms"]
            due = row["simulation_completed_at_ms"]
            if not (self.start - HOUR <= decision < self.end and due < self.end):
                continue
            if due < decision or row["symbol"] != self.symbol:
                raise ValueError(f"Invalid recorded Trader timing/symbol at source line {line_no}")
            intent = row.get("intent")
            if row.get("status") == "completed":
                if intent is None:
                    raise ValueError(f"Completed source cycle lacks intent at line {line_no}")
                validated = TradeIntentV2.model_validate(intent)
                if validated.decision_time_ms != decision or validated.symbol != self.symbol:
                    raise ValueError(f"Source intent identity mismatch at line {line_no}")
            elif intent is not None:
                raise ValueError(f"Failed source cycle contains intent at line {line_no}")
            packet_file = row.get("frozen_packet_file")
            if not packet_file or not (source / packet_file).is_file():
                raise ValueError(f"Source cycle lacks frozen packet at line {line_no}")
            self.enqueue(due, 0, "recorded_trader", source_line=line_no,
                decision=decision)
        if getattr(self, "only_trade", None) and not any(
            item["kind"] == "recorded_trader" for item in self.agenda
        ):
            raise ValueError("Selected trade is not available in this replay window")
        for source_file in sorted((source / "role_runs").glob("*.json")):
            capture = json.loads(source_file.read_text())
            if capture.get("role") != "CONTEXT_ANALYST" or capture.get("host", {}).get("accepted") is not True:
                continue
            final = None
            for call in capture.get("calls", []):
                raw = call.get("raw_response")
                if raw is None:
                    continue
                try:
                    value = MarketContextV2.model_validate_json(raw)
                except (ValueError, TypeError):
                    continue
                final = (value, call.get("simulation_finished_at_ms"))
            if final is None:
                raise ValueError(f"Accepted analyst capture lacks valid final output: {source_file}")
            context, due = final
            if not isinstance(due, int) or due < context.decision_time_ms:
                raise ValueError(f"Invalid analyst output timing: {source_file}")
            if context.symbol != self.symbol:
                raise ValueError(f"Analyst source symbol mismatch: {source_file}")
            if due < self.end and context.decision_time_ms < self.end:
                self.enqueue(due, 0, "recorded_context", source_file=source_file.name,
                    decision=context.decision_time_ms)

    def _copy_recorded_artifacts(self):
        source = self.recorded_run
        assert source is not None
        destination = self.root / "source_artifacts"
        for name in ("cycles.jsonl", "run_manifest.json"):
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, destination / name)
        for name in ("role_runs", "frozen_packets", "wire_requests"):
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True)

    async def _apply_recorded(self, item, *, drain_events=True):
        source = self.recorded_run
        assert source is not None
        if item["kind"] == "recorded_context":
            capture = json.loads((source / "role_runs" / item["source_file"]).read_text())
            final = None
            for call in capture["calls"]:
                try:
                    candidate = MarketContextV2.model_validate_json(call["raw_response"])
                except (KeyError, ValueError, TypeError):
                    continue
                final = (candidate, call["simulation_finished_at_ms"])
            if final is None or final[1] != item["due"] or final[0].decision_time_ms != item["decision"]:
                raise ValueError("Recorded analyst output changed after scheduling")
            context = final[0]
            self.store.save_market_context(context)
            await self.events.publish(BrooksEvent(EventType.MARKET_CONTEXT_UPDATED, self.symbol,
                {"timeframe": context.timeframe, "market_context": context.model_dump(mode="json"),
                    "recorded_source_file": f"source_artifacts/role_runs/{item['source_file']}"},
                correlation_id=f"recorded-{context.timeframe}-{context.decision_time_ms}"))
            if drain_events:
                await self.drain()
            return
        lines = (source / "cycles.jsonl").read_text().splitlines()
        row = json.loads(lines[item["source_line"] - 1])
        if row["simulation_completed_at_ms"] != item["due"] or row["decision_time_ms"] != item["decision"]:
            raise ValueError("Recorded Trader output changed after scheduling")
        intent = row.get("intent")
        if intent is not None:
            validated = TradeIntentV2.model_validate(intent)
            self.store.save_trader_intent(validated.model_dump(mode="json"))
            event = BrooksEvent(EventType.TRADER_INTENT_CREATED, self.symbol,
                {"intent": validated.model_dump(mode="json"), "shadow_mode": False,
                    "recorded_source_line": item["source_line"]},
                correlation_id=row["correlation_id"])
            await self.events.publish(event)
            if drain_events:
                await self.drain()
            else:
                await self._route_entry(event)
        new_row = dict(row)
        new_row.update(round_id=f"recorded-1h-{row['decision_time_ms']}", simulation_completed_at_ms=self.now,
            recorded_source={"cycle_line": item["source_line"], "original_round_id": row["round_id"],
                "original_simulation_completed_at_ms": row["simulation_completed_at_ms"]},
            role_runs=[], source_role_runs=[f"source_artifacts/{f}" for f in row.get("role_runs", [])],
            frozen_packet_file=f"source_artifacts/{row['frozen_packet_file']}",
            gm_result=self.gm_results.get(row["correlation_id"]),
            simulation={k:v for k,v in self.venue.snapshot().items() if k not in ("fills", "executions", "equity_curve")})
        append_json(self.root / "cycles.jsonl", new_row)

    async def _apply_due_recorded(self, until_ms):
        if self.recorded_run is None:
            return
        due = sorted((item for item in self.agenda
            if item["kind"].startswith("recorded_") and item["due"] <= until_ms),
            key=lambda item: (item["due"], item["sequence"]))
        for item in due:
            # Recorded outputs require no inference. Publish them even while
            # a PM call is pending, before processing later market candles.
            self.now = max(self.now, item["due"])
            self.venue.now_ms = self.now
            await self._apply_recorded(item, drain_events=False)
            self.agenda.remove(item)

    def enqueue(self, due: int, priority: int, kind: str, **fields):
        self.sequence += 1
        self.agenda.append({"due": due, "priority": priority, "sequence": self.sequence, "kind": kind, **fields})

    async def advance_to(self, target: int, *, stop_on_event: bool = False):
        async with self._advance_lock:
            await self._advance_locked(target, stop_on_event=stop_on_event)

    async def _advance_locked(self, target: int, *, stop_on_event: bool = False):
        target = min(target, self.end-1)
        if target < self.now:
            return
        while self.minute_index < len(self.minute_bars):
            bar = self.minute_bars[self.minute_index]
            if bar["close_time_ms"] > target:
                break
            self.minute_index += 1
            if bar["close_time_ms"] <= self.now:
                continue
            await self._apply_due_recorded(bar["close_time_ms"] - 1)
            self.now = bar["close_time_ms"]
            self.venue.resolve_executor_bar(bar)
            await self._protect_long_positions()
            self.venue.set_market(bar, decision_time_ms=self.now)
            await self._apply_due_recorded(self.now)
            if self.now >= self.start:
                await self.watcher.poll()
                await self._activate_pending_entries(bar)
                await self.watcher.poll()
                if stop_on_event and not self.event_queue.empty():
                    # Deliver opening/fill/exit wakes at the observed minute;
                    # do not skip an entire short-lived position on a jump to
                    # the next H1 output or 30-minute timer.
                    self.venue.now_ms = self.now
                    return
        await self._apply_due_recorded(target)
        self.now = max(target, self.now)
        self.venue.now_ms = self.now

    def observe_hedge_write(self):
        # The order is observed after the pre-write account snapshot. Advance
        # execution time by one millisecond; no later candle is supplied.
        self.now += 1
        self.venue.now_ms = self.now

    def active_correlations(self, symbol):
        return [row["correlation_id"] for row in read_bindings(self.store.root,
            account_name=self.venue.account_name, connector_name=self.venue.connector_name,
            controller_id=self.venue.controller_id)
            if symbol in (None, "", "*", row.get("symbol"))]

    async def load_pm_context(self, cid):
        context = await self.venue.pm_load_context(cid)
        if context is None or not self.long_lock_policy or cid != self.only_trade:
            return context
        state = self.venue.long_policy_context(cid)
        if state is None:
            return context
        context["management_policy"].update(
            policy_id="brooks-long-lock-selected-operation", version="experimental-1",
            protection_semantics="LONG limit locks exposure through a confirmed hedge; final operation exits require nonnegative combined net",
            applicable_risk_behavior={
                "long_exit_policy": "lock_and_wait_nonnegative_net",
                "operation_correlation_id": self.only_trade,
                "projection_scope": "MAIN_PLUS_HEDGE_NET",
                "funding_mode": "not_modeled",
                "short_stop_policy": "normal",
                "duration_policy": "pm_managed_long",
                "lock_trigger": "observed_closed_m1_combined_net_5R" if self.max_unhedged_loss_r == "5" else "observed_closed_m1",
                "lock_ratio": "1", "max_unhedged_loss_r": self.max_unhedged_loss_r,
                "policy_state": state,
            })
        return context

    async def _protect_long_positions(self):
        if not getattr(self, "long_lock_policy", False):
            return
        while self.venue.long_lock_requests:
            proof = self.venue.long_lock_requests.pop(0)
            cid = proof["correlation_id"]
            binding = self.store.read_trade_document(cid, "binding.json")
            action = "INCREASE_HEDGE" if binding and binding.get("hedge_position_id") else "HEDGE"
            record = {"simulation_time_ms": self.now, "proof": proof,
                "action": action, "target_hedge_ratio": "1"}
            try:
                if binding is None:
                    raise ValueError("LONG protection has no authoritative binding")
                result = await self.gm.execute_hedge(
                    correlation_id=cid, decision_id=f"long-lock-{self.now}",
                    action=action, target_hedge_ratio="1",
                    plan_main_position_id=binding.get("main_position_id"),
                    plan_hedge_position_id=binding.get("hedge_position_id"))
                record["gm_result"] = result
                if result.get("assessment") != "confirmed":
                    self.venue.rearm_long_protection(cid)
            except Exception as exc:
                record["error"] = {"type": type(exc).__name__, "message": str(exc)}
                # No fabricated hedge: the next observed M1 can retry after
                # reconciliation. Failed protection remains visible.
                logging.exception("LONG protection failed %s", cid)
                self.venue.rearm_long_protection(cid)
            append_json(self.root / "long_lock_events.jsonl", record)
        while self.venue.duration_requests:
            proof = self.venue.duration_requests.pop(0)
            append_json(self.root / "long_duration_events.jsonl", proof)
            event = BrooksEvent(EventType.PM_TIMER, self.symbol,
                {"duration_limit_reached": proof}, correlation_id=proof["correlation_id"],
                created_at_ms=self.now)
            await self.events.publish(event)
            self.enqueue(self.now, 20, "pm", event=event.to_dict())

    def save_pm(self, cid, decision):
        value = decision.model_dump(mode="json")
        self.store.write_trade_document(cid, "latest_management_intent.json", value)
        self.store.append_trade_history(cid, "management_history.jsonl", value)

    def record_pm_read(self, cid, record):
        append_json(self.root / "pm_market_reads.jsonl", {"correlation_id": cid, **record})

    def persist_capture(self, record):
        write_json(self.root / record["file"], record)

    def _persist_pending_entry(self, cid: str, *, gm_result=None):
        state = self.pending_entries[cid].model_dump(mode="json")
        event = self.pending_events[cid]
        record = {**state, "original_intent": event["payload"]["intent"],
            "source_event_id": event["event_id"]}
        if gm_result is not None:
            record["gm_result"] = gm_result
        write_json(self.store.root / "trades" / cid / "pending_entry.json", record)
        self.venue.pending_intents = [value.model_dump(mode="json")
            for value in self.pending_entries.values() if value.status == "pending"]

    def _register_pending_entry(self, event: BrooksEvent):
        cid = event.correlation_id
        if cid in self.pending_entries:
            return
        intent = event.payload["intent"]
        for prior_id, prior in list(self.pending_entries.items()):
            if prior.status == "pending" and prior.symbol == event.symbol:
                self.pending_entries[prior_id] = prior.model_copy(update={
                    "status": "canceled", "changed_at_ms": self.now,
                    "resolution_reason": "superseded_by_newer_entry"})
                self._persist_pending_entry(prior_id)
                append_json(self.root / "pending_entry_events.jsonl",
                    self.pending_entries[prior_id].model_dump(mode="json"))
        self.pending_events[cid] = event.to_dict()
        self.pending_entries[cid] = process_pending_stop(intent,
            correlation_id=cid, available_at_ms=self.now, closed_bars=[],
            as_of_ms=self.now, max_intent_age_ms=self.policy.max_intent_age_ms)
        self._persist_pending_entry(cid)
        append_json(self.root / "pending_entry_events.jsonl",
            self.pending_entries[cid].model_dump(mode="json"))
        logging.info("PENDING ENTRY %s available=%s", cid, self.now)

    async def _activate_pending_entries(self, bar):
        for cid, prior in list(self.pending_entries.items()):
            if prior.status != "pending":
                continue
            source = BrooksEvent.from_dict(self.pending_events[cid])
            state = process_pending_stop(source.payload["intent"],
                correlation_id=cid, available_at_ms=prior.available_at_ms,
                closed_bars=[bar], as_of_ms=self.now,
                max_intent_age_ms=self.policy.max_intent_age_ms, state=prior)
            self.pending_entries[cid] = state
            if state.status == "pending":
                continue
            # Proof is saved before the one permitted submission. Never emit a
            # second Trader intent or modify a stored model response.
            self._persist_pending_entry(cid)
            append_json(self.root / "pending_entry_events.jsonl", state.model_dump(mode="json"))
            logging.info("PENDING RESOLVED %s %s sim=%s", cid, state.status, self.now)
            if state.status != "triggered":
                continue
            write_json(self.store.root / "trades" / cid / "execution_trade_intent.json", state.execution_intent)
            activated = replace(source, payload={**source.payload,
                "intent": state.execution_intent, "pending_activation": state.evidence})
            self.venue.stage_trade_intent(cid, state.execution_intent)
            # GM is deterministic. scoped() would recursively await this lock.
            result = await self.gm_consumer.handle(activated)
            outcome = result.to_dict() if isinstance(result, BrooksEvent) else result
            self.gm_results[cid] = outcome
            logging.info("ENTRY ACTIVATION %s %s", cid, outcome.get("type") if isinstance(outcome, dict) else outcome)
            if isinstance(result, BrooksEvent) and result.type == EventType.GM_ENTRY_REJECTED:
                self.venue.mark_entry_rejected(cid, str(result.payload.get("reason", "")))
            if (self.store.root / "trades" / cid / "original_trade_intent.json").exists():
                self.store.write_trade_document(cid, "original_trade_intent.json", source.payload["intent"])
            self._persist_pending_entry(cid, gm_result=outcome)
            append_json(self.root / "entry_activation_outcomes.jsonl", {
                "correlation_id": cid, "simulation_time_ms": self.now,
                "activation": state.model_dump(mode="json"), "gm_result": outcome})
            self.venue.clear_staged_trade_intent()

    async def _route_entry(self, event):
        intent = event.payload["intent"]
        if intent.get("decision") == "NO_TRADE":
            return
        cid = event.correlation_id
        if cid in self.gm_results:
            return
        setup, trigger = intent.get("setup") or {}, intent.get("trigger") or {}
        if (self.recorded_run is not None and setup.get("trigger_status") == "pending"
            and trigger.get("kind") == "stop"):
            self._register_pending_entry(event)
            return
        self.venue.stage_trade_intent(cid, intent)
        result = await self.gm_consumer.handle(event)
        self.gm_results[cid] = result.to_dict() if isinstance(result, BrooksEvent) else result

    async def drain(self):
        while not self.event_queue.empty():
            event = self.event_queue.get_nowait()
            if event.type == EventType.TRADER_INTENT_CREATED:
                await self._route_entry(event)
            elif event.type == EventType.MANAGEMENT_INTENT_CREATED:
                rejection = self._long_management_rejection(event)
                if rejection:
                    result = BrooksEvent(EventType.GM_MANAGEMENT_REJECTED, event.symbol,
                        {"reason": rejection, "action": event.payload.get("action"),
                            "policy": "lock_and_wait_nonnegative_net"},
                        correlation_id=event.correlation_id, causation_id=event.event_id)
                    await self.events.publish(result)
                else:
                    result, _ = await self.scoped(f"gm-management-{event.event_id}", lambda: self.gm_consumer.handle(event))
                if (getattr(self, "long_lock_policy", False)
                    and isinstance(result, BrooksEvent)
                    and result.type == EventType.GM_MANAGEMENT_APPROVED):
                    action = event.payload.get("action")
                    confirmed = result.payload.get("result", {}).get("assessment") == "confirmed"
                    if action in ("REDUCE_HEDGE", "REMOVE_HEDGE") and confirmed:
                        self.venue.rearm_long_protection(event.correlation_id)
                    if action == "HOLD":
                        state = self.venue.long_policy_context(event.correlation_id)
                        if state and state.get("duration_expires_at_ms") is not None and state["duration_expires_at_ms"] <= self.now:
                            self.venue.prolong_long_duration(event.correlation_id,
                                self.now + self.pm_interval_sec * 1000)
                            append_json(self.root / "long_duration_events.jsonl", {
                                "correlation_id": event.correlation_id,
                                "simulation_time_ms": self.now, "action": "HOLD_EXTENSION",
                                "expires_at_ms": self.now + self.pm_interval_sec * 1000})
                append_json(self.root / "management_outcomes.jsonl", {"simulation_time_ms": self.now,
                    "correlation_id": event.correlation_id, "decision": event.payload,
                    "gm_result": result.to_dict() if isinstance(result, BrooksEvent) else result})
            if event.type.value in PM_WAKE_EVENTS and event.type != EventType.PM_TIMER:
                self.enqueue(self.now, 20, "pm", event=event.to_dict())
            if (getattr(self, "only_trade", None) == event.correlation_id
                and event.type == EventType.POSITION_CLOSED):
                self.manifest["stop_reason"] = "selected_trade_closed"
                self.stop.set()

    def _long_management_rejection(self, event):
        if not getattr(self, "long_lock_policy", False):
            return None
        if event.payload.get("action") not in ("CLOSE", "REDUCE"):
            return None
        state = self.venue.long_policy_context(event.correlation_id)
        if state is None:
            return "LONG exit blocked: operation net projection is unavailable"
        if state.get("main_side") != "LONG":
            return None
        projection = state.get("projected_exit_net")
        if projection is None or Decimal(projection) < 0:
            return "LONG exit blocked: combined MAIN+HEDGE net after modeled costs is negative or unknown"
        if Decimal(state["hedge_quantity"]) > 0:
            return "LONG exit blocked: unwind HEDGE and re-evaluate fresh state before closing/reducing MAIN"
        return None

    async def scoped(self, label: str, operation):
        scope = {"round_id": label, "captures": []}
        token = ACTIVE_CAPTURE.set(scope)
        host = {"status": "cancelled", "accepted": False}
        initial_sim_time, begun = self.now, time.monotonic()
        tick_stop = asyncio.Event()
        async def tick():
            while not tick_stop.is_set():
                try:
                    await asyncio.wait_for(tick_stop.wait(), timeout=0.2)
                except TimeoutError:
                    await self.advance_to(initial_sim_time + int((time.monotonic()-begun)*1000))
        ticker = asyncio.create_task(tick())
        try:
            result = await operation()
            host = {"status": "completed", "accepted": result is not None}
            return result, scope
        except Exception as exc:
            host = {"status": "failed", "accepted": False, "error": {"type": type(exc).__name__, "message": str(exc)[:1000]}}
            append_json(self.root / "failures.jsonl", {"round_id": label, "simulation_time_ms": self.now, **host})
            logging.exception("Replay operation failed: %s", label)
            return None, scope
        finally:
            tick_stop.set()
            await ticker
            await self.advance_to(initial_sim_time + int((time.monotonic()-begun)*1000))
            for record in scope["captures"]:
                record["host"] = host
                record["status"] = host["status"]
                self.persist_capture(record)
            ACTIVE_CAPTURE.reset(token)

    async def checkpoint(self):
        self.venue.save_checkpoint(self.args.state / "venue_checkpoint.json")
        self.venue.flush_to(self.root / "simulation")
        shutil.copyfile(self.store.events_path, self.root / "events.jsonl")
        for timeframe in ("d1", "h4"):
            context_dir = self.store.root / "context" / timeframe
            if context_dir.exists():
                shutil.copytree(context_dir, self.root / "contexts" / timeframe, dirs_exist_ok=True)
        trades_dir = self.store.root / "trades"
        if trades_dir.exists():
            shutil.copytree(trades_dir, self.root / "trade_state", dirs_exist_ok=True)
        self.manifest["simulation_time_ms"] = self.now
        self.manifest["updated_at_ms"] = time.time_ns() // 1_000_000
        write_json(self.root / "run_manifest.json", self.manifest)
        write_json(self.args.state / "walkforward_checkpoint.json", {
            "git_head": self.args.git_head, "start_ms": self.start, "end_ms": self.end,
            "now": self.now, "minute_index": self.minute_index, "agenda": self.agenda,
            "sequence": self.sequence, "manifest": self.manifest, "gm_results": self.gm_results,
            "pending_entries": {cid: value.model_dump(mode="json") for cid, value in self.pending_entries.items()},
            "pending_events": self.pending_events,
            "only_trade": self.only_trade, "long_lock_policy": self.long_lock_policy,
            "max_unhedged_loss_r": self.max_unhedged_loss_r,
            "watcher_snapshots": list(self.watcher._previous.values())})
        render_report(self.root)

    async def run(self):
        if self.manifest.get("status") == "completed":
            return
        self.manifest["status"] = "running"
        write_json(self.root / "run_manifest.json", self.manifest)
        render_report(self.root)
        if self.recorded_run is not None:
            self._copy_recorded_artifacts()
        if self.recorded_run is None and not (self.args.state / "walkforward_checkpoint.json").exists():
            await self.advance_to(self.now)
            for tf, interval in (("1d", DAY), ("4h", 4*HOUR)):
                close = self.start-interval-1
                self.current_round = f"bootstrap-{tf}-{close}"
                event = BrooksEvent(EventType.D1_BAR_CLOSED if tf=="1d" else EventType.H4_BAR_CLOSED,
                    self.symbol, {"decision_time_ms": close}, correlation_id=f"bootstrap-{tf}-{close}")
                await self.scoped(self.current_round, lambda tf=tf, event=event: self.contexts[tf].handle(event))
                await self.drain()
        await self.checkpoint()
        while self.agenda and not self.stop.is_set():
            ready = [event for event in self.agenda if event["due"] <= self.now]
            item = min(ready, key=lambda e:(e["priority"],e["due"],e["sequence"])) if ready else min(self.agenda, key=lambda e:(e["due"],e["priority"],e["sequence"]))
            await self.advance_to(max(self.now, item["due"]), stop_on_event=True)
            await self.drain()
            if self.now < item["due"] or item not in self.agenda:
                continue
            label = f"{item['kind']}-{item.get('timeframe','pm')}-{item.get('decision',item['due'])}-{item['sequence']}"
            self.current_round = label
            logging.info("REPLAY START %s sim=%s", label, self.now)
            if item["kind"].startswith("recorded_"):
                await self._apply_recorded(item)
            elif item["kind"] == "clock":
                tf, decision = item["timeframe"], item["decision"]
                event_type = {"1h":EventType.H1_BAR_CLOSED,"4h":EventType.H4_BAR_CLOSED,"1d":EventType.D1_BAR_CLOSED}[tf]
                self.clock.source = self.source.at_decision_time(decision)
                event = await self.clock.publish_closed_bar(self.symbol, tf, decision, event_type)
                consumer = self.trader if tf=="1h" else self.contexts[tf]
                result, scope = await self.scoped(label, lambda: consumer.handle(event))
                await self.drain()
                if tf=="1h":
                    cycle_id = DecisionCycleStore.identity(self.symbol, decision)
                    cycle = dict(self.trader._cycle_store.get(cycle_id) or {})
                    packet = cycle.pop("frozen_packet", None)
                    packet_file = f"frozen_packets/{cycle_id}.json"
                    if packet is not None:
                        write_json(self.root / packet_file, packet)
                    row = {"round_id": label, "symbol": self.symbol, "decision_time_ms": decision,
                        "simulation_completed_at_ms": self.now, "status":cycle.get("status"), "cycle":cycle,
                        "correlation_id": event.correlation_id,
                        "intent":result.model_dump(mode="json") if result else None,
                        "frozen_packet_file":packet_file, "packet_hash":cycle.get("packet_hash"),
                        "role_runs":[record["file"] for record in scope["captures"]],
                        "gm_result":self.gm_results.get(event.correlation_id),
                        "simulation":{k:v for k,v in self.venue.snapshot().items() if k not in ("fills","executions","equity_curve")}}
                    append_json(self.root / "cycles.jsonl", row)
                    logging.info("REPLAY H1 %s %s", decision, result.decision if result else "FAILED")
            else:
                event = BrooksEvent.from_dict(item["event"]) if item["kind"]=="pm" else BrooksEvent(EventType.PM_TIMER,"*",created_at_ms=self.now)
                await self.scoped(label, lambda: self.pm.handle_event(event.to_dict()))
                await self.drain()
            self.agenda.remove(item)
            await self.checkpoint()
        if self.manifest.get("stop_reason") == "selected_trade_closed":
            self.manifest.update(status="completed", finished_at_ms=time.time_ns()//1_000_000,
                pending_endpoint_pm_events=len(self.agenda))
        elif not self.agenda:
            await self.advance_to(self.end-1)
            await self.drain()
            # Exit/mark events at the endpoint are retained; no post-window model calls.
            self.manifest.update(status="completed", finished_at_ms=time.time_ns()//1_000_000,
                                 pending_endpoint_pm_events=len(self.agenda))
        else:
            self.manifest["status"] = "paused"
        await self.checkpoint()


async def main_async(args):
    load_dotenv(REPO / ".env")
    runner = WalkForward(args)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runner.stop.set)
    real_build, real_send, real_gm_time = agent_runner.build_llm_client, httpx.AsyncClient.send, gm_module.time
    class ClockProxy:
        def time(self): return runner.now / 1000
        def __getattr__(self, name): return getattr(real_gm_time, name)
    gm_module.time = ClockProxy()
    def build(key, **kwargs):
        inner = real_build(key, **kwargs)
        if type(inner).__name__ != "PydanticAIClient":
            raise RuntimeError("This replay requires PydanticAI API; CLI bridges are forbidden")
        scope = ACTIVE_CAPTURE.get()
        if scope is None:
            raise RuntimeError("Historical model call escaped its trace scope")
        system = kwargs.get("system_prompt", "")
        role = "POSITION_MANAGER" if "Brooks POSITION_MANAGER role" in system else "CONTEXT_ANALYST" if "Brooks CONTEXT_ANALYST role" in system else "TRADER"
        run_id = f"{scope['round_id']}-{len(scope['captures'])+1}-{uuid4().hex[:8]}"
        record = {"run_id":run_id,"round_id":scope["round_id"],"role":role,"model":key,
            "file":f"role_runs/{run_id}.json","system":system,"status":"running","calls":[]}
        scope["captures"].append(record)
        runner.persist_capture(record)
        return CapturedClient(inner, record, runner)
    async def send(client, request, *positional, **kwargs):
        scope = ACTIVE_CAPTURE.get()
        if request.method=="POST" and request.url.path.endswith("/chat/completions") and scope and scope["captures"]:
            record = scope["captures"][-1]
            call = record["calls"][-1]
            wire_file = f"wire_requests/{record['run_id']}-{len(record['calls'])}-{len(call['wire_requests'])+1}.json"
            path = runner.root / wire_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(request.content)
            call["wire_requests"].append({"file":wire_file,"sha256":hashlib.sha256(request.content).hexdigest(),"endpoint":str(request.url)})
            runner.persist_capture(record)
        return await real_send(client, request, *positional, **kwargs)
    agent_runner.build_llm_client, httpx.AsyncClient.send = build, send
    try:
        await runner.run()
    finally:
        agent_runner.build_llm_client, httpx.AsyncClient.send, gm_module.time = real_build, real_send, real_gm_time
        runner.events.close()
        runner.store.flush()
    return 0 if runner.manifest["status"]=="completed" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("/tmp/brooks-walkforward-10d-data"))
    parser.add_argument("--output", type=Path, default=REPO / "docs/brooks_walkforward_2026-09-20_2026-09-29")
    parser.add_argument("--state", type=Path, default=Path("/tmp/brooks-walkforward-10d-state"))
    parser.add_argument("--symbol", default="ETH-USDT")
    parser.add_argument("--start", default="2026-09-20T00:00:00Z")
    parser.add_argument("--end", default="2026-09-30T00:00:00Z")
    parser.add_argument("--agent-key", default="custom@opencode-go:deepseek-v4.1-flash")
    parser.add_argument("--initial-equity", default="10000")
    parser.add_argument("--fee-rate", default="0.0004")
    parser.add_argument("--slippage-bps", default="1")
    parser.add_argument("--git-head", required=True)
    parser.add_argument("--recorded-run", type=Path,
        help="Replay accepted Trader and Context Analyst source outputs at their original completion times; run PM and GM live")
    parser.add_argument("--only-trade", help="Reproduce only this source Trader correlation id; stop after its position closes")
    parser.add_argument("--long-lock-policy", action="store_true",
        help="Simulated selected-operation experiment: LONG limit locks a hedge and negative combined net MAIN exits are blocked")
    parser.add_argument("--max-unhedged-loss-r", choices=["5"],
        help="Permit unhedged LONG operation loss up to 5 times the original GM risk; lock at observed combined net ceiling")
    parser.add_argument("--pm-timeout-sec", type=float, default=60,
        help="Explicit PM role timeout for this replay; production and default replay policy unchanged")
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    lock = (args.state / "runner.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
