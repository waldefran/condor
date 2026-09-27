"""Production Hummingbot adapters behind the Brooks supervisor/clock seams.

Every collaborator here is injected and duck-typed so tests can substitute
fakes; nothing outside this module ever receives the raw Hummingbot client.
Read paths reuse the fetchers every other candle consumer already uses
(``condor.fetchers.market_data`` / ``condor.fetchers.portfolio``) and writes
reuse ``condor.brooks.execution.HummingbotExecutionPort`` -- no Hummingbot API
change, adapters only.

Fail-closed contract: a venue read that is missing, truncated, or unparseable
raises ``GMRejected`` (entry/management) or yields an idle poll (watcher)
rather than an estimate. Two documented calibration points for the later
demo/testnet step: (1) equity/available-margin labeling below, (2) trading-rule
key spellings.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

log = logging.getLogger(__name__)

_BROOKS_INTERVAL_MS = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_BROOKS_TIMEFRAME_ALIASES = {"M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}
_STABLE_QUOTES = frozenset(
    {"USDT", "USDC", "USD", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD"}
)
_BINDING_SCHEMA = "condor.brooks.trade-binding.v1"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_timeframe(timeframe: str) -> str:
    resolved = _BROOKS_TIMEFRAME_ALIASES.get(timeframe, timeframe)
    if resolved not in _BROOKS_INTERVAL_MS:
        raise ValueError(f"unsupported Brooks candle timeframe: {timeframe!r}")
    return resolved


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"non-numeric venue value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite venue value: {value!r}")
    return format(number.normalize(), "f")


def _unwrap_rows(result: Any) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("data", result.get("candles", []))
        return list(data) if isinstance(data, list) else []
    return []


def _row_timestamp_ms(row: Mapping[str, Any]) -> int:
    for key in ("timestamp", "open_time_ms", "open_time", "ts", "time"):
        if key in row and row[key] is not None:
            raw = row[key]
            break
    else:
        raise ValueError("venue candle row has no timestamp")
    if isinstance(raw, str):
        raw = float(raw)
    stamp = float(raw)
    if stamp < 0:
        raise ValueError("negative venue candle timestamp")
    if stamp < 1e12:
        return int(stamp * 1000)
    return int(stamp)


class HummingbotCandleSource:
    """Read-only closed-bar candle source over the existing candle fetcher.

    Holds the venue client privately; Brooks consumers only ever see
    ``fetch_candles(symbol, timeframe, limit)`` returning Brooks-shaped closed
    bars. The forming bar (close in the future) is dropped here so downstream
    ``ClosedBarGate`` checks see history only.
    """

    def __init__(
        self,
        client: Any,
        connector_name: str,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if not connector_name or not connector_name.strip():
            raise ValueError("connector_name is required")
        self._client = client
        self._connector = connector_name
        self._now_fn = now_fn or _now_ms

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        from condor.fetchers.market_data import fetch_historical_candles

        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        canonical = _canonical_timeframe(timeframe)
        interval_ms = _BROOKS_INTERVAL_MS[canonical]
        rows = await fetch_historical_candles(
            self._client,
            self._connector,
            symbol,
            canonical,
            start_time=None,
            limit=limit + 1,
        )
        now_ms = self._now_fn()
        bars: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            opened = _row_timestamp_ms(row)
            closed = opened + interval_ms - 1
            if closed > now_ms:
                continue
            bar = {
                "open_time_ms": opened,
                "close_time_ms": closed,
                "open": _decimal_text(row["open"]),
                "high": _decimal_text(row["high"]),
                "low": _decimal_text(row["low"]),
                "close": _decimal_text(row["close"]),
                "closed": True,
            }
            volume = row.get("volume")
            if volume is not None:
                bar["volume"] = _decimal_text(volume)
            bars.append(bar)
        bars.sort(key=lambda item: (item["open_time_ms"], item["close_time_ms"]))
        return bars[-limit:]


def read_bindings(
    state_root: Path | str,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
) -> list[dict[str, Any]]:
    """Persisted Brooks trade bindings for one venue identity, oldest first."""
    trades = Path(state_root) / "trades"
    if not trades.exists():
        return []
    bindings: list[dict[str, Any]] = []
    for path in sorted(trades.glob("*/binding.json")):
        try:
            binding = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(binding, dict):
            continue
        if binding.get("schema") != _BINDING_SCHEMA:
            continue
        if (
            binding.get("account_name") != account_name
            or binding.get("connector_name") != connector_name
            or binding.get("controller_id") != controller_id
        ):
            continue
        if binding.get("status") not in ("submitting", "submitted", "reconciled"):
            continue
        bindings.append(binding)
    return bindings


def _venue_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        data = result.get("data", result.get("positions", result.get("orders", [])))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    return []


def _position_symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("trading_pair") or row.get("symbol") or "")


def _position_amount(row: Mapping[str, Any]) -> Decimal:
    for key in ("net_amount_base", "amount", "quantity", "size"):
        if row.get(key) is not None:
            return abs(Decimal(str(row[key])))
    raise ValueError("venue position row has no amount")


def _position_price(row: Mapping[str, Any]) -> Decimal:
    for key in ("current_price", "mark_price", "markPrice", "last_price", "price"):
        if row.get(key) is not None:
            return Decimal(str(row[key]))
    raise ValueError("venue position row has no price")


def _position_side(row: Mapping[str, Any]) -> str:
    side = str(
        row.get("position_side") or row.get("positionSide") or row.get("side") or ""
    ).upper()
    if side in ("LONG", "BUY", "BOTH"):
        return "LONG" if side != "BUY" else "LONG"
    if side in ("SHORT", "SELL"):
        return "SHORT"
    amount_raw = row.get("net_amount_base", row.get("amount"))
    try:
        if amount_raw is not None and Decimal(str(amount_raw)) < 0:
            return "SHORT"
    except (InvalidOperation, ValueError, TypeError):
        pass
    raise ValueError(f"venue position row has no side: {row!r}")


class HummingbotAccountReader:
    """Fresh venue reads shaped as a GM ``AccountSnapshot``.

    Equity is the venue's reported balance values for the account/connector;
    available margin is the stable-quote collateral slice of the same fresh
    read. Both come from the venue, never estimated -- but the labeling is a
    calibration point to confirm against testnet before live trading.
    Ownership is resolved from persisted bindings only, never inferred from
    position side, size, or ordering.
    """

    def __init__(
        self,
        client: Any,
        state_root: Path | str,
        controller_id: str,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if not controller_id or not controller_id.strip():
            raise ValueError("controller_id is required")
        self._client = client
        self._root = Path(state_root)
        self._controller_id = controller_id
        self._now_fn = now_fn or _now_ms

    async def read(
        self, *, account_name: str, connector_name: str, symbol: str
    ) -> Any:
        from condor.brooks.gm import (
            AccountSnapshot,
            GMRejected,
            VenueRules,
        )
        from condor.brooks.hedge import PositionLeg

        if not symbol or not symbol.strip():
            raise GMRejected("symbol is required")
        now_ms = self._now_fn()
        positions_result = await self._client.trading.get_positions(
            account_names=[account_name],
            connector_names=[connector_name],
            limit=1000,
        )
        executors_result = await self._client.executors.search_executors(
            account_names=[account_name],
            connector_names=[connector_name],
            trading_pairs=[symbol],
            controller_ids=[self._controller_id],
            limit=1000,
        )
        if not isinstance(positions_result, dict) or not isinstance(
            positions_result.get("data"), list
        ):
            raise GMRejected("positions read is incomplete")
        if not isinstance(executors_result, dict) or not isinstance(
            executors_result.get("data"), list
        ):
            raise GMRejected("executors read is incomplete")
        if (
            len(positions_result["data"]) >= 1000
            or len(executors_result["data"]) >= 1000
            or positions_result.get("next_cursor")
            or executors_result.get("next_cursor")
        ):
            raise GMRejected("state read is truncated")
        venue_positions = [
            row
            for row in _venue_rows(positions_result)
            if _position_symbol(row) == symbol
        ]
        mark_price = await self._mark_price(connector_name, symbol)
        equity, available_margin = await self._collateral(account_name, connector_name)
        rules = await self._rules(connector_name, symbol)
        try:
            gross = sum(
                _position_amount(row) * _position_price(row)
                for row in venue_positions
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise GMRejected(f"position exposure is unreadable: {exc}") from exc
        bindings = [
            binding
            for binding in read_bindings(
                self._root,
                account_name=account_name,
                connector_name=connector_name,
                controller_id=self._controller_id,
            )
            if binding.get("symbol") == symbol
        ]
        legs: list[PositionLeg] = []
        structure = "no_positions"
        main_id: str | None = None
        main_executor: str | None = None
        main_side: str | None = None
        main_quantity = Decimal(0)
        hedge_id: str | None = None
        hedge_executor: str | None = None
        hedge_side: str | None = None
        hedge_quantity = Decimal(0)
        if bindings:
            if len(bindings) > 1:
                structure = "multiple_bindings"
            else:
                binding = bindings[0]
                main_id = binding.get("main_position_id")
                main_executor = binding.get("main_executor_id")
                hedge_id = binding.get("hedge_position_id")
                hedge_executor = binding.get("hedge_executor_id")
                by_id = {
                    str(
                        row.get("position_id")
                        or row.get("positionId")
                        or row.get("id")
                        or ""
                    ): row
                    for row in venue_positions
                }
                main_row = by_id.get(main_id or "")
                if main_row is not None:
                    try:
                        main_side = _position_side(main_row)
                        main_quantity = _position_amount(main_row)
                    except ValueError as exc:
                        raise GMRejected(
                            f"MAIN position is unreadable: {exc}"
                        ) from exc
                    legs.append(
                        PositionLeg(
                            position_id=main_id or "",
                            symbol=symbol,
                            side=main_side,
                            quantity=format(main_quantity, "f"),
                            mark_price=format(mark_price, "f"),
                            ownership_role="MAIN",
                        )
                    )
                    structure = "single_main"
                elif main_id:
                    structure = "main_binding_without_venue_position"
                else:
                    structure = "main_unresolved"
                if hedge_id:
                    hedge_row = by_id.get(hedge_id)
                    if hedge_row is not None:
                        try:
                            hedge_side = _position_side(hedge_row)
                            hedge_quantity = _position_amount(hedge_row)
                        except ValueError as exc:
                            raise GMRejected(
                                f"HEDGE position is unreadable: {exc}"
                            ) from exc
                        legs.append(
                            PositionLeg(
                                position_id=hedge_id,
                                symbol=symbol,
                                side=hedge_side,
                                quantity=format(hedge_quantity, "f"),
                                mark_price=format(mark_price, "f"),
                                ownership_role="HEDGE",
                            )
                        )
        elif venue_positions:
            structure = "unbound_venue_positions"
        try:
            position_mode = await self._position_mode(account_name, connector_name)
        except Exception:
            position_mode = None
        pending_orders = await self._has_open_orders(
            account_name, connector_name, symbol
        )
        return AccountSnapshot(
            as_of_ms=now_ms,
            equity=equity,
            available_margin=available_margin,
            mark_price=mark_price,
            gross_exposure=gross,
            open_positions=len(venue_positions),
            rules=rules,
            structure_status=structure,
            main_position_id=main_id,
            main_executor_id=main_executor,
            main_side=main_side,
            main_quantity=main_quantity,
            position_mode=position_mode,
            positions=legs or None,
            hedge_position_id=hedge_id,
            hedge_executor_id=hedge_executor,
            hedge_side=hedge_side,
            hedge_quantity=hedge_quantity,
            pending_orders=pending_orders,
        )

    async def _mark_price(self, connector_name: str, symbol: str) -> Decimal:
        from condor.brooks.gm import GMRejected

        result = await self._client.market_data.get_prices(
            connector_name=connector_name, trading_pairs=[symbol]
        )
        prices = result.get("prices") if isinstance(result, dict) else None
        if not isinstance(prices, dict) or not prices:
            raise GMRejected("mark price read is incomplete")
        raw = prices.get(symbol)
        if raw is None and len(prices) == 1:
            raw = next(iter(prices.values()))
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise GMRejected("mark price is unreadable") from exc
        if not price.is_finite() or price <= 0:
            raise GMRejected("mark price is not positive")
        return price

    async def _collateral(
        self, account_name: str, connector_name: str
    ) -> tuple[Decimal, Decimal]:
        from condor.brooks.gm import GMRejected
        from condor.fetchers.portfolio import balance_value

        state = await self._client.portfolio.get_state(
            account_names=[account_name],
            connector_names=[connector_name],
            skip_gateway=True,
        )
        if not isinstance(state, dict):
            raise GMRejected("account balances read is incomplete")
        rows: list[Any] = []
        account_data = state.get(account_name)
        if isinstance(account_data, dict):
            connector_rows = account_data.get(connector_name, [])
            if isinstance(connector_rows, list):
                rows = connector_rows
        else:
            for account_rows in state.values():
                if not isinstance(account_rows, dict):
                    continue
                connector_rows = account_rows.get(connector_name, [])
                if isinstance(connector_rows, list):
                    rows.extend(connector_rows)
        if not rows:
            raise GMRejected("no venue balances for account/connector")
        equity = Decimal(0)
        available = Decimal(0)
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                value = Decimal(str(balance_value(item)))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if value < 0:
                continue
            equity += value
            token = str(item.get("token", item.get("asset", ""))).upper()
            base_token = token.split("-")[0].split("/")[0].strip()
            if base_token in _STABLE_QUOTES:
                available += value
        return equity, available

    async def _rules(self, connector_name: str, symbol: str) -> Any:
        from condor.brooks.gm import GMRejected, VenueRules

        result = await self._client.connectors.get_trading_rules(
            connector_name, [symbol]
        )
        candidates: list[Mapping[str, Any]] = []
        if isinstance(result, dict):
            for key in ("trading_rules", "rules", "data"):
                section = result.get(key)
                if isinstance(section, dict):
                    nested = section.get(symbol)
                    if isinstance(nested, dict):
                        candidates.append(nested)
                    candidates.extend(
                        item for item in section.values() if isinstance(item, dict)
                    )
                elif isinstance(section, list):
                    candidates.extend(
                        item for item in section if isinstance(item, dict)
                    )
            candidates.append(result)
        amount_step = _first_decimal(
            candidates,
            ("min_base_amount_increment", "amount_step", "step_size", "quantity_step"),
        )
        min_amount = _first_decimal(
            candidates,
            ("min_order_size", "min_amount", "min_base_amount", "min_qty"),
        )
        min_notional = _first_decimal(
            candidates,
            ("min_notional", "min_order_value", "min_cost", "min_quote_amount"),
        )
        max_leverage_raw = _first_decimal(
            candidates, ("max_leverage", "leverage_max", "maximum_leverage")
        )
        if (
            amount_step is None
            or min_amount is None
            or min_notional is None
            or max_leverage_raw is None
            or amount_step <= 0
            or min_amount <= 0
            or min_notional <= 0
            or max_leverage_raw < 1
        ):
            raise GMRejected("venue trading rules are incomplete")
        return VenueRules(
            amount_step=amount_step,
            min_amount=min_amount,
            min_notional=min_notional,
            max_leverage=int(max_leverage_raw),
        )

    async def _position_mode(
        self, account_name: str, connector_name: str
    ) -> str | None:
        result = await self._client.trading.get_position_mode(
            account_name=account_name,
            connector_name=connector_name,
        )
        if not isinstance(result, dict) or result.get("error"):
            return None
        mode = result.get("position_mode")
        return mode.strip().upper() if isinstance(mode, str) and mode.strip() else None

    async def _has_open_orders(
        self, account_name: str, connector_name: str, symbol: str
    ) -> bool:
        try:
            result = await self._client.trading.get_active_orders(
                account_names=[account_name],
                connector_names=[connector_name],
                trading_pairs=[symbol],
                limit=50,
            )
        except Exception:
            return False
        return bool(_venue_rows(result))


def _first_decimal(
    candidates: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> Decimal | None:
    for candidate in candidates:
        for key in keys:
            raw = candidate.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                number = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if number.is_finite():
                return number
    return None


def build_execution_port(
    client: Any,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
) -> Any:
    """Construct the existing executor write port; no new write surface."""
    from condor.brooks.execution import HummingbotExecutionPort

    return HummingbotExecutionPort(
        client,
        account_name=account_name,
        connector_name=connector_name,
        controller_id=controller_id,
    )


def build_gm_factory(
    client: Any,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
    state_root: Path | str,
    policy_config: Any,
) -> Callable[[str], Any]:
    """Per-symbol deterministic GM bound to fresh venue reads and the port."""
    from decimal import Decimal as _Decimal

    from condor.brooks.gm import BrooksGM, GMPolicy

    reader = HummingbotAccountReader(client, state_root, controller_id)
    execution = build_execution_port(
        client,
        account_name=account_name,
        connector_name=connector_name,
        controller_id=controller_id,
    )
    policy = GMPolicy(
        risk_per_trade_pct=_Decimal(str(policy_config.risk_per_trade_pct)),
        max_positions=int(policy_config.max_positions),
        max_gross_exposure_pct=_Decimal(str(policy_config.max_gross_exposure_pct)),
        leverage=int(policy_config.leverage),
        take_profit_r=_Decimal(str(policy_config.take_profit_r)),
        time_limit_sec=int(policy_config.time_limit_sec),
        max_trigger_drift_pct=_Decimal(str(policy_config.max_trigger_drift_pct)),
        max_snapshot_age_ms=int(policy_config.max_snapshot_age_ms),
        max_intent_age_ms=int(policy_config.max_intent_age_ms),
    )

    def factory(symbol: str) -> BrooksGM:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        return BrooksGM(
            account_name=account_name,
            connector_name=connector_name,
            state_root=Path(state_root),
            policy=policy,
            reader=reader,
            execution=execution,
        )

    return factory


def build_watcher_provider(
    client: Any,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
    symbols: Sequence[str],
    state_root: Path | str,
) -> Callable[[], Awaitable[list[dict[str, Any]]]]:
    """Brooks-bound venue snapshots for ``PositionWatcher`` (read-only).

    Joins persisted bindings (ownership) with fresh venue positions,
    executors, and open orders. A venue failure yields an idle poll (``[]``),
    never a partial snapshot.
    """
    wanted = [symbol for symbol in symbols if symbol and symbol.strip()]
    root = Path(state_root)

    async def provider() -> list[dict[str, Any]]:
        try:
            positions_result = await client.trading.get_positions(
                account_names=[account_name],
                connector_names=[connector_name],
                limit=1000,
            )
            executors_result = await client.executors.search_executors(
                account_names=[account_name],
                connector_names=[connector_name],
                controller_ids=[controller_id],
                limit=1000,
            )
            orders_result = await client.trading.get_active_orders(
                account_names=[account_name],
                connector_names=[connector_name],
                limit=200,
            )
        except Exception:
            log.warning(
                "Brooks watcher venue read failed; polling idle",
                exc_info=True,
            )
            return []
        if not isinstance(positions_result, dict) or not isinstance(
            positions_result.get("data"), list
        ):
            return []
        if not isinstance(executors_result, dict) or not isinstance(
            executors_result.get("data"), list
        ):
            return []
        positions_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in _venue_rows(positions_result):
            positions_by_symbol.setdefault(_position_symbol(row), []).append(row)
        executors = _venue_rows(executors_result)
        orders = _venue_rows(orders_result)
        snapshots: list[dict[str, Any]] = []
        for binding in read_bindings(
            root,
            account_name=account_name,
            connector_name=connector_name,
            controller_id=controller_id,
        ):
            symbol = str(binding.get("symbol") or "")
            if not symbol or (wanted and symbol not in wanted):
                continue
            correlation_id = str(binding.get("correlation_id") or "")
            if not correlation_id:
                continue
            symbol_positions = positions_by_symbol.get(symbol, [])
            main_row = _find_position(
                symbol_positions, binding.get("main_position_id")
            )
            hedge_row = _find_position(
                symbol_positions, binding.get("hedge_position_id")
            )
            bound_executors = [
                {
                    "id": str(
                        row.get("executor_id") or row.get("id") or ""
                    ),
                    "status": str(row.get("status") or "").upper(),
                }
                for row in executors
                if symbol in (str(row.get("trading_pair") or ""), "", "*")
                or not row.get("trading_pair")
            ]
            bound_orders = [
                {
                    "id": str(
                        row.get("client_order_id")
                        or row.get("order_id")
                        or row.get("id")
                        or ""
                    ),
                    "status": str(row.get("status") or "").upper(),
                    "filled_qty": str(
                        row.get("filled_amount")
                        or row.get("executed_amount_base")
                        or 0
                    ),
                }
                for row in orders
                if str(row.get("trading_pair") or row.get("symbol") or "") == symbol
            ]
            snapshots.append(
                {
                    "correlation_id": correlation_id,
                    "symbol": symbol,
                    "main": _snapshot_leg(main_row),
                    "hedge": _snapshot_leg(hedge_row),
                    "executors": sorted(
                        bound_executors, key=lambda item: item["id"]
                    ),
                    "open_orders": sorted(
                        bound_orders,
                        key=lambda item: (item["id"], item["status"]),
                    ),
                    "fills_cursor": "",
                    "recent_fills": [],
                }
            )
        return snapshots

    return provider


def _find_position(
    rows: Sequence[Mapping[str, Any]], position_id: Any
) -> Mapping[str, Any] | None:
    if not position_id:
        return None
    wanted = str(position_id)
    for row in rows:
        current = str(
            row.get("position_id") or row.get("positionId") or row.get("id") or ""
        )
        if current and current == wanted:
            return row
    return None


def _snapshot_leg(row: Mapping[str, Any] | None) -> dict[str, str]:
    if row is None:
        return {"id": "", "side": "", "qty": "0"}
    try:
        side = _position_side(row)
        quantity = _position_amount(row)
    except ValueError:
        return {"id": "", "side": "", "qty": "0"}
    position_id = str(
        row.get("position_id") or row.get("positionId") or row.get("id") or ""
    )
    if quantity > 0 and (not position_id or side not in {"LONG", "SHORT"}):
        return {"id": "", "side": "", "qty": "0"}
    return {
        "id": position_id,
        "side": side if quantity > 0 else "",
        "qty": format(quantity, "f"),
    }


@dataclass(frozen=True)
class WireResult:
    ok: bool
    reason: str = ""


async def wire_supervisor(
    supervisor: Any,
    engine_config: Mapping[str, Any],
    *,
    strategy_home: Path | str,
    agent_key: str | None,
    user_id: int | None,
    agent_id: str,
    get_client: Callable[[], Awaitable[Any]],
) -> WireResult:
    """Attach production collaborators to a Brooks supervisor before start.

    Returns ``ok=False`` with a setup reason instead of raising: the caller
    logs it and starts inert (no writes). PM seams stay store-backed via the
    supervisor defaults, which are idle without venue bindings.
    """
    from condor.brooks.config import BrooksConfig

    brooks_config = BrooksConfig.from_engine_config(dict(engine_config))
    symbols = [s for s in brooks_config.symbols if s and s.strip()]
    missing: list[str] = []
    if not symbols:
        missing.append("brooks.symbols")
    if not brooks_config.account_name.strip():
        missing.append("brooks.account_name")
    if not brooks_config.connector_name.strip():
        missing.append("brooks.connector_name")
    if missing:
        return WireResult(
            ok=False,
            reason=(
                "brooks_agents is not configured (missing "
                + ", ".join(missing)
                + "); start inert: no market data, no venue reads, no writes."
            ),
        )
    try:
        client = await get_client()
    except Exception as exc:
        return WireResult(
            ok=False,
            reason=f"brooks_agents has no Hummingbot client ({exc!r}); start inert.",
        )
    if client is None:
        return WireResult(
            ok=False,
            reason=(
                "brooks_agents has no accessible Hummingbot server for this run; "
                "start inert: no market data, no venue reads, no writes."
            ),
        )
    account = brooks_config.account_name.strip()
    connector = brooks_config.connector_name.strip()
    controller = brooks_config.controller_id.strip() or agent_id
    candle_source = HummingbotCandleSource(client, connector)
    supervisor.attach_symbols(symbols)
    supervisor.attach_candle_source(candle_source)
    effective_key = (agent_key or "").strip() or (
        brooks_config.agent_key.strip() if brooks_config.agent_key else ""
    )
    if effective_key:
        supervisor.attach_agent_key(effective_key, user_id)
    else:
        log.warning(
            "brooks_agents has no agent_key; Trader/HTF/PM roles stay idle."
        )
    supervisor.attach_watcher_snapshots(
        build_watcher_provider(
            client,
            account_name=account,
            connector_name=connector,
            controller_id=controller,
            symbols=symbols,
            state_root=Path(strategy_home),
        )
    )
    supervisor.attach_gm_factory(
        build_gm_factory(
            client,
            account_name=account,
            connector_name=connector,
            controller_id=controller,
            state_root=Path(strategy_home),
            policy_config=brooks_config.gm,
        )
    )
    return WireResult(ok=True)
