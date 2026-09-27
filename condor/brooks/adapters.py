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

    async def read(self, *, account_name: str, connector_name: str, symbol: str) -> Any:
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
                _position_amount(row) * _position_price(row) for row in venue_positions
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
                        raise GMRejected(f"MAIN position is unreadable: {exc}") from exc
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
            main_row = _find_position(symbol_positions, binding.get("main_position_id"))
            hedge_row = _find_position(
                symbol_positions, binding.get("hedge_position_id")
            )
            bound_executors = [
                {
                    "id": str(row.get("executor_id") or row.get("id") or ""),
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
                        row.get("filled_amount") or row.get("executed_amount_base") or 0
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
                    "executors": sorted(bound_executors, key=lambda item: item["id"]),
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


_PM_ORDER_SIDES = {
    "LONG": "LONG",
    "BUY": "LONG",
    "BID": "LONG",
    "SHORT": "SHORT",
    "SELL": "SHORT",
    "ASK": "SHORT",
}


def build_pm_load_context(
    client: Any,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
    state_root: Path | str,
    now_fn: Callable[[], int] | None = None,
) -> Callable[[str], Awaitable[dict[str, Any] | None]]:
    """Production ``pm_load_context`` for the Brooks PositionManager.

    Assembles one consistent, timestamped PM snapshot per correlation id from
    the persisted binding, fresh MAIN/HEDGE venue positions, bound executors,
    open orders, the original TradeIntent, the latest TraderIntent and
    MarketContext, management history, the governing management policy, fresh
    HedgeState and margin health. The emitted keys are exactly the
    ``pm._small_context`` allowlist; there are no candles in the initial
    context -- candles stay on-demand via the PM read tools (limit <= 30
    through ClosedBarGate).

    Fail-closed: a missing, truncated, unparseable, or ambiguous binding,
    MAIN/HEDGE ownership, venue snapshot, authoritative store document, or
    timestamp yields ``None`` so the PM never runs on a guess. Ambient
    best-effort context (latest TraderIntent/MarketContext) degrades to
    ``None`` fields instead of blocking. Venue/store failures also yield
    ``None`` (logged) rather than raising into the supervisor loop.
    """
    reader = HummingbotAccountReader(client, state_root, controller_id, now_fn=now_fn)
    root = Path(state_root)
    clock = now_fn or _now_ms

    async def load(correlation_id: str) -> dict[str, Any] | None:
        try:
            return await _pm_snapshot(
                reader,
                client,
                root,
                account_name=account_name,
                connector_name=connector_name,
                controller_id=controller_id,
                correlation_id=correlation_id,
                now_ms=clock(),
            )
        except Exception:
            log.warning(
                "Brooks PM context for %r is unavailable; PM stays idle",
                correlation_id,
                exc_info=True,
            )
            return None

    return load


def _pm_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _pm_snapshot(
    reader: HummingbotAccountReader,
    client: Any,
    root: Path,
    *,
    account_name: str,
    connector_name: str,
    controller_id: str,
    correlation_id: str,
    now_ms: int,
) -> dict[str, Any] | None:
    """Assemble one PM snapshot; ``None`` when anything authoritative is off."""
    import re

    from condor.brooks.contracts import (
        HedgeStateV1,
        ManagementPolicyContext,
        MarketContextV1,
        TradeIntentV2,
    )
    from condor.brooks.hedge import build_hedge_state
    from condor.brooks.pm import PM_V1_ACTIONS
    from condor.brooks.store import BrooksStore

    if not isinstance(correlation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", correlation_id
    ):
        return None
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        return None
    matches = [
        binding
        for binding in read_bindings(
            root,
            account_name=account_name,
            connector_name=connector_name,
            controller_id=controller_id,
        )
        if binding.get("correlation_id") == correlation_id
    ]
    if len(matches) != 1:
        return None
    binding = matches[0]
    symbol = binding.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    main_position_id = binding.get("main_position_id")
    if not isinstance(main_position_id, str) or not main_position_id:
        return None
    hedge_position_id = binding.get("hedge_position_id")
    if hedge_position_id is not None and (
        not isinstance(hedge_position_id, str)
        or not hedge_position_id
        or hedge_position_id == main_position_id
    ):
        return None
    # Fresh venue snapshot; HummingbotAccountReader raises GMRejected when any
    # leg is missing, truncated, or unparseable.
    snapshot = await reader.read(
        account_name=account_name, connector_name=connector_name, symbol=symbol
    )
    legs = list(snapshot.positions or [])
    main_legs = [
        leg
        for leg in legs
        if leg.ownership_role == "MAIN" and leg.position_id == main_position_id
    ]
    if len(main_legs) != 1:
        return None
    try:
        if Decimal(main_legs[0].quantity) <= 0:
            return None
    except InvalidOperation:
        return None
    if (
        hedge_position_id is not None
        and len(
            [
                leg
                for leg in legs
                if leg.ownership_role == "HEDGE"
                and leg.position_id == hedge_position_id
            ]
        )
        != 1
    ):
        return None
    hedge = build_hedge_state(
        legs,
        main_position_id=main_position_id,
        hedge_position_id=hedge_position_id,
        as_of_ms=now_ms,
    )
    if hedge.structure_status not in ("ok", "single_main"):
        return None
    # net_exposure is the signed net mark-notional exposure, i.e. the same
    # measurement build_hedge_state reports as net_exposure_usd.
    hedge_state = HedgeStateV1.model_validate(
        {
            "schema": hedge.schema,
            "main_side": hedge.main_side,
            "main_size": hedge.main_size,
            "hedge_side": hedge.hedge_side,
            "hedge_size": hedge.hedge_size,
            "net_exposure": hedge.net_exposure_usd,
            "hedge_ratio": hedge.hedge_ratio,
            "main_position_id": hedge.main_position_id,
            "hedge_position_id": hedge.hedge_position_id,
            "unresolved": hedge.unresolved,
            "structure_status": hedge.structure_status,
            "net_exposure_usd": hedge.net_exposure_usd,
            "gross_exposure_usd": hedge.gross_exposure_usd,
            "ratio_basis": hedge.ratio_basis,
        }
    ).model_dump(mode="json")
    # Authoritative per-trade documents (GM root). The entry path writes the
    # original intent before the binding, so a binding without a matching
    # intent is a torn write: fail closed.
    original_raw = _pm_read_json(
        root / "trades" / correlation_id / "original_trade_intent.json"
    )
    if not isinstance(original_raw, dict):
        return None
    original_intent = TradeIntentV2.model_validate(original_raw).model_dump(mode="json")
    if original_intent["symbol"] != symbol:
        return None
    # Supervisor store documents (brooks_state). Management history must be a
    # JSONL list of mappings; latest ambient intents degrade to None.
    history_rows = BrooksStore.read_jsonl(
        root / "brooks_state" / "trades" / correlation_id / "management_history.jsonl"
    )
    if any(not isinstance(row, dict) for row in history_rows):
        return None
    latest_trader = _pm_latest_intent(root, "trader", symbol)
    latest_market = _pm_latest_context(root, symbol)
    # Bound executors: the read itself must be complete; rows without an
    # identity cannot be referenced, so they are skipped, never guessed.
    executors_result = await client.executors.search_executors(
        account_names=[account_name],
        connector_names=[connector_name],
        trading_pairs=[symbol],
        controller_ids=[controller_id],
        limit=1000,
    )
    if not isinstance(executors_result, dict) or not isinstance(
        executors_result.get("data"), list
    ):
        return None
    if len(executors_result["data"]) >= 1000 or executors_result.get("next_cursor"):
        return None
    bound_executors = [
        sanitized
        for row in executors_result["data"]
        if isinstance(row, dict)
        and (sanitized := _pm_sanitize_executor(row)) is not None
    ]
    # Open orders are protection-relevant: any unscoped or unidentifiable row
    # fails the whole snapshot closed instead of hiding an order.
    orders_result = await client.trading.get_active_orders(
        account_names=[account_name],
        connector_names=[connector_name],
        trading_pairs=[symbol],
        limit=200,
    )
    if not isinstance(orders_result, dict) or not isinstance(
        orders_result.get("data"), list
    ):
        return None
    open_orders = []
    for row in orders_result["data"]:
        if not isinstance(row, dict):
            return None
        if str(row.get("trading_pair") or row.get("symbol") or "") != symbol:
            return None
        sanitized = _pm_sanitize_order(row, symbol, now_ms)
        if sanitized is None:
            return None
        open_orders.append(sanitized)
    management_policy = ManagementPolicyContext.model_validate(
        {
            "policy_id": f"brooks-pm-v1:{controller_id}",
            "version": "1",
            "policy_family": "brooks-position-management-v1",
            # No strategy-stop mandate is wired for V1, so the PM must not
            # force protection on an otherwise coherent position; the skill
            # treats an unmet stop mandate as an intervention trigger.
            "strategy_stop_required": False,
            "allowed_management_actions": sorted(PM_V1_ACTIONS),
            "protection_semantics": (
                "gm-compiled: PM decisions are advisory; only the "
                "deterministic GM may write to the venue."
            ),
        }
    ).model_dump(mode="json")
    margin_health = _pm_margin_health(
        snapshot.equity, snapshot.available_margin, snapshot.gross_exposure
    )
    quote = symbol.rsplit("-", 1)[-1].rsplit("/", 1)[-1].strip() or symbol
    main_leg = main_legs[0]
    main_position = {
        "position_id": main_leg.position_id,
        "symbol": main_leg.symbol,
        "side": main_leg.side,
        "quantity": main_leg.quantity,
        "mark_price": main_leg.mark_price,
        "ownership_role": main_leg.ownership_role,
        "as_of_ms": now_ms,
    }
    return {
        "correlation_id": correlation_id,
        "symbol": symbol,
        "decision_time_ms": now_ms,
        "account": {
            "equity": format(snapshot.equity, "f"),
            "available_margin": format(snapshot.available_margin, "f"),
            "currency": quote,
            "position_mode": snapshot.position_mode,
            "as_of_ms": now_ms,
        },
        "positions": [
            {
                "position_id": leg.position_id,
                "symbol": leg.symbol,
                "side": leg.side,
                "quantity": leg.quantity,
                "mark_price": leg.mark_price,
                "ownership_role": leg.ownership_role,
                "as_of_ms": now_ms,
            }
            for leg in legs
        ],
        "position": main_position,
        "executor_state": {
            "as_of_ms": now_ms,
            "controller_id": controller_id,
            "executors": bound_executors,
        },
        "open_orders": open_orders,
        "recent_fills": [],
        "fills_since_last_event": [],
        "original_trade_intent": original_intent,
        "latest_trader_intent": latest_trader,
        "latest_market_context": latest_market,
        "market_analysis": None,
        "management_history": history_rows[-10:],
        "management_policy": management_policy,
        "hedge_state": hedge_state,
        "margin_health": margin_health,
    }


def _pm_latest_intent(root: Path, role: str, symbol: str) -> dict[str, Any] | None:
    """Latest ambient TraderIntent for this symbol; ``None`` when not usable."""
    from condor.brooks.contracts import TradeIntentV2

    raw = _pm_read_json(root / "brooks_state" / role / "latest.json")
    if not isinstance(raw, dict):
        return None
    try:
        intent = TradeIntentV2.model_validate(raw).model_dump(mode="json")
    except Exception:
        return None
    return intent if intent.get("symbol") == symbol else None


def _pm_latest_context(root: Path, symbol: str) -> dict[str, Any] | None:
    """Latest ambient MarketContext for this symbol; ``None`` when not usable."""
    from condor.brooks.contracts import MarketContextV1

    raw = _pm_read_json(root / "brooks_state" / "htf" / "latest.json")
    if not isinstance(raw, dict):
        return None
    try:
        context = MarketContextV1.model_validate(raw).model_dump(mode="json")
    except Exception:
        return None
    return context if context.get("symbol") == symbol else None


def _pm_sanitize_executor(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Best-effort executor status; rows without an identity are skipped."""
    executor_id = row.get("executor_id") or row.get("id")
    if executor_id is None or not str(executor_id).strip():
        return None
    sanitized: dict[str, Any] = {
        "executor_id": str(executor_id),
        "status": str(row.get("status") or "UNKNOWN").upper(),
    }
    for key in ("trading_pair", "symbol", "controller_id", "controller_ids"):
        if row.get(key) is not None:
            sanitized[key] = row[key]
    return sanitized


def _pm_sanitize_order(
    row: Mapping[str, Any], symbol: str, now_ms: int
) -> dict[str, Any] | None:
    """Protection-relevant order facts; ``None`` when identity/side is unclear.

    Quantity, filled quantity, and price are included only when they parse as
    decimals -- omitted, never zero-filled, when the venue leaves them out.
    """
    order_id = row.get("client_order_id") or row.get("order_id") or row.get("id")
    if order_id is None or not str(order_id).strip():
        return None
    side = _PM_ORDER_SIDES.get(str(row.get("side") or "").upper())
    if side is None:
        return None
    sanitized: dict[str, Any] = {
        "order_id": str(order_id),
        "symbol": symbol,
        "side": side,
        "order_type": str(row.get("order_type") or row.get("type") or "UNKNOWN"),
        "status": str(row.get("status") or "UNKNOWN").upper(),
        "as_of_ms": now_ms,
    }
    for source_key, target_key in (
        ("quantity", "quantity"),
        ("amount", "quantity"),
        ("filled_quantity", "filled_quantity"),
        ("filled_amount", "filled_quantity"),
        ("executed_amount_base", "filled_quantity"),
        ("price", "price"),
        ("limit_price", "price"),
    ):
        if target_key in sanitized or row.get(source_key) is None:
            continue
        try:
            number = Decimal(str(row[source_key]))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if number.is_finite() and number >= 0:
            sanitized[target_key] = format(number, "f")
    reduce_only = row.get("reduce_only")
    if isinstance(reduce_only, bool):
        sanitized["reduce_only"] = reduce_only
    return sanitized


def _pm_margin_health(
    equity: Decimal, available_margin: Decimal, gross_exposure: Decimal
) -> str:
    """Conservative first calibration: free collateral decides the category."""
    if available_margin <= 0:
        return "CRITICAL"
    if equity > 0 and available_margin < equity * Decimal("0.2"):
        return "WARNING"
    if gross_exposure > 0 and available_margin <= 0:
        return "CRITICAL"
    return "SAFE"


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
    logs it and starts inert (no writes). The production PM context builder
    is wired as ``pm_load_context``; it stays fail-closed (``None``) without
    venue bindings or when ownership cannot be established unambiguously.
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
        log.warning("brooks_agents has no agent_key; Trader/HTF/PM roles stay idle.")
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
    supervisor.attach_pm(
        load_context=build_pm_load_context(
            client,
            account_name=account,
            connector_name=connector,
            controller_id=controller,
            state_root=Path(strategy_home),
        )
    )
    return WireResult(ok=True)
