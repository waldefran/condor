"""Deterministic Brooks gate for MAIN entries and basic PM management."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from .execution import ExecutionRejected, HummingbotExecutionPort
from .hedge import (
    HedgeBlocked,
    HedgeCommand,
    HedgeResult,
    HedgeState,
    PositionLeg,
    assess_hedge_result,
    build_hedge_state,
    compile_hedge_action,
)


class GMRejected(ValueError):
    """An intent cannot safely become a venue write."""


log = logging.getLogger(__name__)


#: Fresh pre-write reads retried while the structure is unresolved (read-only).
#: The demo venue splits a just-changed leg into transient rows that merge
#: within a minute or so; deterministic rejections still fail immediately.
_HEDGE_FRESH_READS = 6
_HEDGE_FRESH_DELAY_SEC = 10.0
#: Sized for venue read-after-write races: the demo venue transiently
#: misreports the just-written leg for tens of seconds while settled reads
#: are exact, so a short window would wedge on noise.
_HEDGE_RECORROBORATE_READS = 6
_HEDGE_RECORROBORATE_DELAY_SEC = 10.0


def _hedge_filled_quantity(
    reconciled_snapshot: Any,
    reconciled_state: Any,
    fresh_state: Any,
    outcome: str,
) -> str:
    if (
        reconciled_snapshot is not None
        and getattr(reconciled_snapshot, "filled_quantity", None) is not None
    ):
        return str(reconciled_snapshot.filled_quantity)
    if outcome == "succeeded" and reconciled_state is not None:
        diff = abs(
            Decimal(reconciled_state.hedge_size) - Decimal(fresh_state.hedge_size)
        )
        return format(diff, "f")
    return "0"


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
    # None when the venue publishes no leverage cap: the policy leverage then
    # sizes (margin-checked) and the venue validates the write. Never invented.
    max_leverage: int | None


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
    position_mode: str | None = None
    positions: Sequence[PositionLeg] | None = None
    hedge_position_id: str | None = None
    hedge_executor_id: str | None = None
    hedge_side: str | None = None
    hedge_quantity: Decimal = Decimal(0)
    hedge_mark_price: Decimal | None = None
    pending_orders: bool = False
    filled_quantity: str | Decimal | None = None


class AccountStateReader(Protocol):
    async def read(
        self, *, account_name: str, connector_name: str, symbol: str
    ) -> AccountSnapshot: ...


class MainReconciler(Protocol):
    """Deterministic MAIN identity from explicit executor lineage.

    Returns the venue position id bound to this executor, or ``None`` when the
    venue has not shown it yet or the read is ambiguous. Never raises for a
    venue miss: ``None`` keeps the binding ``submitted`` for a later retry.
    Implementations must resolve ownership from executor/controller lineage
    (executor ids, controller id, account, connector, symbol), never from
    position side, quantity, ordering, or PnL alone.
    """

    async def reconcile(
        self,
        *,
        account_name: str,
        connector_name: str,
        controller_id: str,
        symbol: str,
        side: str,
        executor_id: str,
    ) -> str | None: ...


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
    if (
        snapshot.rules.max_leverage is not None
        and policy.leverage > snapshot.rules.max_leverage
    ):
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
        reconciler: MainReconciler | None = None,
    ):
        self.account_name = account_name
        self.connector_name = connector_name
        self.root = Path(state_root)
        self.policy = policy
        self.reader = reader
        self.execution = execution
        self.reconciler = reconciler

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

    @staticmethod
    def _append_execution(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(data, sort_keys=True, default=str) + "\n"
        with path.open("a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())

    async def execute_entry(
        self, intent: Any, *, correlation_id: str, shadow_mode: bool = False
    ) -> dict[str, Any] | None:
        trade = _data(intent)
        if trade.get("decision") == "NO_TRADE":
            return None
        if shadow_mode or bool(trade.get("shadow_mode", False)):
            return None
        if isinstance(trade.get("intent"), dict) and bool(
            trade["intent"].get("shadow_mode", False)
        ):
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
            await self._try_reconcile_binding(binding_path, binding)
            return json.loads(binding_path.read_text(encoding="utf-8"))
        finally:
            self._unlock(lock, fd)

    async def _try_reconcile_binding(
        self, binding_path: Path, binding: dict[str, Any]
    ) -> None:
        # Best-effort: the venue read may lag the accepted executor, so a miss
        # or a reconciler failure keeps the binding submitted for reconcile_main.
        if self.reconciler is None:
            return
        try:
            position_id = await self.reconciler.reconcile(
                account_name=self.account_name,
                connector_name=self.connector_name,
                controller_id=self.execution.controller_id,
                symbol=binding.get("symbol", ""),
                side=binding.get("main_side", ""),
                executor_id=binding.get("main_executor_id", ""),
            )
        except Exception:
            return
        if isinstance(position_id, str) and position_id:
            binding["main_position_id"] = position_id
            binding["status"] = "reconciled"
            self._replace(binding_path, binding)

    async def reconcile_main(self, correlation_id: str) -> dict[str, Any]:
        """Retry MAIN reconciliation for a submitted binding.

        Returns the binding, reconciled when the venue now shows the executor's
        position, unchanged (still submitted) when it does not. Never invents
        an identity: without an explicit lineage match the binding is untouched.
        """
        trade_dir = self._trade_dir(correlation_id)
        binding_path = trade_dir / "binding.json"
        if not binding_path.exists():
            raise GMRejected("MAIN binding is missing")
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        symbol = binding.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise GMRejected("MAIN binding has no symbol")
        lock, fd = await self._locked(symbol)
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if binding.get("status") == "reconciled" and binding.get(
                "main_position_id"
            ):
                return binding
            if binding.get("status") not in ("submitting", "submitted") or not binding.get(
                "main_executor_id"
            ):
                raise GMRejected("MAIN execution is not confirmed")
            if self.reconciler is None:
                raise GMRejected("no reconciler is attached")
            try:
                position_id = await self.reconciler.reconcile(
                    account_name=self.account_name,
                    connector_name=self.connector_name,
                    controller_id=self.execution.controller_id,
                    symbol=symbol,
                    side=binding.get("main_side", ""),
                    executor_id=binding.get("main_executor_id", ""),
                )
            except Exception as exc:
                raise GMRejected(f"reconciliation read failed: {exc}") from exc
            if isinstance(position_id, str) and position_id:
                binding["main_position_id"] = position_id
                binding["status"] = "reconciled"
                self._replace(binding_path, binding)
            return binding
        finally:
            self._unlock(lock, fd)

    async def execute_management(
        self,
        *,
        correlation_id: str,
        decision_id: str | None = None,
        action: str | None = None,
        reduce_fraction: Decimal | str | None = None,
        target_hedge_ratio: str | Decimal | None = None,
        expected_state: HedgeState | dict[str, Any] | None = None,
        plan_main_position_id: str | None = None,
        plan_hedge_position_id: str | None = None,
        decision: Any = None,
        shadow_mode: bool = False,
    ) -> dict[str, Any]:
        if decision is not None:
            data = _data(decision) if not isinstance(decision, dict) else decision
            if action is None or not action:
                action = data.get("action")
            if decision_id is None or not decision_id:
                decision_id = data.get("decision_id")
            if reduce_fraction is None:
                reduce_fraction = data.get("reduce_fraction")
            if shadow_mode or bool(data.get("shadow_mode", False)):
                shadow_mode = True
            if data.get("hedge_plan"):
                hp = data["hedge_plan"]
                if hasattr(hp, "model_dump"):
                    hp = hp.model_dump(mode="json")
                if isinstance(hp, dict):
                    if target_hedge_ratio is None:
                        target_hedge_ratio = hp.get("target_hedge_ratio")
                    if plan_main_position_id is None:
                        plan_main_position_id = hp.get("main_position_id")
                    if plan_hedge_position_id is None:
                        plan_hedge_position_id = hp.get("hedge_position_id")
                else:
                    if target_hedge_ratio is None:
                        target_hedge_ratio = getattr(hp, "target_hedge_ratio", None)
                    if plan_main_position_id is None:
                        plan_main_position_id = getattr(hp, "main_position_id", None)
                    if plan_hedge_position_id is None:
                        plan_hedge_position_id = getattr(hp, "hedge_position_id", None)

        if shadow_mode:
            return {
                "action": action or "HOLD",
                "status": "no_write",
                "shadow_mode": True,
            }

        if action == "HOLD":
            return {"action": "HOLD", "status": "no_write"}

        if action == "MANAGEMENT_BLOCKED":
            return {"action": "MANAGEMENT_BLOCKED", "status": "no_write"}

        if not decision_id:
            decision_id = str(uuid4())

        if action in ("HEDGE", "INCREASE_HEDGE", "REDUCE_HEDGE", "REMOVE_HEDGE"):
            return await self._execute_hedge(
                correlation_id=correlation_id,
                decision_id=decision_id,
                action=action,
                target_hedge_ratio=target_hedge_ratio,
                expected_state=expected_state,
                plan_main_position_id=plan_main_position_id,
                plan_hedge_position_id=plan_hedge_position_id,
            )

        if action not in ("REDUCE", "CLOSE"):
            raise GMRejected("unsupported management action")
        if not decision_id or not all(c.isalnum() or c in "-_" for c in decision_id):
            raise GMRejected("unsafe decision_id")
        trade_dir = self._trade_dir(correlation_id)
        binding_path = trade_dir / "binding.json"
        if not binding_path.exists():
            raise GMRejected("MAIN binding is missing")
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        symbol = binding.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise GMRejected("MAIN binding has no symbol")
        lock, fd = await self._locked(symbol)
        try:
            # Re-read under lock: neither the PM snapshot nor the earlier binding
            # is an authority for current quantity or ownership.
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if binding.get("status") not in (
                "submitted",
                "reconciled",
            ) or not binding.get("main_executor_id"):
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
            if not binding.get("main_position_id") and state.main_position_id:
                binding["main_position_id"] = state.main_position_id
                self._replace(binding_path, binding)
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

    async def execute_hedge(
        self,
        *,
        correlation_id: str,
        decision_id: str | None = None,
        action: str | None = None,
        target_hedge_ratio: str | Decimal | None = None,
        expected_state: HedgeState | dict[str, Any] | None = None,
        plan_main_position_id: str | None = None,
        plan_hedge_position_id: str | None = None,
        decision: Any = None,
        shadow_mode: bool = False,
    ) -> dict[str, Any]:
        return await self.execute_management(
            correlation_id=correlation_id,
            decision_id=decision_id,
            action=action,
            target_hedge_ratio=target_hedge_ratio,
            expected_state=expected_state,
            plan_main_position_id=plan_main_position_id,
            plan_hedge_position_id=plan_hedge_position_id,
            decision=decision,
            shadow_mode=shadow_mode,
        )

    async def _resolve_first_hedge_leg(
        self,
        *,
        symbol: str,
        binding: dict[str, Any],
        main_position_id: str | None,
        main_side: str | None,
        hedge_executor_id: str | None,
    ) -> dict[str, str] | None:
        reconciler = getattr(self.reconciler, "reconcile_hedge", None)
        if reconciler is None or not hedge_executor_id or not main_position_id:
            return None
        hedge_side = (
            "SHORT" if (main_side or binding.get("main_side")) == "LONG" else "LONG"
        )
        try:
            return await reconciler(
                account_name=self.account_name,
                connector_name=self.connector_name,
                controller_id=self.execution.controller_id,
                symbol=symbol,
                main_position_id=main_position_id,
                hedge_side=hedge_side,
                hedge_executor_id=hedge_executor_id,
            )
        except Exception:
            return None

    async def _read_hedge_state(
        self,
        *,
        symbol: str,
        binding: dict[str, Any],
        main_pos_id: str | None,
        hedge_pos_id: str | None,
        action: str,
        fresh_state: HedgeState,
        hedge_executor_id: str | None,
    ) -> tuple[Any, HedgeState | None]:
        """One fresh read shaped as post-write HedgeState; ``None`` when off."""
        reconciled_snapshot = None
        try:
            reconciled_snapshot = await self.reader.read(
                account_name=self.account_name,
                connector_name=self.connector_name,
                symbol=symbol,
            )
            if reconciled_snapshot.positions is not None:
                rec_legs = reconciled_snapshot.positions
            else:
                rec_legs = []
                if (
                    reconciled_snapshot.main_position_id
                    and reconciled_snapshot.main_quantity > 0
                ):
                    rec_legs.append(
                        PositionLeg(
                            position_id=reconciled_snapshot.main_position_id,
                            symbol=symbol,
                            side=reconciled_snapshot.main_side
                            or binding.get("main_side"),
                            quantity=str(reconciled_snapshot.main_quantity),
                            mark_price=str(reconciled_snapshot.mark_price),
                            ownership_role="MAIN",
                        )
                    )
                rec_h_id = getattr(
                    reconciled_snapshot, "hedge_position_id", None
                ) or (hedge_pos_id if action != "REMOVE_HEDGE" else None)
                rec_h_qty = getattr(
                    reconciled_snapshot, "hedge_quantity", Decimal(0)
                )
                if rec_h_id and rec_h_qty > 0:
                    rec_h_side = getattr(reconciled_snapshot, "hedge_side", None)
                    if not rec_h_side:
                        main_s = reconciled_snapshot.main_side or binding.get(
                            "main_side"
                        )
                        rec_h_side = "SHORT" if main_s == "LONG" else "LONG"
                    rec_h_mark = (
                        getattr(reconciled_snapshot, "hedge_mark_price", None)
                        or reconciled_snapshot.mark_price
                    )
                    rec_legs.append(
                        PositionLeg(
                            position_id=rec_h_id,
                            symbol=symbol,
                            side=rec_h_side,
                            quantity=str(rec_h_qty),
                            mark_price=str(rec_h_mark),
                            ownership_role="HEDGE",
                        )
                    )
            if action == "HEDGE":
                h_legs = [leg for leg in rec_legs if leg.ownership_role == "HEDGE"]
                if len(h_legs) == 1:
                    rec_target_h_id = h_legs[0].position_id
                elif getattr(reconciled_snapshot, "hedge_position_id", None):
                    rec_target_h_id = getattr(
                        reconciled_snapshot, "hedge_position_id", None
                    )
                else:
                    # First HEDGE: the binding carries no hedge id yet, so the
                    # reader cannot see the new leg. Resolve it through hedge
                    # executor lineage instead of inventing it; None stays on
                    # the fail-closed ambiguous path below.
                    rec_target_h_id = None
                    resolved = await self._resolve_first_hedge_leg(
                        symbol=symbol,
                        binding=binding,
                        main_position_id=main_pos_id,
                        main_side=fresh_state.main_side,
                        hedge_executor_id=hedge_executor_id,
                    )
                    if resolved is not None:
                        rec_legs = list(rec_legs) + [
                            PositionLeg(
                                position_id=resolved["position_id"],
                                symbol=symbol,
                                side=resolved["side"],
                                quantity=resolved["quantity"],
                                mark_price=resolved["mark_price"],
                                ownership_role="HEDGE",
                            )
                        ]
                        rec_target_h_id = resolved["position_id"]
            elif action == "REMOVE_HEDGE":
                rec_target_h_id = None
            else:
                rec_target_h_id = hedge_pos_id

            reconciled_state: HedgeState | None = build_hedge_state(
                rec_legs,
                main_position_id=main_pos_id,
                hedge_position_id=rec_target_h_id,
                as_of_ms=reconciled_snapshot.as_of_ms,
            )
        except Exception:
            reconciled_state = None
        return reconciled_snapshot, reconciled_state

    async def _read_fresh_hedge(
        self,
        *,
        symbol: str,
        binding: dict[str, Any],
        persisted_main_id: str | None,
        persisted_hedge_id: str | None,
        plan_main_position_id: str | None,
        hedge_pos_id: str | None,
        action: str,
    ) -> tuple[Any, Any, Any, Any, Any, Any, list, HedgeState | None, str | None]:
        """One fresh pre-write read; unresolved structure returns a reason.

        Deterministic rejections (stale snapshot, margin, mode, identity)
        raise immediately and are never retried; only an unresolvable
        structure comes back for a bounded re-read.
        """
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
        equity = _decimal(state.equity, "equity")
        margin = _decimal(state.available_margin, "available_margin", positive=False)
        mark = _decimal(state.mark_price, "mark_price")
        gross = _decimal(state.gross_exposure, "gross_exposure", positive=False)

        hedge_mode = getattr(state, "position_mode", None)
        if not hedge_mode and hasattr(self.execution, "get_position_mode"):
            try:
                hedge_mode = await self.execution.get_position_mode()
            except Exception as exc:
                raise GMRejected(
                    f"cannot confirm HEDGE position mode: {exc}"
                ) from exc
        if hedge_mode != "HEDGE":
            raise GMRejected(
                "account/connector is not confirmed in HEDGE position mode"
            )

        main_pos_id = (
            persisted_main_id or plan_main_position_id or state.main_position_id
        )
        if not main_pos_id:
            raise GMRejected("no authoritative MAIN position ID")

        if state.positions is not None:
            legs = state.positions
        else:
            legs = []
            if state.main_position_id and state.main_quantity > 0:
                legs.append(
                    PositionLeg(
                        position_id=state.main_position_id,
                        symbol=symbol,
                        side=state.main_side or binding.get("main_side"),
                        quantity=str(state.main_quantity),
                        mark_price=str(state.mark_price),
                        ownership_role="MAIN",
                    )
                )
            h_id = getattr(state, "hedge_position_id", None) or persisted_hedge_id
            h_qty = getattr(state, "hedge_quantity", Decimal(0))
            if h_id and h_qty > 0:
                h_side = getattr(state, "hedge_side", None)
                if not h_side:
                    main_s = state.main_side or binding.get("main_side")
                    h_side = "SHORT" if main_s == "LONG" else "LONG"
                h_mark = (
                    getattr(state, "hedge_mark_price", None) or state.mark_price
                )
                legs.append(
                    PositionLeg(
                        position_id=h_id,
                        symbol=symbol,
                        side=h_side,
                        quantity=str(h_qty),
                        mark_price=str(h_mark),
                        ownership_role="HEDGE",
                    )
                )

        try:
            fresh_state = build_hedge_state(
                legs,
                main_position_id=main_pos_id,
                hedge_position_id=hedge_pos_id,
                as_of_ms=state.as_of_ms,
            )
        except HedgeBlocked as exc:
            return (
                state,
                equity,
                margin,
                mark,
                gross,
                main_pos_id,
                legs,
                None,
                f"cannot rebuild hedge state: {exc}",
            )
        if fresh_state.unresolved:
            return (
                state,
                equity,
                margin,
                mark,
                gross,
                main_pos_id,
                legs,
                None,
                f"hedge structure unresolved: {fresh_state.structure_status}",
            )
        return (
            state,
            equity,
            margin,
            mark,
            gross,
            main_pos_id,
            legs,
            fresh_state,
            None,
        )

    async def _execute_hedge(
        self,
        *,
        correlation_id: str,
        decision_id: str,
        action: str,
        target_hedge_ratio: str | Decimal | None,
        expected_state: HedgeState | dict[str, Any] | None,
        plan_main_position_id: str | None,
        plan_hedge_position_id: str | None,
    ) -> dict[str, Any]:
        if target_hedge_ratio is None:
            raise GMRejected("target_hedge_ratio is required for hedge action")
        if not decision_id or not all(c.isalnum() or c in "-_" for c in decision_id):
            raise GMRejected("unsafe decision_id")

        trade_dir = self._trade_dir(correlation_id)
        binding_path = trade_dir / "binding.json"
        if not binding_path.exists():
            raise GMRejected("MAIN binding is missing")
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        symbol = binding.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise GMRejected("MAIN binding has no symbol")

        lock, fd = await self._locked(symbol)
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if binding.get("status") == "reconciliation_required":
                raise GMRejected(
                    "trade is in reconciliation_required state; reconcile before retry"
                )
            if binding.get("status") not in (
                "submitted",
                "reconciled",
            ) or not binding.get("main_executor_id"):
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

            mgmt_dir = trade_dir / "management"
            if mgmt_dir.exists():
                for prev_file in mgmt_dir.glob("*.json"):
                    try:
                        prev_rec = json.loads(prev_file.read_text(encoding="utf-8"))
                        if prev_rec.get("status") in (
                            "submitting",
                            "reconciliation_required",
                        ):
                            raise GMRejected(
                                "prior management decision is unreconciled; reconcile before retry"
                            )
                    except (json.JSONDecodeError, OSError):
                        pass

            persisted_main_id = binding.get("main_position_id")
            persisted_hedge_id = binding.get("hedge_position_id")

            if (
                plan_main_position_id
                and persisted_main_id
                and plan_main_position_id != persisted_main_id
            ):
                raise GMRejected("plan main_position_id does not match binding")

            if action == "HEDGE":
                if persisted_hedge_id:
                    raise GMRejected(
                        "HEDGE requested but hedge position already exists; use INCREASE_HEDGE"
                    )
                hedge_pos_id = None
            else:
                if not persisted_hedge_id and not plan_hedge_position_id:
                    raise GMRejected(
                        f"{action} requested but no hedge position is bound; use HEDGE first"
                    )
                if (
                    plan_hedge_position_id
                    and persisted_hedge_id
                    and plan_hedge_position_id != persisted_hedge_id
                ):
                    raise GMRejected("plan hedge_position_id does not match binding")
                hedge_pos_id = persisted_hedge_id or plan_hedge_position_id

            state = None
            equity = margin = mark = gross = main_pos_id = None
            legs: list[Any] = []
            fresh_state: HedgeState | None = None
            fail_reason = "hedge structure unresolved"
            for _fresh_attempt in range(_HEDGE_FRESH_READS):
                (
                    state,
                    equity,
                    margin,
                    mark,
                    gross,
                    main_pos_id,
                    legs,
                    fresh_state,
                    fail_reason,
                ) = await self._read_fresh_hedge(
                    symbol=symbol,
                    binding=binding,
                    persisted_main_id=persisted_main_id,
                    persisted_hedge_id=persisted_hedge_id,
                    plan_main_position_id=plan_main_position_id,
                    hedge_pos_id=hedge_pos_id,
                    action=action,
                )
                if fail_reason is None:
                    break
                log.info(
                    "Brooks hedge fresh action=%s attempt=%d reason=%s",
                    action,
                    _fresh_attempt,
                    fail_reason,
                )
                if _fresh_attempt >= _HEDGE_FRESH_READS - 1:
                    raise GMRejected(fail_reason)
                await asyncio.sleep(_HEDGE_FRESH_DELAY_SEC)

            resolved_expected = expected_state
            if resolved_expected is None:
                saved_path = trade_dir / "hedge_state.json"
                if saved_path.exists():
                    try:
                        resolved_expected = HedgeState(
                            **json.loads(saved_path.read_text(encoding="utf-8"))
                        )
                    except Exception as exc:
                        raise GMRejected(
                            f"cannot load saved hedge state: {exc}"
                        ) from exc
                else:
                    # Consumer-routed first HEDGE carries no expected state and
                    # no file exists yet. Treat the just-read snapshot as the
                    # decision basis, stamped strictly older so the freshness
                    # guard below still compares two distinct instants; an
                    # unreadable or ambiguous snapshot keeps failing closed.
                    try:
                        resolved_expected = build_hedge_state(
                            legs,
                            main_position_id=main_pos_id,
                            hedge_position_id=hedge_pos_id,
                            as_of_ms=state.as_of_ms - 1,
                        )
                    except HedgeBlocked as exc:
                        raise GMRejected(f"cannot rebuild hedge state: {exc}") from exc
                    if resolved_expected.unresolved:
                        raise GMRejected(
                            "hedge structure unresolved: "
                            f"{resolved_expected.structure_status}"
                        )
            elif isinstance(resolved_expected, dict):
                resolved_expected = HedgeState(**resolved_expected)

            pending_orders = getattr(state, "pending_orders", False)
            try:
                command = compile_hedge_action(
                    action,  # type: ignore
                    target_hedge_ratio=str(target_hedge_ratio),
                    plan_main_position_id=main_pos_id,
                    plan_hedge_position_id=hedge_pos_id,
                    expected_state=resolved_expected,
                    fresh_state=fresh_state,
                    hedge_mode_confirmed=True,
                    pending_order=pending_orders,
                    quantity_increment=(
                        str(state.rules.amount_step)
                        if state.rules.amount_step
                        else None
                    ),
                    minimum_quantity=(
                        str(state.rules.min_amount) if state.rules.min_amount else None
                    ),
                )
            except HedgeBlocked as exc:
                raise GMRejected(f"hedge compilation rejected: {exc}") from exc

            cmd_qty = _decimal(command.quantity, "command.quantity", positive=True)
            mark_p = _decimal(
                fresh_state.hedge_mark_price
                or fresh_state.mark_price
                or state.mark_price,
                "mark_price",
                positive=True,
            )
            delta_notional = cmd_qty * mark_p

            if command.position_action == "OPEN":
                if delta_notional < state.rules.min_notional:
                    raise GMRejected(
                        "hedge delta notional is below venue minimum notional"
                    )
                if (
                    state.rules.max_leverage is not None
                    and self.policy.leverage > state.rules.max_leverage
                ):
                    raise GMRejected("leverage exceeds venue rule")
                margin_required = delta_notional / Decimal(self.policy.leverage)
                if margin_required > margin:
                    raise GMRejected("available margin is insufficient for hedge")
                gross_pct = _decimal(
                    self.policy.max_gross_exposure_pct, "max_gross_exposure_pct"
                )
                if gross + delta_notional > equity * gross_pct:
                    raise GMRejected("gross exposure cap exceeded")
            else:
                if (
                    action == "REDUCE_HEDGE"
                    and delta_notional < state.rules.min_notional
                ):
                    raise GMRejected(
                        "hedge reduction notional is below venue minimum notional"
                    )

            record = {
                "decision_id": decision_id,
                "action": action,
                "target_hedge_ratio": str(target_hedge_ratio),
                "quantity": command.quantity,
                "side": command.side,
                "position_action": command.position_action,
                "status": "submitting",
                "created_at_ms": int(time.time() * 1000),
            }
            self._write_new(record_path, record)

            executor_id = None
            write_error = None
            outcome: Literal["succeeded", "failed", "unknown"] = "succeeded"
            try:
                executor_id = await self.execution.execute_hedge(
                    symbol=symbol,
                    side=command.side,
                    quantity=cmd_qty,
                    position_action=command.position_action,
                    leverage=self.policy.leverage,
                )
            except ExecutionRejected as exc:
                outcome = "failed"
                write_error = exc
            except Exception as exc:
                outcome = "unknown"
                write_error = exc

            reconciled_snapshot, reconciled_state = await self._read_hedge_state(
                symbol=symbol,
                binding=binding,
                main_pos_id=main_pos_id,
                hedge_pos_id=hedge_pos_id,
                action=action,
                fresh_state=fresh_state,
                hedge_executor_id=executor_id,
            )
            filled_qty_str = _hedge_filled_quantity(
                reconciled_snapshot, reconciled_state, fresh_state, outcome
            )
            assessment = assess_hedge_result(
                command,
                outcome=outcome,
                filled_quantity=filled_qty_str,
                reconciled_state=reconciled_state,
            )
            log.info(
                "Brooks hedge assess action=%s attempt=%d outcome=%s requested=%s "
                "filled=%s fresh=%s rec=%s status=%s reason=%s",
                action,
                0,
                outcome,
                command.quantity,
                filled_qty_str,
                getattr(fresh_state, "hedge_size", "?"),
                getattr(reconciled_state, "hedge_size", None),
                assessment.status,
                assessment.reason,
            )
            corroborations = 0
            while assessment.status == "ambiguous" and (
                corroborations < _HEDGE_RECORROBORATE_READS
            ):
                corroborations += 1
                await asyncio.sleep(_HEDGE_RECORROBORATE_DELAY_SEC)
                reconciled_snapshot, reconciled_state = (
                    await self._read_hedge_state(
                        symbol=symbol,
                        binding=binding,
                        main_pos_id=main_pos_id,
                        hedge_pos_id=hedge_pos_id,
                        action=action,
                        fresh_state=fresh_state,
                        hedge_executor_id=executor_id,
                    )
                )
                filled_qty_str = _hedge_filled_quantity(
                    reconciled_snapshot, reconciled_state, fresh_state, outcome
                )
                assessment = assess_hedge_result(
                    command,
                    outcome=outcome,
                    filled_quantity=filled_qty_str,
                    reconciled_state=reconciled_state,
                )
                log.info(
                    "Brooks hedge assess action=%s attempt=%d outcome=%s requested=%s "
                    "filled=%s fresh=%s rec=%s status=%s reason=%s",
                    action,
                    corroborations,
                    outcome,
                    command.quantity,
                    filled_qty_str,
                    getattr(fresh_state, "hedge_size", "?"),
                    getattr(reconciled_state, "hedge_size", None),
                    assessment.status,
                    assessment.reason,
                )

            if assessment.status == "confirmed":
                record.update(
                    status="submitted",
                    executor_id=executor_id,
                    filled_quantity=filled_qty_str,
                    assessment="confirmed",
                )
                self._replace(record_path, record)
                self._replace(trade_dir / "hedge_state.json", asdict(reconciled_state))

                if action in ("HEDGE", "INCREASE_HEDGE"):
                    if reconciled_state.hedge_position_id:
                        binding["hedge_position_id"] = (
                            reconciled_state.hedge_position_id
                        )
                    # The bound executor stays the leg's creator: later writers
                    # rotate through management records, so the persisted pair
                    # keeps resolving. Overwriting it here wedged every later
                    # step on id-less venues (tag pointed at the creator leg
                    # while the id pointed at the latest writer).
                    if action == "HEDGE" and executor_id:
                        binding["hedge_executor_id"] = executor_id
                    binding["hedge_size"] = reconciled_state.hedge_size
                elif action == "REMOVE_HEDGE":
                    binding["hedge_position_id"] = None
                    binding["hedge_executor_id"] = None
                    binding["hedge_size"] = "0"
                elif action == "REDUCE_HEDGE":
                    binding["hedge_size"] = reconciled_state.hedge_size
                if (
                    not binding.get("main_position_id")
                    and reconciled_state.main_position_id
                ):
                    binding["main_position_id"] = reconciled_state.main_position_id

                self._replace(binding_path, binding)

                execution_entry = {
                    "decision_id": decision_id,
                    "action": action,
                    "status": "confirmed",
                    "executor_id": executor_id,
                    "quantity": command.quantity,
                    "filled_quantity": filled_qty_str,
                    "target_hedge_ratio": command.target_hedge_ratio,
                    "timestamp_ms": int(time.time() * 1000),
                }
                self._append_execution(trade_dir / "executions.jsonl", execution_entry)
                return record

            if assessment.status == "failed":
                record.update(
                    status="failed",
                    reason=assessment.reason,
                    error=str(write_error) if write_error else None,
                )
                self._replace(record_path, record)
                raise GMRejected(f"hedge execution failed: {assessment.reason}")

            # status in ("partial", "ambiguous")
            record.update(
                status="reconciliation_required",
                assessment_status=assessment.status,
                reason=assessment.reason,
                executor_id=executor_id,
                filled_quantity=filled_qty_str,
                error=str(write_error) if write_error else None,
            )
            self._replace(record_path, record)
            binding["status"] = "reconciliation_required"
            self._replace(binding_path, binding)
            if write_error and isinstance(write_error, (TimeoutError, ConnectionError)):
                raise write_error
            raise GMRejected(
                f"hedge execution requires reconciliation ({assessment.status}): {assessment.reason}"
            )
        finally:
            self._unlock(lock, fd)
