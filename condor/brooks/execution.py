"""Host-only Hummingbot writes for Brooks, using the existing typed primitives."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from mcp_servers.hummingbot_api.tools import executor_create, executors


class ExecutionRejected(RuntimeError):
    """The venue did not unambiguously accept a requested write."""


def _positive(value: Decimal | str | int | float, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


def _accepted(result: Any) -> str:
    if not isinstance(result, dict) or result.get("error"):
        raise ExecutionRejected(f"executor write rejected: {result!r}")
    executor_id = result.get("executor_id")
    if not isinstance(executor_id, str) or not executor_id.strip():
        raise ExecutionRejected(
            "executor write returned no executor_id; reconcile before retry"
        )
    return executor_id


class HummingbotExecutionPort:
    """Narrow Brooks adapter over Hummingbot's existing executor operations.

    A client is injected so the normal Condor Hummingbot resolver can select the
    server. No MCP tool is exposed to a Trader or PM by this class.
    """

    def __init__(
        self, client: Any, *, account_name: str, connector_name: str, controller_id: str
    ):
        if not all(
            isinstance(x, str) and x.strip()
            for x in (account_name, connector_name, controller_id)
        ):
            raise ValueError("account, connector and controller must be explicit")
        self.client = client
        self.account_name = account_name
        self.connector_name = connector_name
        self.controller_id = controller_id

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
        if side not in ("LONG", "SHORT") or not symbol:
            raise ValueError("invalid MAIN side or symbol")
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
            raise ValueError("leverage must be a positive integer")
        if (
            isinstance(time_limit_sec, bool)
            or not isinstance(time_limit_sec, int)
            or time_limit_sec < 1
        ):
            raise ValueError("time limit must be a positive integer")
        amount = _positive(quantity, "quantity")
        stop = _positive(stop_loss_pct, "stop_loss_pct")
        target = _positive(take_profit_pct, "take_profit_pct")
        if stop >= 1 or target >= 1:
            raise ValueError("barrier percentages must be below one")
        result = await executor_create.create_position_executor(
            self.client,
            connector_name=self.connector_name,
            trading_pair=symbol,
            side=1 if side == "LONG" else 2,
            amount=float(amount),
            leverage=leverage,
            stop_loss=float(stop),
            take_profit=float(target),
            time_limit=time_limit_sec,
            open_order_type=1,  # MARKET; no resting limit with a stale reference price
            account_name=self.account_name,
            controller_id=self.controller_id,
            save_as_default=False,
        )
        return _accepted(result)

    async def reduce_main(
        self, *, symbol: str, side: str, quantity: Decimal, leverage: int
    ) -> str:
        if side not in ("LONG", "SHORT") or not symbol:
            raise ValueError("invalid MAIN side or symbol")
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
            raise ValueError("invalid leverage")
        amount = _positive(quantity, "quantity")
        result = await executor_create.create_order_executor(
            self.client,
            connector_name=self.connector_name,
            trading_pair=symbol,
            side=2 if side == "LONG" else 1,
            amount=str(amount),
            execution_strategy="MARKET",
            leverage=leverage,
            position_action="CLOSE",
            account_name=self.account_name,
            controller_id=self.controller_id,
            save_as_default=False,
        )
        return _accepted(result)

    async def close_main(self, *, executor_id: str) -> str:
        if not executor_id:
            raise ValueError("MAIN executor_id is required")
        result = await executors.stop_executor(
            self.client, executor_id=executor_id, keep_position=False
        )
        if not isinstance(result, dict) or result.get("error"):
            raise ExecutionRejected(f"MAIN close rejected: {result!r}")
        status = result.get("result")
        if (
            not isinstance(status, dict)
            or not status
            or status.get("status") in ("already_terminated", "failed", "error")
        ):
            raise ExecutionRejected(
                "MAIN close result is ambiguous; reconcile position"
            )
        return executor_id

    async def get_state(self, *, symbol: str) -> dict[str, Any]:
        """Return raw fresh reads; the GM must validate their shape and ownership."""
        positions = await self.client.trading.get_positions(
            account_names=[self.account_name],
            connector_names=[self.connector_name],
            limit=1000,
        )
        active = await self.client.executors.search_executors(
            account_names=[self.account_name],
            connector_names=[self.connector_name],
            trading_pairs=[symbol],
            controller_ids=[self.controller_id],
            limit=1000,
        )
        if not isinstance(positions, dict) or not isinstance(
            positions.get("data"), list
        ):
            raise ExecutionRejected("positions read is incomplete")
        if not isinstance(active, dict) or not isinstance(active.get("data"), list):
            raise ExecutionRejected("executors read is incomplete")
        if (
            len(positions["data"]) >= 1000
            or len(active["data"]) >= 1000
            or positions.get("next_cursor")
            or active.get("next_cursor")
        ):
            raise ExecutionRejected("state read is truncated")
        return {"positions": positions["data"], "executors": active["data"]}
