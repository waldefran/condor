"""Deterministic Brooks gate for MAIN entries and basic PM management."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .execution import HummingbotExecutionPort


class GMRejected(ValueError):
    """An intent cannot safely become a venue write."""


def _decimal(value: Any, name: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise GMRejected(f"{name} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GMRejected(f"{name} is invalid") from None
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise GMRejected(
            f"{name} must be {'positive' if positive else 'nonnegative'} and finite"
        )
    return number


def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise GMRejected("expected a validated contract or mapping")


@dataclass(frozen=True)
class VenueRules:
    amount_step: Decimal
    min_amount: Decimal
    min_notional: Decimal
    max_leverage: int


@dataclass(frozen=True)
class AccountSnapshot:
    """Fresh normalized account state supplied by a trusted venue reader.

    The reader must aggregate account exposure and resolve MAIN ownership from
    persisted bindings. Missing fields are rejected rather than estimated.
    """

    as_of_ms: int
    equity: Decimal
    available_margin: Decimal
    mark_price: Decimal
    gross_exposure: Decimal
    open_positions: int
    rules: VenueRules
    structure_status: str = "no_positions"
    main_position_id: str | None = None
    main_executor_id: str | None = None
    main_side: str | None = None
    main_quantity: Decimal = Decimal(0)


class AccountStateReader(Protocol):
    async def read(
        self, *, account_name: str, connector_name: str, symbol: str
    ) -> AccountSnapshot: ...


@dataclass(frozen=True)
class GMPolicy:
    risk_per_trade_pct: Decimal
    max_positions: int
    max_gross_exposure_pct: Decimal
    leverage: int
    take_profit_r: Decimal
    time_limit_sec: int
    max_trigger_drift_pct: Decimal = Decimal("0.01")
    max_snapshot_age_ms: int = 15_000
    max_intent_age_ms: int = 7_200_000


@dataclass(frozen=True)
class MainPlan:
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    stop_price: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    risk_usd: Decimal
    notional: Decimal
    margin_required: Decimal
    leverage: int
    time_limit_sec: int


def compile_main(
    intent: Any,
    snapshot: AccountSnapshot,
    policy: GMPolicy,
    *,
    now_ms: int | None = None,
) -> MainPlan:
    """Size a linear perp from a structural stop, then apply every venue/account cap."""
    trade = _data(intent)
    if trade.get("schema") != "brooks.trade-intent.v2" or trade.get("role") != "TRADER":
        raise GMRejected("unvalidated Trader contract")
    decision = trade.get("decision")
    if decision not in ("ENTER_LONG", "ENTER_SHORT"):
        raise GMRejected("intent is not an entry")
    symbol = trade.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise GMRejected("symbol is required")
    side = "LONG" if decision == "ENTER_LONG" else "SHORT"
    trigger = trade.get("trigger")
    invalidation = trade.get("invalidation")
    if not isinstance(trigger, dict) or not isinstance(invalidation, dict):
        raise GMRejected("trigger and structural invalidation are required")
    reference = _decimal(trigger.get("price"), "trigger.price")
    stop = _decimal(invalidation.get("price"), "invalidation.price")
    if (side == "LONG" and stop >= reference) or (
        side == "SHORT" and stop <= reference
    ):
        raise GMRejected("invalidation is on the wrong side of entry")
    if snapshot.structure_status != "no_positions":
        raise GMRejected("position ownership or structure is unresolved")
    if isinstance(snapshot.open_positions, bool) or snapshot.open_positions < 0:
        raise GMRejected("open position count is invalid")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    decision_time = trade.get("decision_time_ms")
    if (
        isinstance(decision_time, bool)
        or not isinstance(decision_time, int)
        or decision_time > now
        or now - decision_time > policy.max_intent_age_ms
    ):
        raise GMRejected("Trader intent is stale or has no decision time")
    if (
        not isinstance(snapshot.as_of_ms, int)
        or snapshot.as_of_ms > now
        or now - snapshot.as_of_ms > policy.max_snapshot_age_ms
    ):
        raise GMRejected("account snapshot is stale")
    equity = _decimal(snapshot.equity, "equity")
    margin = _decimal(snapshot.available_margin, "available_margin", positive=False)
    mark = _decimal(snapshot.mark_price, "mark_price")
    gross = _decimal(snapshot.gross_exposure, "gross_exposure", positive=False)
    risk_pct = _decimal(policy.risk_per_trade_pct, "risk_per_trade_pct")
    gross_pct = _decimal(policy.max_gross_exposure_pct, "max_gross_exposure_pct")
    reward_r = _decimal(policy.take_profit_r, "take_profit_r")
    drift_pct = _decimal(
        policy.max_trigger_drift_pct, "max_trigger_drift_pct", positive=False
    )
    if risk_pct >= 1 or drift_pct >= 1 or policy.max_positions < 1:
        raise GMRejected("invalid risk policy")
    if (
        not isinstance(policy.leverage, int)
        or isinstance(policy.leverage, bool)
        or policy.leverage < 1
    ):
        raise GMRejected("invalid leverage")
    if policy.leverage > snapshot.rules.max_leverage:
        raise GMRejected("leverage exceeds venue rule")
    if not isinstance(policy.time_limit_sec, int) or policy.time_limit_sec < 1:
        raise GMRejected("time limit is required")
    if snapshot.open_positions >= policy.max_positions:
        raise GMRejected("max positions reached")
    if abs(mark - reference) / reference > drift_pct:
        raise GMRejected("market moved too far from trigger")
    if (side == "LONG" and stop >= mark) or (side == "SHORT" and stop <= mark):
        raise GMRejected("stop is no longer protective at mark")
    distance = abs(mark - stop)
    risk_usd = equity * risk_pct
    raw_qty = risk_usd / distance
    rules = snapshot.rules
    step = _decimal(rules.amount_step, "amount_step")
    minimum = _decimal(rules.min_amount, "min_amount")
    min_notional = _decimal(rules.min_notional, "min_notional")
    quantity = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < minimum:
        raise GMRejected("risk-sized quantity is below venue minimum")
    notional = quantity * mark
    if notional < min_notional:
        raise GMRejected("risk-sized notional is below venue minimum")
    if gross + notional > equity * gross_pct:
        raise GMRejected("gross exposure cap exceeded")
    margin_required = notional / Decimal(policy.leverage)
    if margin_required > margin:
        raise GMRejected("available margin is insufficient")
    stop_pct = distance / mark
    target_pct = stop_pct * reward_r
    if stop_pct >= 1 or target_pct >= 1:
        raise GMRejected("computed barrier is invalid")
    if quantity * distance > risk_usd:
        raise GMRejected("quantization exceeded risk budget")
    return MainPlan(
        symbol,
        side,
        quantity,
        mark,
        stop,
        stop_pct,
        target_pct,
        risk_usd,
        notional,
        margin_required,
        policy.leverage,
        policy.time_limit_sec,
    )


class BrooksGM:
    """Serializes writes by account/connector/symbol and persists their identity."""

    _locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def __init__(
        self,
        *,
        account_name: str,
        connector_name: str,
        state_root: Path,
        policy: GMPolicy,
        reader: AccountStateReader,
        execution: HummingbotExecutionPort,
    ):
        self.account_name = account_name
        self.connector_name = connector_name
        self.root = Path(state_root)
        self.policy = policy
        self.reader = reader
        self.execution = execution

    async def _locked(self, symbol: str):
        key = (self.account_name, self.connector_name, symbol)
        lock = self._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        lock_dir = self.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        import hashlib

        name = hashlib.sha256("\0".join(key).encode()).hexdigest()
        fd = os.open(lock_dir / name, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            return lock, fd
        except BaseException:
            os.close(fd)
            lock.release()
            raise

    @staticmethod
    def _unlock(lock: asyncio.Lock, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        lock.release()

    def _trade_dir(self, correlation_id: str) -> Path:
        if not correlation_id or not all(
            c.isalnum() or c in "-_" for c in correlation_id
        ):
            raise GMRejected("unsafe correlation_id")
        return self.root / "trades" / correlation_id

    def _has_unreconciled_main(self, symbol: str) -> bool:
        """A venue read may lag a submitted executor; its binding still owns the slot."""
        for path in (self.root / "trades").glob("*/binding.json"):
            try:
                binding = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GMRejected("cannot inspect existing MAIN bindings") from exc
            if (
                not isinstance(binding, dict)
                or binding.get("schema") != "condor.brooks.trade-binding.v1"
            ):
                raise GMRejected("cannot verify existing MAIN binding")
            if (
                binding.get("account_name") == self.account_name
                and binding.get("connector_name") == self.connector_name
                and binding.get("symbol") == symbol
                and binding.get("status") in ("submitting", "submitted")
            ):
                return True
        return False

    @staticmethod
    def _write_new(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as file:
            json.dump(data, file, sort_keys=True, default=str)
            file.flush()
            os.fsync(file.fileno())

    @staticmethod
    def _replace(path: Path, data: dict[str, Any]) -> None:
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(data, file, sort_keys=True, default=str)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)

    async def execute_entry(
        self, intent: Any, *, correlation_id: str
    ) -> dict[str, Any] | None:
        trade = _data(intent)
        if trade.get("decision") == "NO_TRADE":
            return None
        symbol = trade.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise GMRejected("symbol is required")
        lock, fd = await self._locked(symbol)
        try:
            trade_dir = self._trade_dir(correlation_id)
            binding_path = trade_dir / "binding.json"
            if binding_path.exists():
                raise GMRejected(
                    "correlation already submitted; reconcile before retry"
                )
            if self._has_unreconciled_main(symbol):
                raise GMRejected(
                    "MAIN already submitted for symbol; reconcile before retry"
                )
            snapshot = await self.reader.read(
                account_name=self.account_name,
                connector_name=self.connector_name,
                symbol=symbol,
            )
            plan = compile_main(trade, snapshot, self.policy)
            binding = {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": correlation_id,
                "account_name": self.account_name,
                "connector_name": self.connector_name,
                "controller_id": self.execution.controller_id,
                "symbol": symbol,
                "main_side": plan.side,
                "planned_quantity": str(plan.quantity),
                "status": "submitting",
                "main_executor_id": None,
                "executor_id": None,
                "main_position_id": None,
            }
            self._write_new(trade_dir / "original_trade_intent.json", trade)
            self._write_new(binding_path, binding)
            try:
                executor_id = await self.execution.open_main(
                    symbol=symbol,
                    side=plan.side,
                    quantity=plan.quantity,
                    leverage=plan.leverage,
                    stop_loss_pct=plan.stop_loss_pct,
                    take_profit_pct=plan.take_profit_pct,
                    time_limit_sec=plan.time_limit_sec,
                )
            except Exception:
                # A timeout can mean accepted. Keep the reservation until reconciliation.
                raise
            binding["main_executor_id"] = executor_id
            binding["executor_id"] = executor_id
            binding["status"] = "submitted"
            self._replace(binding_path, binding)
            return binding
        finally:
            self._unlock(lock, fd)

    async def execute_management(
        self,
        *,
        correlation_id: str,
        decision_id: str,
        action: str,
        reduce_fraction: Decimal | None = None,
    ) -> dict[str, Any]:
        trade_dir = self._trade_dir(correlation_id)
        binding_path = trade_dir / "binding.json"
        if not binding_path.exists():
            raise GMRejected("MAIN binding is missing")
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        symbol = binding.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise GMRejected("MAIN binding has no symbol")
        if action == "HOLD":
            return {"action": "HOLD", "status": "no_write"}
        if action not in ("REDUCE", "CLOSE"):
            raise GMRejected("unsupported management action")
        if not decision_id or not all(c.isalnum() or c in "-_" for c in decision_id):
            raise GMRejected("unsafe decision_id")
        lock, fd = await self._locked(symbol)
        try:
            # Re-read under lock: neither the PM snapshot nor the earlier binding
            # is an authority for current quantity or ownership.
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if binding.get("status") != "submitted" or not binding.get(
                "main_executor_id"
            ):
                raise GMRejected("MAIN execution is not confirmed")
            if (
                binding.get("account_name") != self.account_name
                or binding.get("connector_name") != self.connector_name
                or binding.get("controller_id") != self.execution.controller_id
            ):
                raise GMRejected(
                    "MAIN binding belongs to a different account or controller"
                )
            record_path = trade_dir / "management" / f"{decision_id}.json"
            if record_path.exists():
                raise GMRejected("decision already submitted; reconcile before retry")
            state = await self.reader.read(
                account_name=self.account_name,
                connector_name=self.connector_name,
                symbol=symbol,
            )
            now = int(time.time() * 1000)
            if (
                state.as_of_ms > now
                or now - state.as_of_ms > self.policy.max_snapshot_age_ms
            ):
                raise GMRejected("account snapshot is stale")
            _decimal(state.equity, "equity")
            _decimal(state.available_margin, "available_margin", positive=False)
            _decimal(state.gross_exposure, "gross_exposure", positive=False)
            _decimal(state.mark_price, "mark_price")
            if (
                state.structure_status != "single_main"
                or state.main_executor_id != binding["main_executor_id"]
                or state.main_side != binding["main_side"]
            ):
                raise GMRejected("MAIN ownership is unresolved or changed")
            if (
                binding.get("main_position_id")
                and state.main_position_id != binding["main_position_id"]
            ):
                raise GMRejected("MAIN position id changed")
            quantity = _decimal(state.main_quantity, "main_quantity")
            if action == "REDUCE":
                fraction = _decimal(reduce_fraction, "reduce_fraction")
                if fraction >= 1:
                    raise GMRejected("REDUCE fraction must be below one")
                step = _decimal(state.rules.amount_step, "amount_step")
                close_qty = ((quantity * fraction) / step).to_integral_value(
                    rounding=ROUND_DOWN
                ) * step
                if (
                    close_qty < _decimal(state.rules.min_amount, "min_amount")
                    or close_qty >= quantity
                ):
                    raise GMRejected("reduction is too small or would close all")
                if close_qty * _decimal(state.mark_price, "mark_price") < _decimal(
                    state.rules.min_notional, "min_notional"
                ):
                    raise GMRejected("reduction is below minimum notional")
            else:
                close_qty = quantity
            record = {
                "decision_id": decision_id,
                "action": action,
                "quantity": str(close_qty),
                "status": "submitting",
            }
            self._write_new(record_path, record)
            if action == "REDUCE":
                executor_id = await self.execution.reduce_main(
                    symbol=symbol,
                    side=state.main_side,
                    quantity=close_qty,
                    leverage=self.policy.leverage,
                )
            else:
                executor_id = await self.execution.close_main(
                    executor_id=state.main_executor_id
                )
            record.update(status="submitted", executor_id=executor_id)
            self._replace(record_path, record)
            return record
        finally:
            self._unlock(lock, fd)
