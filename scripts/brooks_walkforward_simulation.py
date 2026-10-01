"""Offline venue adapter for the Brooks historical walk-forward runner.

This module gives the production Brooks GM, account reader, position reconciler
and PM context loader a small in-memory Hummingbot-shaped venue. Writes only
mutate this adapter. Main entry writes use the same MARKET semantics as the
current :class:`HummingbotExecutionPort`; the LLM's trigger description is
retained for audit, while subsequent 1m OHLC bars resolve the executor's stop,
target and time limit.

The runner owns historical data and role scheduling. Call ``set_market`` with
each newly available 1m bar before invoking production GM/PM code, then call
``resolve_executor_bar`` once for each bar while an executor is open.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from typing import Any, Callable

from condor.brooks.adapters import (
    HummingbotAccountReader,
    HummingbotPositionReconciler,
    build_pm_load_context,
)
from condor.brooks.execution import ExecutionRejected
from scripts.brooks_long_lock_policy import projected_operation_exit_net


_D0 = Decimal(0)
_D1 = Decimal(1)
_BPS = Decimal(10_000)


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{name} must be {'positive and ' if positive else ''}finite")
    return number


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json")
        if isinstance(result, dict):
            return result
    raise TypeError("expected a mapping or Pydantic model")


def _side_sign(side: str) -> Decimal:
    normalized = side.upper()
    if normalized in {"LONG", "BUY", "BID"}:
        return _D1
    if normalized in {"SHORT", "SELL", "ASK"}:
        return -_D1
    raise ValueError(f"unsupported position side: {side!r}")


@dataclass
class _Position:
    position_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    role: str
    opened_at_ms: int
    executor_id: str
    correlation_id: str | None
    original_quantity: Decimal
    leverage: int
    realized_gross: Decimal = _D0
    entry_fees: Decimal = _D0
    exit_fees: Decimal = _D0
    slippage_cost: Decimal = _D0
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    time_limit_sec: int | None = None


@dataclass
class _Trade:
    correlation_id: str
    symbol: str
    side: str
    opened_at_ms: int
    main_position_id: str
    main_executor_id: str
    entry_price: Decimal
    initial_quantity: Decimal
    initial_risk_usd: Decimal
    stop_price: Decimal
    target_price: Decimal
    time_limit_sec: int
    trigger_status: str | None = None
    trigger_kind: str | None = None
    submitted_order_type: str = "MARKET"
    submission_mark: Decimal = _D0
    realized_gross_pnl: Decimal = _D0
    fees: Decimal = _D0
    slippage_cost: Decimal = _D0
    max_favorable_price: Decimal | None = None
    max_adverse_price: Decimal | None = None
    max_favorable_r: Decimal = _D0
    max_adverse_r: Decimal = _D0
    close_reason: str | None = None
    closed_at_ms: int | None = None
    exit_price: Decimal | None = None
    ambiguous_bar_count: int = 0
    partial_entry_bar_count: int = 0
    management_actions: list[dict[str, Any]] = field(default_factory=list)
    protection_limit: Decimal | None = None
    protection_armed: bool = True
    last_lock_proof: dict[str, Any] | None = None
    duration_expires_at_ms: int | None = None
    duration_notified_at_ms: int | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at_ms is None


class _MarketData:
    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner

    async def get_prices(self, connector_name: str, trading_pairs: list[str]) -> dict:
        return {
            "prices": {
                symbol: float(self._owner.mark_price)
                for symbol in trading_pairs
                if symbol == self._owner.symbol
            }
        }


class _Trading:
    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner

    async def get_positions(
        self, account_names=None, connector_names=None, limit=1000
    ) -> dict:
        rows = [
            {
                "position_id": pos.position_id,
                "trading_pair": pos.symbol,
                "position_side": pos.side,
                "net_amount_base": str(pos.quantity),
                "entry_price": str(pos.entry_price),
                "current_price": str(self._owner.mark_price),
                "unrealized_pnl": str(self._owner._unrealized_for(pos)),
            }
            for pos in self._owner._positions.values()
            if pos.quantity > 0
        ]
        return {"data": rows[:limit]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ) -> dict:
        return {"data": []}

    async def get_position_mode(self, account_name: str, connector_name: str) -> dict:
        return {"position_mode": "HEDGE"}

    async def search_orders(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        status=None,
        start_time=None,
        end_time=None,
        limit=50,
    ) -> dict:
        rows = self._owner.fills
        if trading_pairs:
            rows = [row for row in rows if row.get("trading_pair") in trading_pairs]
        if status:
            values = [status] if isinstance(status, str) else status
            wanted = {str(value).upper() for value in values}
            rows = [row for row in rows if str(row.get("status", "")).upper() in wanted]
        if start_time is not None:
            rows = [row for row in rows if row["timestamp_ms"] >= start_time * 1000]
        if end_time is not None:
            rows = [row for row in rows if row["timestamp_ms"] // 1000 <= end_time]
        return {"data": [dict(row) for row in rows[-limit:]]}


class _Executors:
    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner

    async def search_executors(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        controller_ids=None,
        limit=1000,
    ) -> dict:
        rows = self._owner.executor_rows
        if trading_pairs:
            rows = [row for row in rows if row.get("trading_pair") in trading_pairs]
        if controller_ids:
            rows = [row for row in rows if row.get("controller_id") in controller_ids]
        return {"data": [dict(row) for row in rows[-limit:]]}

    async def get_positions_summary(self, controller_id=None) -> dict:
        rows = self._owner.holds
        if controller_id:
            rows = [row for row in rows if row.get("controller_id") == controller_id]
        return {"positions": [dict(row) for row in rows]}

    async def get_executor(self, executor_id: str) -> dict | None:
        for row in self._owner.executor_rows:
            if row.get("executor_id") == executor_id:
                return dict(row)
        return None


class _Portfolio:
    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner

    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ) -> dict:
        equity = self._owner.marked_equity
        margin = self._owner.used_margin
        return {
            self._owner.account_name: {
                self._owner.connector_name: [
                    {
                        "token": "USDT",
                        "units": str(equity),
                        "available_units": str(max(_D0, equity - margin)),
                        "price": "1",
                        "value": str(equity),
                    }
                ]
            }
        }


class _Connectors:
    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner

    async def get_trading_rules(self, connector_name: str, trading_pairs=None) -> dict:
        return {
            "trading_rules": {
                self._owner.symbol: {
                    "min_base_amount_increment": str(self._owner.amount_step),
                    "min_order_size": str(self._owner.min_amount),
                    "min_notional": str(self._owner.min_notional),
                    "max_leverage": self._owner.max_leverage,
                }
            }
        }


class _SimClient:
    """Read-only Hummingbot-shaped facade used by production read adapters."""

    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self.market_data = _MarketData(owner)
        self.trading = _Trading(owner)
        self.executors = _Executors(owner)
        self.portfolio = _Portfolio(owner)
        self.connectors = _Connectors(owner)


class WalkForwardExecutionPort:
    """Immediate-fill ExecutionPort whose writes remain inside the simulation."""

    def __init__(self, owner: "WalkForwardVenueAdapter") -> None:
        self._owner = owner
        self.controller_id = owner.controller_id

    async def get_position_mode(self) -> str:
        return "HEDGE"

    async def open_main(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        leverage: int,
        stop_loss_pct: Decimal,
        take_profit_pct: Decimal,
        time_limit_sec: int,
    ) -> str:
        owner = self._owner
        if symbol != owner.symbol or owner._staged_intent is None:
            raise ExecutionRejected("simulated MAIN requires a staged Trader intent")
        intent, correlation_id = owner._staged_intent
        if any(pos.role == "MAIN" and pos.quantity > 0 for pos in owner._positions.values()):
            raise ExecutionRejected("simulated venue already has a MAIN position")
        if side not in ("LONG", "SHORT") or leverage < 1 or time_limit_sec < 1:
            raise ExecutionRejected("invalid simulated MAIN order")
        quantity = _decimal(quantity, "quantity", positive=True)
        stop_loss_pct = _decimal(stop_loss_pct, "stop_loss_pct", positive=True)
        take_profit_pct = _decimal(take_profit_pct, "take_profit_pct", positive=True)
        market = owner.mark_price
        sign = _side_sign(side)
        fill = owner._entry_fill(market, sign)
        exec_id = owner._next_executor_id()
        pos_id = owner._next_position_id("main")
        stop_price = market * (_D1 - stop_loss_pct if sign > 0 else _D1 + stop_loss_pct)
        target_price = market * (_D1 + take_profit_pct if sign > 0 else _D1 - take_profit_pct)
        entry_fee = abs(fill * quantity) * owner.taker_fee_rate
        slip_cost = abs(fill - market) * quantity
        pos = _Position(
            position_id=pos_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=fill,
            role="MAIN",
            opened_at_ms=owner.now_ms,
            executor_id=exec_id,
            correlation_id=correlation_id,
            original_quantity=quantity,
            leverage=leverage,
            entry_fees=entry_fee,
            slippage_cost=slip_cost,
            stop_price=stop_price,
            target_price=target_price,
            time_limit_sec=time_limit_sec,
        )
        owner._positions[pos_id] = pos
        owner._add_executor(exec_id, symbol, status="RUNNING", position_id=pos_id)
        owner.executor_rows[-1]["config"] = {
            "stop_price": str(stop_price),
            "target_price": str(target_price),
        }
        owner._add_hold(exec_id, symbol, side, quantity)
        owner._charge_fee(entry_fee)
        owner._slippage += slip_cost
        owner._record_fill(
            exec_id,
            symbol,
            side,
            "OPEN",
            quantity,
            fill,
            "MAIN_OPEN",
            correlation_id,
            position_id=pos_id,
        )
        trade = _Trade(
            correlation_id=correlation_id,
            symbol=symbol,
            side=side,
            opened_at_ms=owner.now_ms,
            main_position_id=pos_id,
            main_executor_id=exec_id,
            entry_price=fill,
            initial_quantity=quantity,
            initial_risk_usd=quantity * abs(market - stop_price),
            stop_price=stop_price,
            target_price=target_price,
            time_limit_sec=time_limit_sec,
            trigger_status=(intent.get("setup") or {}).get("trigger_status"),
            trigger_kind=(intent.get("trigger") or {}).get("kind"),
            submission_mark=market,
            fees=entry_fee,
            slippage_cost=slip_cost,
            max_favorable_price=fill,
            max_adverse_price=fill,
            protection_limit=stop_price if owner.long_lock_policy and side == "LONG" else None,
            duration_expires_at_ms=(owner.now_ms + time_limit_sec * 1000)
                if owner.long_lock_policy and side == "LONG" else None,
        )
        trade.management_actions.append(
            {
                "at_ms": owner.now_ms,
                "action": "OPEN_MAIN",
                "order_type": "MARKET",
                "trigger_status": trade.trigger_status,
                "trigger_kind": trade.trigger_kind,
                "mark_at_submission": str(market),
                "fill_price": str(fill),
                "quantity": str(quantity),
            }
        )
        owner.trades.append(trade)
        owner.active_correlation_id = correlation_id
        owner._mark_entry_submission(
            correlation_id, status="filled_market", executor_id=exec_id,
            fill_price=fill, mark=market
        )
        owner._staged_intent = None
        owner._record_equity(event="main_open")
        return exec_id

    async def reduce_main(
        self, *, symbol: str, side: str, quantity: Decimal, leverage: int
    ) -> str:
        if leverage < 1:
            raise ExecutionRejected("invalid simulated leverage")
        return self._owner._close_main_quantity(
            symbol=symbol, side=side, quantity=_decimal(quantity, "quantity", positive=True),
            reason="PM_REDUCE", executor_status="RUNNING"
        )

    async def close_main(self, *, executor_id: str) -> str:
        owner = self._owner
        pos = next(
            (
                item
                for item in owner._positions.values()
                if item.role == "MAIN" and item.executor_id == executor_id and item.quantity > 0
            ),
            None,
        )
        if pos is None:
            raise ExecutionRejected("simulated MAIN executor has no open position")
        owner._close_position(pos, pos.quantity, reason="PM_CLOSE")
        owner._set_executor_status(executor_id, "CLOSED")
        owner._record_equity(event="pm_close")
        return executor_id

    async def execute_hedge(
        self,
        *,
        symbol: str,
        side: str | int,
        quantity: Decimal | str,
        position_action: str,
        leverage: int,
    ) -> str:
        if leverage < 1 or symbol != self._owner.symbol:
            raise ExecutionRejected("invalid simulated hedge order")
        if position_action not in ("OPEN", "CLOSE"):
            raise ExecutionRejected("invalid simulated hedge action")
        normalized_side = {1: "BUY", 2: "SELL"}.get(side, side)
        if normalized_side not in ("BUY", "SELL", "LONG", "SHORT"):
            raise ExecutionRejected("invalid simulated hedge side")
        sign = _side_sign(str(normalized_side))
        quantity = _decimal(quantity, "quantity", positive=True)
        owner = self._owner
        exec_id = owner._next_executor_id()
        if position_action == "OPEN":
            hedge_side = "LONG" if sign > 0 else "SHORT"
            hedge = next(
                (item for item in owner._positions.values() if item.role == "HEDGE" and item.quantity > 0),
                None,
            )
            if hedge is not None and hedge.side != hedge_side:
                raise ExecutionRejected("simulated hedge direction conflicts with open leg")
            market = owner.mark_price
            fill = owner._entry_fill(market, sign)
            fee = abs(fill * quantity) * owner.taker_fee_rate
            slip = abs(fill - market) * quantity
            if hedge is None:
                pos_id = owner._next_position_id("hedge")
                hedge = _Position(
                    position_id=pos_id,
                    symbol=symbol,
                    side=hedge_side,
                    quantity=quantity,
                    entry_price=fill,
                    role="HEDGE",
                    opened_at_ms=owner.now_ms,
                    executor_id=exec_id,
                    correlation_id=owner.active_correlation_id,
                    original_quantity=quantity,
                    leverage=leverage,
                    entry_fees=fee,
                    slippage_cost=slip,
                )
                owner._positions[pos_id] = hedge
            else:
                total = hedge.quantity + quantity
                hedge.entry_price = (hedge.entry_price * hedge.quantity + fill * quantity) / total
                hedge.quantity = total
                hedge.original_quantity += quantity
                hedge.entry_fees += fee
                hedge.slippage_cost += slip
            # A filled order executor is terminal; unlike a MAIN position
            # executor it does not manage an ongoing triple barrier.
            owner._add_executor(
                exec_id, symbol, status="TERMINATED", position_id=hedge.position_id
            )
            owner._add_hold(exec_id, symbol, hedge.side, quantity)
            owner._charge_fee(fee)
            owner._slippage += slip
            trade = owner._trade_for(owner.active_correlation_id)
            if trade is not None:
                trade.fees += fee
                trade.slippage_cost += slip
            owner._record_fill(
                exec_id,
                symbol,
                hedge_side,
                "OPEN",
                quantity,
                fill,
                "HEDGE_OPEN",
                owner.active_correlation_id,
                position_id=hedge.position_id,
            )
            owner._attribute_management(
                "INCREASE_HEDGE" if hedge.quantity != quantity else "HEDGE",
                exec_id, quantity, fill
            )
        else:
            hedge = next(
                (item for item in owner._positions.values() if item.role == "HEDGE" and item.quantity > 0),
                None,
            )
            if hedge is None:
                raise ExecutionRejected("simulated hedge close has no open hedge")
            # The hedge leg is closed by GM with the opposite market side.
            action = "REMOVE_HEDGE" if quantity == hedge.quantity else "REDUCE_HEDGE"
            owner._close_position(hedge, quantity, reason=action, executor_id=exec_id)
            owner._add_executor(exec_id, symbol, status="CLOSED", position_id=hedge.position_id)
            owner._add_hold(exec_id, symbol, hedge.side, quantity)
        owner._record_equity(event="hedge_write")
        if owner.on_hedge_write is not None:
            owner.on_hedge_write()
        return exec_id


class WalkForwardVenueAdapter:
    """A single-symbol offline venue connected to production Brooks adapters.

    Args mirror venue/risk facts rather than agent behavior. The historical
    bar source remains an input owned by the main runner and can be attached as
    ``candle_source`` for the production PM's read tools.
    """

    def __init__(
        self,
        symbol: str,
        *,
        state_root: Path | str,
        initial_equity: Decimal | str | int | float = 10_000,
        starting_mark: Decimal | str | int | float = 1,
        amount_step: Decimal | str | int | float = "0.001",
        min_amount: Decimal | str | int | float = "0.001",
        min_notional: Decimal | str | int | float = 5,
        max_leverage: int = 20,
        taker_fee_rate: Decimal | str | int | float = "0.0004",
        slippage_bps: Decimal | str | int | float = 1,
        account_name: str = "walkforward",
        connector_name: str = "binance_perpetual",
        controller_id: str = "brooks-walkforward",
        candle_source: Any | None = None,
        now_fn: Any | None = None,
        long_lock_policy: bool = False,
        on_hedge_write: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if not all((account_name.strip(), connector_name.strip(), controller_id.strip())):
            raise ValueError("account, connector and controller are required")
        if isinstance(max_leverage, bool) or not isinstance(max_leverage, int) or max_leverage < 1:
            raise ValueError("max_leverage must be a positive integer")
        self.symbol = symbol
        self.state_root = Path(state_root)
        self.initial_equity = _decimal(initial_equity, "initial_equity", positive=True)
        self.mark_price = _decimal(starting_mark, "starting_mark", positive=True)
        self.amount_step = _decimal(amount_step, "amount_step", positive=True)
        self.min_amount = _decimal(min_amount, "min_amount", positive=True)
        self.min_notional = _decimal(min_notional, "min_notional", positive=True)
        self.max_leverage = max_leverage
        self.taker_fee_rate = _decimal(taker_fee_rate, "taker_fee_rate")
        self.slippage_bps = _decimal(slippage_bps, "slippage_bps")
        if self.taker_fee_rate < 0 or self.taker_fee_rate >= 1:
            raise ValueError("taker_fee_rate must be in [0, 1)")
        if self.slippage_bps < 0 or self.slippage_bps >= _BPS:
            raise ValueError("slippage_bps must be in [0, 10000)")
        self.account_name = account_name
        self.connector_name = connector_name
        self.controller_id = controller_id
        self.candle_source = candle_source
        self.long_lock_policy = bool(long_lock_policy)
        self.on_hedge_write = on_hedge_write
        self.long_lock_requests: list[dict[str, Any]] = []
        self.duration_requests: list[dict[str, Any]] = []
        self._now_fn = now_fn
        self._time_ms = 0
        self.current_bar: dict[str, Any] | None = None
        self.last_resolved_bar_close_ms: int | None = None
        self._positions: dict[str, _Position] = {}
        self._realized_gross = _D0
        self._fees = _D0
        self._slippage = _D0
        self._exec_seq = 0
        self._position_seq = 0
        self.executor_rows: list[dict[str, Any]] = []
        self.holds: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.trades: list[_Trade] = []
        self.entry_submissions: list[dict[str, Any]] = []
        self.pending_intents: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.active_correlation_id: str | None = None
        self._staged_intent: tuple[dict[str, Any], str] | None = None
        self.client = _SimClient(self)
        self.execution = WalkForwardExecutionPort(self)
        self.reader = HummingbotAccountReader(
            self.client,
            self.state_root,
            self.controller_id,
            now_fn=self._clock_ms,
        )
        self.reconciler = HummingbotPositionReconciler(self.client, self.controller_id)
        self.pm_load_context = build_pm_load_context(
            self.client,
            account_name=self.account_name,
            connector_name=self.connector_name,
            controller_id=self.controller_id,
            state_root=self.state_root,
            now_fn=self._clock_ms,
        )

    @property
    def now_ms(self) -> int:
        if self._now_fn is None:
            return self._time_ms
        value = self._now_fn()
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("simulation clock must return integer milliseconds")
        return value

    @now_ms.setter
    def now_ms(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("simulation timestamp must be integer milliseconds")
        self._time_ms = value

    def _clock_ms(self) -> int:
        return self.now_ms

    @property
    def realized_gross_pnl(self) -> Decimal:
        return self._realized_gross

    @property
    def fees_paid(self) -> Decimal:
        return self._fees

    @property
    def slippage_cost(self) -> Decimal:
        return self._slippage

    @property
    def costs(self) -> dict[str, str]:
        return {
            "taker_fees": str(self._fees),
            "slippage": str(self._slippage),
            "funding": "0",
            "total_reported_costs": str(self._fees + self._slippage),
        }

    @property
    def used_margin(self) -> Decimal:
        return sum(
            (abs(pos.quantity * self.mark_price) / Decimal(pos.leverage)
             for pos in self._positions.values() if pos.quantity > 0),
            _D0,
        )

    @property
    def gross_exposure(self) -> Decimal:
        return sum(
            (abs(pos.quantity * self.mark_price) for pos in self._positions.values() if pos.quantity > 0),
            _D0,
        )

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((self._unrealized_for(pos) for pos in self._positions.values()), _D0)

    @property
    def marked_equity(self) -> Decimal:
        return max(_D0, self.initial_equity + self._realized_gross - self._fees + self.unrealized_pnl)

    def set_market(
        self,
        bar: Mapping[str, Any] | None = None,
        *,
        decision_time_ms: int | None = None,
        mark_price: Decimal | str | int | float | None = None,
    ) -> None:
        """Advance only to a market value the replay has made available.

        ``mark_price`` supports a current-known close or explicit venue mark.
        It must never be a value derived from a not-yet-available bar extreme.
        """
        if bar is not None:
            self.current_bar = dict(bar)
            bar_time = bar.get("close_time_ms", bar.get("open_time_ms"))
            if bar_time is not None:
                self.now_ms = int(bar_time)
        if decision_time_ms is not None:
            if isinstance(decision_time_ms, bool) or not isinstance(decision_time_ms, int):
                raise ValueError("decision_time_ms must be an integer")
            self.now_ms = decision_time_ms
        if mark_price is not None:
            self.mark_price = _decimal(mark_price, "mark_price", positive=True)
        elif bar is not None and bar.get("close") is not None:
            self.mark_price = _decimal(bar["close"], "bar.close", positive=True)
        if self.now_ms <= 0:
            raise ValueError("a positive replay timestamp is required")

    def stage_trade_intent(self, correlation_id: str, intent: Any) -> None:
        """Make the exact real Trader output available to the next GM write."""
        if not correlation_id or not all(c.isalnum() or c in "-_" for c in correlation_id):
            raise ValueError("correlation_id is unsafe")
        trade = _mapping(intent)
        if trade.get("symbol") != self.symbol:
            raise ValueError("Trader intent symbol does not match simulation venue")
        self._staged_intent = (trade, correlation_id)
        setup = trade.get("setup") if isinstance(trade.get("setup"), dict) else {}
        self.entry_submissions.append(
            {
                "correlation_id": correlation_id,
                "decision_time_ms": trade.get("decision_time_ms"),
                "decision": trade.get("decision"),
                "trigger_status": setup.get("trigger_status"),
                "trigger_kind": (trade.get("trigger") or {}).get("kind"),
                "simulated_order_type": "MARKET",
                "status": "submitted_to_gm",
                "note": "Current Brooks ExecutionPort maps accepted entries to MARKET.",
            }
        )

    def clear_staged_trade_intent(self) -> None:
        self._staged_intent = None

    def mark_entry_rejected(self, correlation_id: str, reason: str) -> None:
        self._mark_entry_submission(correlation_id, status="gm_rejected", reason=reason)
        if self._staged_intent and self._staged_intent[1] == correlation_id:
            self._staged_intent = None

    def _mark_entry_submission(self, correlation_id: str, **updates: Any) -> None:
        for row in reversed(self.entry_submissions):
            if row.get("correlation_id") == correlation_id:
                row.update(updates)
                return

    def _next_executor_id(self) -> str:
        self._exec_seq += 1
        return f"wf-exec-{self._exec_seq}"

    def _next_position_id(self, role: str) -> str:
        self._position_seq += 1
        return f"wf-{role}-{self._position_seq}"

    def _entry_fill(self, market: Decimal, sign: Decimal) -> Decimal:
        return market * (_D1 + sign * self.slippage_bps / _BPS)

    def _exit_fill(self, market: Decimal, sign: Decimal) -> Decimal:
        # Closing a long sells and closing a short buys.
        return market * (_D1 - sign * self.slippage_bps / _BPS)

    def _add_executor(
        self, executor_id: str, symbol: str, *, status: str, position_id: str
    ) -> None:
        self.executor_rows.append(
            {
                "executor_id": executor_id,
                "controller_id": self.controller_id,
                "account_name": self.account_name,
                "connector_name": self.connector_name,
                "trading_pair": symbol,
                "status": status,
                "position_id": position_id,
            }
        )

    def _set_executor_status(self, executor_id: str, status: str) -> None:
        for row in self.executor_rows:
            if row.get("executor_id") == executor_id:
                row["status"] = status

    def _add_hold(self, executor_id: str, symbol: str, side: str, quantity: Decimal) -> None:
        self.holds.append(
            {
                "account_name": self.account_name,
                "connector_name": self.connector_name,
                "trading_pair": symbol,
                "position_side": side,
                "net_amount_base": float(quantity),
                "controller_id": self.controller_id,
                "executor_ids": [executor_id],
            }
        )

    def _charge_fee(self, fee: Decimal) -> None:
        self._fees += fee

    def _record_fill(
        self,
        executor_id: str,
        symbol: str,
        side: str,
        position_action: str,
        quantity: Decimal,
        price: Decimal,
        action: str,
        correlation_id: str | None,
        *,
        reason: str | None = None,
        position_id: str | None = None,
    ) -> None:
        row = {
            "client_order_id": f"wf-fill-{len(self.fills) + 1}",
            "order_id": f"wf-fill-{len(self.fills) + 1}",
            "executor_id": executor_id,
            "position_id": position_id,
            "trading_pair": symbol,
            "trade_type": (
                "BUY" if (side == "LONG") == (position_action == "OPEN") else "SELL"
            ),
            "position_action": position_action,
            "status": "FILLED",
            "filled_amount": str(quantity),
            "price": str(price),
            "fee": str(abs(price * quantity) * self.taker_fee_rate),
            "timestamp_ms": self.now_ms,
            "action": action,
            "correlation_id": correlation_id,
        }
        if reason:
            row["reason"] = reason
        self.fills.append(row)
        self.executions.append(dict(row))

    def _trade_for(self, correlation_id: str | None) -> _Trade | None:
        if correlation_id:
            for trade in reversed(self.trades):
                if trade.correlation_id == correlation_id:
                    return trade
        if self.active_correlation_id:
            for trade in reversed(self.trades):
                if trade.correlation_id == self.active_correlation_id:
                    return trade
        return self.trades[-1] if self.trades else None

    def _attribute_management(
        self, action: str, executor_id: str, quantity: Decimal, price: Decimal
    ) -> None:
        trade = self._trade_for(self.active_correlation_id)
        if trade is not None:
            trade.management_actions.append(
                {
                    "at_ms": self.now_ms,
                    "action": action,
                    "executor_id": executor_id,
                    "quantity": str(quantity),
                    "mark_or_fill_price": str(price),
                }
            )

    def _unrealized_for(self, pos: _Position) -> Decimal:
        return (self.mark_price - pos.entry_price) * pos.quantity * _side_sign(pos.side)

    def _main_position(self, symbol: str, side: str | None = None) -> _Position:
        for pos in self._positions.values():
            if pos.role == "MAIN" and pos.symbol == symbol and pos.quantity > 0:
                if side is None or pos.side == side:
                    return pos
        raise ExecutionRejected("simulated MAIN position is missing")

    def long_policy_context(self, correlation_id: str) -> dict[str, Any] | None:
        """Current operation liquidation estimate for the PM's bounded snapshot."""
        trade = self._trade_for(correlation_id)
        if trade is None:
            return None
        legs = [pos for pos in self._positions.values()
                if pos.correlation_id == correlation_id and pos.quantity > 0]
        main_quantity = sum((pos.quantity for pos in legs if pos.role == "MAIN"), _D0)
        hedge_quantity = sum((pos.quantity for pos in legs if pos.role == "HEDGE"), _D0)
        projected = projected_operation_exit_net(
            trade.realized_gross_pnl, trade.fees,
            [(pos.side, pos.quantity, pos.entry_price) for pos in legs],
            self.mark_price, self.taker_fee_rate, self.slippage_bps)
        return {
            "main_side": trade.side,
            "main_quantity": str(main_quantity),
            "hedge_quantity": str(hedge_quantity),
            "projected_exit_net": str(projected),
            "realized_net": str(trade.realized_gross_pnl - trade.fees),
            "protection_limit": str(trade.protection_limit) if trade.protection_limit is not None else None,
            "protection_armed": trade.protection_armed,
            "last_lock_proof": trade.last_lock_proof,
            "duration_expires_at_ms": trade.duration_expires_at_ms,
        }

    def rearm_long_protection(self, correlation_id: str, limit: Decimal | str | None = None) -> None:
        """Arm a new protection episode after the host has handled a lock request."""
        trade = self._trade_for(correlation_id)
        if not self.long_lock_policy or trade is None or trade.side != "LONG" or not trade.is_open:
            raise ValueError("no open locked LONG operation")
        if limit is not None:
            trade.protection_limit = _decimal(limit, "protection_limit", positive=True)
        trade.protection_armed = True

    def prolong_long_duration(self, correlation_id: str, expires_at_ms: int) -> None:
        trade = self._trade_for(correlation_id)
        if not self.long_lock_policy or trade is None or trade.side != "LONG" or not trade.is_open:
            raise ValueError("no open locked LONG operation")
        if isinstance(expires_at_ms, bool) or not isinstance(expires_at_ms, int) or expires_at_ms <= self.now_ms:
            raise ValueError("new duration expiry must be later than current simulation time")
        trade.duration_expires_at_ms = expires_at_ms
        trade.duration_notified_at_ms = None

    def _guard_main_long_exit(self, pos: _Position, quantity: Decimal) -> None:
        if not self.long_lock_policy or pos.role != "MAIN" or pos.side != "LONG":
            return
        trade = self._trade_for(pos.correlation_id)
        if trade is None:
            raise ExecutionRejected("locked LONG operation lacks trade record")
        context = self.long_policy_context(trade.correlation_id)
        assert context is not None
        if quantity == pos.quantity and Decimal(context["hedge_quantity"]) > 0:
            raise ExecutionRejected("locked LONG cannot close MAIN while hedge remains")
        if Decimal(context["projected_exit_net"]) < 0:
            raise ExecutionRejected("locked LONG projected operation exit is negative")

    def _close_main_quantity(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        reason: str,
        executor_status: str,
    ) -> str:
        pos = self._main_position(symbol, side)
        if quantity <= 0 or quantity > pos.quantity:
            raise ExecutionRejected("simulated MAIN close quantity is invalid")
        self._guard_main_long_exit(pos, quantity)
        exec_id = self._next_executor_id()
        self._add_executor(exec_id, symbol, status=executor_status, position_id=pos.position_id)
        self._add_hold(exec_id, symbol, side, quantity)
        self._close_position(pos, quantity, reason=reason, executor_id=exec_id)
        trade = self._trade_for(pos.correlation_id)
        if trade is not None:
            trade.management_actions.append(
                {
                    "at_ms": self.now_ms,
                    "action": reason,
                    "executor_id": exec_id,
                    "quantity": str(quantity),
                    "mark_price": str(self.mark_price),
                }
            )
        self._record_equity(event=reason.lower())
        return exec_id

    def _close_position(
        self,
        pos: _Position,
        quantity: Decimal,
        *,
        reason: str,
        executor_id: str | None = None,
    ) -> None:
        if quantity <= 0 or quantity > pos.quantity:
            raise ExecutionRejected("simulated close quantity exceeds open leg")
        self._guard_main_long_exit(pos, quantity)
        sign = _side_sign(pos.side)
        fill = self._exit_fill(self.mark_price, sign)
        gross = (fill - pos.entry_price) * quantity * sign
        fee = abs(fill * quantity) * self.taker_fee_rate
        slip = abs(fill - self.mark_price) * quantity
        pos.realized_gross += gross
        pos.exit_fees += fee
        pos.slippage_cost += slip
        self._realized_gross += gross
        self._charge_fee(fee)
        self._slippage += slip
        trade = self._trade_for(pos.correlation_id)
        if trade is not None:
            trade.realized_gross_pnl += gross
            trade.fees += fee
            trade.slippage_cost += slip
        pos.quantity -= quantity
        exec_id = executor_id or self._next_executor_id()
        self._record_fill(
            exec_id,
            pos.symbol,
            pos.side,
            "CLOSE",
            quantity,
            fill,
            reason,
            pos.correlation_id,
            reason=reason,
            position_id=pos.position_id,
        )
        if pos.quantity == 0:
            self._set_executor_status(pos.executor_id, "CLOSED")
            if pos.role == "MAIN":
                if trade is not None and not any(
                    other.role == "MAIN" and other.quantity > 0 and other.correlation_id == pos.correlation_id
                    for other in self._positions.values()
                ):
                    trade.closed_at_ms = self.now_ms
                    trade.exit_price = fill
                    trade.close_reason = reason
                    self.active_correlation_id = None
        if pos.role == "MAIN" and trade is not None and pos.quantity > 0:
            trade.exit_price = fill
        self._attribute_management(reason, exec_id, quantity, fill)

    def _update_excursions(self, pos: _Position, bar: Mapping[str, Any]) -> None:
        trade = self._trade_for(pos.correlation_id)
        if trade is None:
            return
        high = _decimal(bar.get("high"), "bar.high", positive=True)
        low = _decimal(bar.get("low"), "bar.low", positive=True)
        sign = _side_sign(pos.side)
        if sign > 0:
            favorable, adverse = high, low
        else:
            favorable, adverse = low, high
        if trade.max_favorable_price is None or (favorable - trade.entry_price) * sign > (
            trade.max_favorable_price - trade.entry_price
        ) * sign:
            trade.max_favorable_price = favorable
        if trade.max_adverse_price is None or (adverse - trade.entry_price) * sign < (
            trade.max_adverse_price - trade.entry_price
        ) * sign:
            trade.max_adverse_price = adverse
        if trade.initial_risk_usd > 0 and trade.initial_quantity > 0:
            unit_risk = trade.initial_risk_usd / trade.initial_quantity
            favorable_move = max(_D0, (trade.max_favorable_price - trade.entry_price) * sign)
            adverse_move = max(_D0, (trade.entry_price - trade.max_adverse_price) * sign)
            trade.max_favorable_r = favorable_move / unit_risk
            trade.max_adverse_r = adverse_move / unit_risk

    def resolve_executor_bar(self, bar: Mapping[str, Any]) -> dict[str, Any] | None:
        """Apply production executor barriers to one newly available 1m bar.

        If stop and target both trade inside a bar, the stop is filled first
        and the result records ``ambiguous_bar=True``. No future bar is read.
        """
        if bar.get("open_time_ms") is None or bar.get("close_time_ms") is None:
            raise ValueError("executor resolution requires 1m bar timestamps")
        open_ms = int(bar["open_time_ms"])
        close_ms = int(bar["close_time_ms"])
        if close_ms != open_ms + 60_000 - 1:
            raise ValueError("executor resolution requires complete 1m candles")
        if bar.get("closed") is False:
            raise ValueError("executor resolution cannot use a forming candle")
        if (
            self.last_resolved_bar_close_ms is not None
            and open_ms != self.last_resolved_bar_close_ms + 1
        ):
            raise ValueError("executor candles must be resolved sequentially without gaps")
        self.last_resolved_bar_close_ms = close_ms
        self.set_market(bar, decision_time_ms=close_ms)
        main = next(
            (pos for pos in self._positions.values() if pos.role == "MAIN" and pos.quantity > 0),
            None,
        )
        if main is None:
            self._record_equity(event="mark")
            return None
        trade = self._trade_for(main.correlation_id)
        high = _decimal(bar.get("high"), "bar.high", positive=True)
        low = _decimal(bar.get("low"), "bar.low", positive=True)
        opening = _decimal(bar.get("open"), "bar.open", positive=True)
        sign = _side_sign(main.side)
        locked_long = self.long_lock_policy and main.side == "LONG" and trade is not None
        stop = trade.protection_limit if locked_long else main.stop_price
        target = main.target_price
        stop_hit = bool(stop is not None and (low <= stop if sign > 0 else high >= stop))
        target_hit = bool(target is not None and (high >= target if sign > 0 else low <= target))
        ambiguous = stop_hit and target_hit
        reason: str | None = None
        exit_mark: Decimal | None = None
        if stop_hit:
            reason = "STOP_LOSS"
            exit_mark = min(opening, stop) if sign > 0 else max(opening, stop)  # adverse opening gap
        elif target_hit:
            reason = "TAKE_PROFIT"
            exit_mark = max(opening, target) if sign > 0 else min(opening, target)  # favorable gap improvement
        elif main.time_limit_sec is not None and close_ms - main.opened_at_ms >= main.time_limit_sec * 1000:
            reason = "TIME_LIMIT"
            exit_mark = _decimal(bar.get("close"), "bar.close", positive=True)
        # A write can happen after the current candle opened. Its OHLC contains
        # an unknown pre-fill segment, so the adapter cannot use that bar to
        # infer a post-fill stop, target, MFE or MAE.
        partial_entry_bar = open_ms < main.opened_at_ms
        if partial_entry_bar:
            reason = None
            exit_mark = None
            if trade is not None:
                trade.partial_entry_bar_count += 1
                self.executions.append(
                    {
                        "event": "partial_entry_bar_skipped",
                        "correlation_id": trade.correlation_id,
                        "bar_open_time_ms": open_ms,
                        "bar_close_time_ms": close_ms,
                        "note": "OHLC includes prices from before the market fill.",
                    }
                )
        elif locked_long:
            hedge_quantity = sum((pos.quantity for pos in self._positions.values()
                if pos.role == "HEDGE" and pos.correlation_id == main.correlation_id), _D0)
            if stop_hit and trade.protection_armed and hedge_quantity < main.quantity:
                proof = {
                    "correlation_id": trade.correlation_id,
                    "bar_open_time_ms": open_ms,
                    "bar_close_time_ms": close_ms,
                    "protection_limit": str(stop),
                    "low": str(low),
                    "high": str(high),
                    "close": str(bar.get("close")),
                    "ambiguous_target_touch": ambiguous,
                    "observed_at_ms": close_ms,
                }
                self.long_lock_requests.append(proof)
                trade.last_lock_proof = proof
                trade.protection_armed = False
                self.executions.append({"event": "long_lock_requested", **proof})
            if (trade.duration_expires_at_ms is not None
                and close_ms >= trade.duration_expires_at_ms
                and trade.duration_notified_at_ms != trade.duration_expires_at_ms):
                request = {"correlation_id": trade.correlation_id,
                    "bar_close_time_ms": close_ms,
                    "expires_at_ms": trade.duration_expires_at_ms,
                    "observed_at_ms": close_ms}
                self.duration_requests.append(request)
                trade.duration_notified_at_ms = trade.duration_expires_at_ms
                self.executions.append({"event": "long_duration_expired", **request})
            if reason == "TAKE_PROFIT":
                if hedge_quantity > 0:
                    reason = None
                else:
                    original_mark = self.mark_price
                    self.mark_price = exit_mark
                    try:
                        self._guard_main_long_exit(main, main.quantity)
                    except ExecutionRejected:
                        reason = None
                    finally:
                        self.mark_price = original_mark
            else:
                # Stops request protection and duration expiry requests a PM wake.
                # Neither is an intrabar LONG liquidation.
                reason = None
            if reason is None:
                exit_mark = None
            self._update_excursions(main, bar)
        else:
            excursion_bar = dict(bar)
            if reason == "STOP_LOSS" and exit_mark is not None:
                # Stop-first on an ambiguous candle: don't count an extreme
                # that might have occurred after the stop closed the trade.
                if sign > 0:
                    excursion_bar["high"] = min(high, exit_mark)
                else:
                    excursion_bar["low"] = max(low, exit_mark)
            elif reason == "TAKE_PROFIT" and exit_mark is not None:
                if sign > 0:
                    excursion_bar["high"] = min(high, exit_mark)
                else:
                    excursion_bar["low"] = max(low, exit_mark)
            self._update_excursions(main, excursion_bar)
        if reason is not None and exit_mark is not None:
            # Slippage applies to the actual executor fill. Move the simulated
            # current quote to its stop/target reference first, then market out.
            self.mark_price = exit_mark
            if ambiguous and trade is not None:
                trade.ambiguous_bar_count += 1
            self._close_position(main, main.quantity, reason=reason)
            # Fills occur at the barrier (with configured slippage), while the
            # end-of-minute account mark returns to the observed candle close.
            self.mark_price = _decimal(bar.get("close"), "bar.close", positive=True)
            result = {
                "correlation_id": main.correlation_id,
                "position_id": main.position_id,
                "at_ms": close_ms,
                "reason": reason,
                "bar_open_time_ms": open_ms,
                "bar_close_time_ms": close_ms,
                "ambiguous_bar": ambiguous,
                "stop_first_assumption": ambiguous,
                "reference_exit_price": str(exit_mark),
                "fill_price": self.fills[-1]["price"],
            }
            self.executions.append({"event": "executor_exit", **result})
            self._record_equity(event=f"executor_{reason.lower()}")
            return result
        self.mark_price = _decimal(bar.get("close"), "bar.close", positive=True)
        self._record_equity(event="mark")
        return None

    def mark_to_market(self, *, at_ms: int | None = None) -> dict[str, str]:
        if at_ms is not None:
            self.now_ms = int(at_ms)
        row = self._record_equity(event="mark")
        return {
            "at_ms": str(row["at_ms"]),
            "mark_price": row["mark_price"],
            "equity": row["equity"],
            "unrealized_pnl": row["unrealized_pnl"],
        }

    def _record_equity(self, *, event: str) -> dict[str, Any]:
        row = {
            "at_ms": self.now_ms,
            "event": event,
            "mark_price": str(self.mark_price),
            "equity": str(self.marked_equity),
            "realized_gross_pnl": str(self._realized_gross),
            "unrealized_pnl": str(self.unrealized_pnl),
            "fees": str(self._fees),
            "slippage_cost": str(self._slippage),
            "gross_exposure": str(self.gross_exposure),
            "used_margin": str(self.used_margin),
        }
        self.equity_curve.append(row)
        return row

    def snapshot(self) -> dict[str, Any]:
        """JSON-compatible final account/trade/execution state for persistence."""
        return {
            "symbol": self.symbol,
            "account_name": self.account_name,
            "connector_name": self.connector_name,
            "controller_id": self.controller_id,
            "as_of_ms": self.now_ms,
            "initial_equity": str(self.initial_equity),
            "equity": str(self.marked_equity),
            "realized_gross_pnl": str(self._realized_gross),
            "unrealized_pnl": str(self.unrealized_pnl),
            "costs": self.costs,
            "execution_assumptions": {
                "entry_order_type": "MARKET at the current known venue mark",
                "taker_fee_rate": str(self.taker_fee_rate),
                "slippage_bps": str(self.slippage_bps),
                "funding": "not modeled; reported as 0",
                "bar_resolution": (
                    "Sequential 1m OHLC; stop wins a same-candle stop/target tie; "
                    "partial entry candle extremes are skipped; MFE/MAE are bar-based approximations."
                ),
            },
            "open_positions": [
                self._position_dump(pos)
                for pos in self._positions.values()
                if pos.quantity > 0
            ],
            "entry_submissions": list(self.entry_submissions),
            "pending_intents": list(self.pending_intents),
            "long_lock_policy": self.long_lock_policy,
            "long_lock_requests": list(self.long_lock_requests),
            "duration_requests": list(self.duration_requests),
            "open_position_count": sum(pos.quantity > 0 for pos in self._positions.values()),
            "open_trade_count": sum(trade.is_open for trade in self.trades),
            "entry_submissions": list(self.entry_submissions),
            "open_trades": [self._trade_dump(trade) for trade in self.trades if trade.is_open],
            "closed_trades": [self._trade_dump(trade) for trade in self.trades if not trade.is_open],
            "fills": [dict(row) for row in self.fills],
            "executions": [dict(row) for row in self.executions],
            "equity_curve": [dict(row) for row in self.equity_curve],
        }

    @staticmethod
    def _position_dump(pos: _Position) -> dict[str, Any]:
        return {
            **asdict(pos),
            "quantity": str(pos.quantity),
            "entry_price": str(pos.entry_price),
            "original_quantity": str(pos.original_quantity),
            "realized_gross": str(pos.realized_gross),
            "entry_fees": str(pos.entry_fees),
            "exit_fees": str(pos.exit_fees),
            "slippage_cost": str(pos.slippage_cost),
            "stop_price": str(pos.stop_price) if pos.stop_price is not None else None,
            "target_price": str(pos.target_price) if pos.target_price is not None else None,
        }

    def _trade_dump(self, trade: _Trade) -> dict[str, Any]:
        remaining = sum(
            (pos.quantity for pos in self._positions.values()
             if pos.role == "MAIN" and pos.correlation_id == trade.correlation_id),
            _D0,
        )
        unrealized = sum(
            (self._unrealized_for(pos) for pos in self._positions.values()
             if pos.correlation_id == trade.correlation_id),
            _D0,
        )
        net_realized = trade.realized_gross_pnl - trade.fees
        return {
            "correlation_id": trade.correlation_id,
            "symbol": trade.symbol,
            "side": trade.side,
            "status": "open" if trade.is_open else "closed",
            "opened_at_ms": trade.opened_at_ms,
            "closed_at_ms": trade.closed_at_ms,
            "entry_price": str(trade.entry_price),
            "submission_mark": str(trade.submission_mark),
            "initial_quantity": str(trade.initial_quantity),
            "remaining_main_quantity": str(remaining),
            "initial_risk_usd": str(trade.initial_risk_usd),
            "stop_price": str(trade.stop_price),
            "target_price": str(trade.target_price),
            "exit_price": str(trade.exit_price) if trade.exit_price is not None else None,
            "close_reason": trade.close_reason,
            "realized_gross_pnl": str(trade.realized_gross_pnl),
            "fees": str(trade.fees),
            "slippage_cost": str(trade.slippage_cost),
            "net_realized_pnl": str(net_realized),
            "unrealized_pnl": str(unrealized),
            "net_pnl_including_open": str(net_realized + unrealized),
            "r_multiple": str((net_realized + unrealized) / trade.initial_risk_usd)
                if trade.initial_risk_usd > 0 else None,
            "mfe_price": str(trade.max_favorable_price) if trade.max_favorable_price is not None else None,
            "mae_price": str(trade.max_adverse_price) if trade.max_adverse_price is not None else None,
            "mfe_r": str(trade.max_favorable_r),
            "mae_r": str(trade.max_adverse_r),
            "ambiguous_bar_count": trade.ambiguous_bar_count,
            "partial_entry_bar_count": trade.partial_entry_bar_count,
            "trigger_status": trade.trigger_status,
            "trigger_kind": trade.trigger_kind,
            "submitted_order_type": trade.submitted_order_type,
            "management_actions": [dict(action) for action in trade.management_actions],
            "protection_limit": str(trade.protection_limit) if trade.protection_limit is not None else None,
            "protection_armed": trade.protection_armed,
            "last_lock_proof": trade.last_lock_proof,
            "duration_expires_at_ms": trade.duration_expires_at_ms,
            "duration_notified_at_ms": trade.duration_notified_at_ms,
        }

    def trades_dump(self) -> list[dict[str, Any]]:
        return [self._trade_dump(trade) for trade in self.trades]

    def open_positions_dump(self) -> list[dict[str, Any]]:
        return [
            self._position_dump(pos)
            for pos in self._positions.values()
            if pos.quantity > 0
        ]

    def set_candle_source(self, source: Any) -> None:
        """Attach the runner's historical, decision-time-gated candle source."""
        self.candle_source = source

    @property
    def equity_history(self) -> list[dict[str, Any]]:
        """Compatibility name used by the walk-forward driver."""
        return self.equity_curve

    def save_checkpoint(self, path: Path | str) -> Path:
        """Atomically persist venue state, ledger rows, counters and staged intent."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "condor.brooks.walkforward-checkpoint.v1",
            "config": {
                "symbol": self.symbol,
                "initial_equity": str(self.initial_equity),
                "amount_step": str(self.amount_step),
                "min_amount": str(self.min_amount),
                "min_notional": str(self.min_notional),
                "max_leverage": self.max_leverage,
                "taker_fee_rate": str(self.taker_fee_rate),
                "slippage_bps": str(self.slippage_bps),
                "account_name": self.account_name,
                "connector_name": self.connector_name,
                "controller_id": self.controller_id,
                "long_lock_policy": self.long_lock_policy,
            },
            "now_ms": self.now_ms,
            "time_ms": self._time_ms,
            "mark_price": str(self.mark_price),
            "current_bar": self.current_bar,
            "last_resolved_bar_close_ms": self.last_resolved_bar_close_ms,
            "realized_gross": str(self._realized_gross),
            "fees": str(self._fees),
            "slippage": str(self._slippage),
            "executor_seq": self._exec_seq,
            "position_seq": self._position_seq,
            "executor_rows": self.executor_rows,
            "holds": self.holds,
            "fills": self.fills,
            "positions": [asdict(row) for row in self._positions.values()],
            "trades": [asdict(row) for row in self.trades],
            "entry_submissions": self.entry_submissions,
            "pending_intents": self.pending_intents,
            "long_lock_requests": self.long_lock_requests,
            "duration_requests": self.duration_requests,
            "executions": self.executions,
            "equity_curve": self.equity_curve,
            "active_correlation_id": self.active_correlation_id,
            "staged_intent": self._staged_intent,
        }
        temp = target.with_name(target.name + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        return target

    def load_checkpoint(self, path: Path | str) -> None:
        """Restore an earlier checkpoint into an adapter built with the same config."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != "condor.brooks.walkforward-checkpoint.v1":
            raise ValueError("unsupported walk-forward simulation checkpoint")
        expected = {
            "symbol": self.symbol,
            "initial_equity": str(self.initial_equity),
            "amount_step": str(self.amount_step),
            "min_amount": str(self.min_amount),
            "min_notional": str(self.min_notional),
            "max_leverage": self.max_leverage,
            "taker_fee_rate": str(self.taker_fee_rate),
            "slippage_bps": str(self.slippage_bps),
            "account_name": self.account_name,
            "connector_name": self.connector_name,
            "controller_id": self.controller_id,
            "long_lock_policy": self.long_lock_policy,
        }
        checkpoint_config = dict(payload.get("config") or {})
        if "long_lock_policy" not in checkpoint_config and not self.long_lock_policy:
            checkpoint_config["long_lock_policy"] = False
        if checkpoint_config != expected:
            raise ValueError("checkpoint config differs from the active simulation")
        self._time_ms = int(payload.get("time_ms", payload.get("now_ms", 0)))
        self.mark_price = _decimal(payload["mark_price"], "checkpoint.mark_price", positive=True)
        self.current_bar = payload.get("current_bar")
        value = payload.get("last_resolved_bar_close_ms")
        self.last_resolved_bar_close_ms = int(value) if value is not None else None
        self._realized_gross = _decimal(payload.get("realized_gross", 0), "checkpoint.realized_gross")
        self._fees = _decimal(payload.get("fees", 0), "checkpoint.fees")
        self._slippage = _decimal(payload.get("slippage", 0), "checkpoint.slippage")
        self._exec_seq = int(payload.get("executor_seq", 0))
        self._position_seq = int(payload.get("position_seq", 0))
        self.executor_rows = list(payload.get("executor_rows", []))
        self.holds = list(payload.get("holds", []))
        self.fills = list(payload.get("fills", []))
        self._positions = {}
        position_decimal_fields = {
            "quantity", "entry_price", "original_quantity", "realized_gross",
            "entry_fees", "exit_fees", "slippage_cost", "stop_price", "target_price",
        }
        for raw in payload.get("positions", []):
            row = dict(raw)
            for name in position_decimal_fields:
                if row.get(name) is not None:
                    row[name] = Decimal(str(row[name]))
            position = _Position(**row)
            self._positions[position.position_id] = position
        trade_decimal_fields = {
            "entry_price", "initial_quantity", "initial_risk_usd", "stop_price",
            "target_price", "submission_mark", "realized_gross_pnl", "fees",
            "slippage_cost", "max_favorable_price", "max_adverse_price",
            "max_favorable_r", "max_adverse_r", "exit_price",
            "protection_limit",
        }
        self.trades = []
        for raw in payload.get("trades", []):
            row = dict(raw)
            for name in trade_decimal_fields:
                if row.get(name) is not None:
                    row[name] = Decimal(str(row[name]))
            valid_fields = {item.name for item in fields(_Trade)}
            row = {name: value for name, value in row.items() if name in valid_fields}
            self.trades.append(_Trade(**row))
        self.pending_intents = list(payload.get("pending_intents", []))
        self.long_lock_requests = list(payload.get("long_lock_requests", []))
        self.duration_requests = list(payload.get("duration_requests", []))
        self.entry_submissions = list(payload.get("entry_submissions", []))
        self.executions = list(payload.get("executions", []))
        self.equity_curve = list(payload.get("equity_curve", []))
        self.active_correlation_id = payload.get("active_correlation_id")
        staged = payload.get("staged_intent")
        self._staged_intent = (dict(staged[0]), staged[1]) if staged else None

    def flush_to(self, root: Path | str) -> Path:
        """Write the complete adapter snapshot as atomic JSON and JSONL ledgers."""
        destination = Path(root)
        destination.mkdir(parents=True, exist_ok=True)

        def replace_text(name: str, content: str) -> None:
            target = destination / name
            temp = target.with_name(target.name + ".tmp")
            with temp.open("w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)

        replace_text(
            "snapshot.json",
            json.dumps(self.snapshot(), sort_keys=True, indent=2, default=str) + "\n",
        )
        ledgers = {
            "fills.jsonl": self.fills,
            "trades.jsonl": self.trades_dump(),
            "equity.jsonl": self.equity_curve,
            "executions.jsonl": self.executions,
            "entry_submissions.jsonl": self.entry_submissions,
        }
        for filename, rows in ledgers.items():
            body = "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows)
            replace_text(filename, body)
        return destination
