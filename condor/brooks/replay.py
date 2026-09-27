"""Deterministic JSONL lifecycle replay and restart evidence (FEAT-015).

Isolated module: standard library only. It never imports sibling
``condor.brooks`` modules, never touches the Hummingbot API, and never
performs network I/O. It reads the durable files described in the plan
(``brooks_state/events.jsonl`` plus ``brooks_state/trades/<id>/...``) and
reconstructs one trade lifecycle keyed by ``correlation_id``.

Fail-closed rule: any ambiguity (duplicate ``event_id``, broken
``causation_id`` chain, conflicting original thesis, unresolvable
MAIN/HEDGE ownership) raises :class:`ReplayAmbiguityError` instead of
returning a best guess. MAIN/HEDGE ownership is taken only from the
explicit ``binding.json`` / ``hedge_state.json`` documents, never inferred
from side, size, ordering, PnL, or model opinion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

HEDGE_STATE_SCHEMA_V1 = "condor.brooks.hedge-state.v1"

EVENTS_FILE = "events.jsonl"
TRADES_DIR = "trades"

# Section routing for the FEAT-015 evidence view. Unknown types still
# appear in the replay (under "other") so nothing is silently dropped.
TRADER_TYPES = frozenset({"TRADER_INTENT_CREATED", "H1_BAR_CLOSED"})
HTF_TYPES = frozenset({"MARKET_CONTEXT_UPDATED", "D1_BAR_CLOSED"})
GM_TYPES = frozenset(
    {
        "GM_ENTRY_APPROVED",
        "GM_ENTRY_REJECTED",
        "GM_MANAGEMENT_APPROVED",
        "GM_MANAGEMENT_REJECTED",
        "RECONCILIATION_REQUIRED",
    }
)
EXECUTION_TYPES = frozenset(
    {"EXECUTION_SUBMITTED", "EXECUTION_CONFIRMED", "EXECUTION_FAILED"}
)
PM_TYPES = frozenset(
    {
        "MANAGEMENT_INTENT_CREATED",
        "PM_TIMER",
        "POSITION_OPENED",
        "POSITION_CHANGED",
        "POSITION_CLOSED",
        "ORDER_CHANGED",
        "REQUEST_MARKET_ANALYSIS",
    }
)
HEDGE_TYPES = frozenset({"HEDGE_OPENED", "HEDGE_CHANGED", "HEDGE_REMOVED"})
FILL_TYPES = frozenset({"FILL"})

SECTION_ORDER = (
    "trader",
    "htf",
    "gm",
    "execution",
    "pm",
    "hedge",
    "fills",
    "other",
)

_TYPE_TO_SECTION = {
    **{t: "trader" for t in TRADER_TYPES},
    **{t: "htf" for t in HTF_TYPES},
    **{t: "gm" for t in GM_TYPES},
    **{t: "execution" for t in EXECUTION_TYPES},
    **{t: "pm" for t in PM_TYPES},
    **{t: "hedge" for t in HEDGE_TYPES},
    **{t: "fills" for t in FILL_TYPES},
}

# Ownership is explicit or it does not exist. These statuses mean the
# hedge state document itself declares the structure unusable.
_BAD_STRUCTURE = frozenset({"unknown", "ambiguous", "conflicted", "error"})


class ReplayError(Exception):
    """Base error for replay/restart-evidence failures."""


class ReplayNotFoundError(ReplayError):
    """No durable data exists for the requested correlation id."""


class ReplayAmbiguityError(ReplayError):
    """Durable data is ambiguous; fail closed instead of guessing."""


@dataclass(frozen=True)
class ReplayEvent:
    """One validated event envelope (§8)."""

    event_id: str
    type: str
    created_at_ms: int
    symbol: str
    correlation_id: str
    causation_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class LifecycleReplay:
    """Deterministic reconstruction of one trade lifecycle."""

    correlation_id: str
    symbol: str
    events: tuple[ReplayEvent, ...]
    sections: Mapping[str, tuple[ReplayEvent, ...]]
    original_trade_intent: Mapping[str, Any] | None
    latest_trade_intent: Mapping[str, Any] | None
    latest_market_context: Mapping[str, Any] | None
    binding: Mapping[str, Any] | None
    hedge_state: Mapping[str, Any] | None
    management_history: tuple[Mapping[str, Any], ...]
    executions: tuple[Mapping[str, Any], ...]
    fills: tuple[ReplayEvent, ...]
    final_state: Mapping[str, Any]
    replay_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestartEvidence:
    """Everything a restart needs to resume one lifecycle safely."""

    correlation_id: str
    symbol: str
    main_position_id: str | None
    hedge_position_id: str | None
    thesis: Mapping[str, Any]
    hedge_state: Mapping[str, Any] | None
    last_execution_cursor: Mapping[str, Any] | None
    management_cursor: Mapping[str, Any] | None
    replay_hash: str


def parse_event(obj: Mapping[str, Any]) -> ReplayEvent:
    """Validate one raw envelope dict into a :class:`ReplayEvent`.

    Raises :class:`ReplayError` on a malformed envelope. Cross-event
    ambiguity (duplicates, broken chains) is detected by
    :func:`validate_linkage`, not here.
    """
    if not isinstance(obj, Mapping):
        raise ReplayError(f"event must be a JSON object, got {type(obj).__name__}")
    required = ("event_id", "type", "created_at_ms", "correlation_id", "payload")
    missing = [k for k in required if k not in obj]
    if missing:
        raise ReplayError(f"event missing required fields: {sorted(missing)}")
    event_id = obj["event_id"]
    if not isinstance(event_id, str) or not event_id:
        raise ReplayError("event_id must be a non-empty string")
    created_at_ms = obj["created_at_ms"]
    if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int):
        raise ReplayError(f"event {event_id!r}: created_at_ms must be an int")
    if not isinstance(obj["payload"], Mapping):
        raise ReplayError(f"event {event_id!r}: payload must be a JSON object")
    causation_id = obj.get("causation_id")
    if causation_id is not None and (
        not isinstance(causation_id, str) or not causation_id
    ):
        raise ReplayError(f"event {event_id!r}: causation_id must be a string or null")
    return ReplayEvent(
        event_id=event_id,
        type=str(obj["type"]),
        created_at_ms=created_at_ms,
        symbol=str(obj.get("symbol", "")),
        correlation_id=str(obj["correlation_id"]),
        causation_id=causation_id,
        payload=obj["payload"],
    )


def validate_linkage(events: list[ReplayEvent]) -> None:
    """Fail closed on ambiguous event linkage.

    Checks: unique ``event_id``, uniform ``correlation_id``, and every
    non-null ``causation_id`` referencing a known ``event_id`` in the same
    stream. Raises :class:`ReplayAmbiguityError` on any violation.
    """
    seen: dict[str, ReplayEvent] = {}
    for ev in events:
        if ev.event_id in seen:
            raise ReplayAmbiguityError(f"duplicate event_id {ev.event_id!r}")
        seen[ev.event_id] = ev
    correlations = {ev.correlation_id for ev in events}
    if len(correlations) > 1:
        raise ReplayAmbiguityError(
            f"mixed correlation_id in one stream: {sorted(correlations)}"
        )
    for ev in events:
        if ev.causation_id is not None and ev.causation_id not in seen:
            raise ReplayAmbiguityError(
                f"event {ev.event_id!r} has dangling "
                f"causation_id {ev.causation_id!r}"
            )
        if ev.causation_id == ev.event_id:
            raise ReplayAmbiguityError(
                f"event {ev.event_id!r} causes itself"
            )


def _canonical_hash(events: list[ReplayEvent]) -> str:
    canonical = json.dumps(
        [
            {
                "event_id": ev.event_id,
                "type": ev.type,
                "created_at_ms": ev.created_at_ms,
                "correlation_id": ev.correlation_id,
                "causation_id": ev.causation_id,
                "payload": _jsonable(ev.payload),
            }
            for ev in events
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _section_of(event_type: str) -> str:
    return _TYPE_TO_SECTION.get(event_type, "other")


def replay_events(
    raw_events: list[Mapping[str, Any] | ReplayEvent],
    correlation_id: str,
    *,
    binding: Mapping[str, Any] | None = None,
    hedge_state: Mapping[str, Any] | None = None,
    management_history: list[Mapping[str, Any]] | None = None,
    executions: list[Mapping[str, Any]] | None = None,
) -> LifecycleReplay:
    """Pure, deterministic replay over in-memory events.

    Filters by ``correlation_id``, orders by ``(created_at_ms, event_id)``,
    validates linkage, splits into FEAT-015 sections, and extracts the
    thesis (first ``TRADER_INTENT_CREATED`` payload). Conflicting original
    intents or unresolvable ownership raise
    :class:`ReplayAmbiguityError`. Empty stream raises
    :class:`ReplayNotFoundError`.
    """
    if not correlation_id:
        raise ReplayError("correlation_id must be a non-empty string")
    parsed = [
        ev if isinstance(ev, ReplayEvent) else parse_event(ev) for ev in raw_events
    ]
    stream = [ev for ev in parsed if ev.correlation_id == correlation_id]
    if not stream:
        raise ReplayNotFoundError(
            f"no events found for correlation_id {correlation_id!r}"
        )
    stream.sort(key=lambda ev: (ev.created_at_ms, ev.event_id))
    validate_linkage(stream)

    symbols = {ev.symbol for ev in stream if ev.symbol}
    if len(symbols) > 1:
        raise ReplayAmbiguityError(
            f"stream {correlation_id!r} spans symbols: {sorted(symbols)}"
        )

    sections: dict[str, list[ReplayEvent]] = {name: [] for name in SECTION_ORDER}
    for ev in stream:
        sections[_section_of(ev.type)].append(ev)

    intents = [
        ev.payload for ev in sections["trader"] if ev.type == "TRADER_INTENT_CREATED"
    ]
    original = intents[0] if intents else None
    latest = intents[-1] if intents else None
    if original is not None:
        for other in intents[1:]:
            if _intent_identity(other) != _intent_identity(original):
                raise ReplayAmbiguityError(
                    f"conflicting original trade intents in {correlation_id!r}: "
                    "refusing to pick a thesis"
                )
    contexts = [
        ev.payload
        for ev in sections["htf"]
        if ev.type == "MARKET_CONTEXT_UPDATED"
    ]
    latest_context = contexts[-1] if contexts else None

    mgmt_history = tuple(management_history or ())
    execs = tuple(executions or ())
    fills = tuple(sections["fills"])

    if hedge_state is not None:
        verify_ownership(hedge_state, binding)

    final_state: dict[str, Any] = {
        "correlation_id": correlation_id,
        "event_count": len(stream),
        "last_event_id": stream[-1].event_id,
        "last_event_type": stream[-1].type,
        "last_created_at_ms": stream[-1].created_at_ms,
        "has_open_main": any(
            ev.type in ("POSITION_OPENED", "POSITION_CHANGED") for ev in sections["pm"]
        )
        and not any(ev.type == "POSITION_CLOSED" for ev in sections["pm"]),
        "hedge_open": any(ev.type == "HEDGE_OPENED" for ev in sections["hedge"])
        and not any(ev.type == "HEDGE_REMOVED" for ev in sections["hedge"]),
        "fill_count": len(fills),
        "execution_count": len(execs),
    }
    if hedge_state is not None:
        final_state["hedge_state"] = dict(hedge_state)

    return LifecycleReplay(
        correlation_id=correlation_id,
        symbol=next(iter(symbols)) if symbols else "",
        events=tuple(stream),
        sections={k: tuple(v) for k, v in sections.items()},
        original_trade_intent=dict(original) if original else None,
        latest_trade_intent=dict(latest) if latest else None,
        latest_market_context=dict(latest_context) if latest_context else None,
        binding=dict(binding) if binding else None,
        hedge_state=dict(hedge_state) if hedge_state else None,
        management_history=mgmt_history,
        executions=execs,
        fills=fills,
        final_state=final_state,
        replay_hash=_canonical_hash(stream),
        warnings=(),
    )


def _intent_identity(intent: Mapping[str, Any]) -> Any:
    """Identity fields that must agree across repeated intent records."""
    return json.dumps(
        {
            k: intent.get(k)
            for k in ("action", "symbol", "side", "entry", "invalidation")
        },
        sort_keys=True,
        default=str,
    )


def verify_ownership(
    hedge_state: Mapping[str, Any],
    binding: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed unless MAIN/HEDGE ownership is explicit and coherent.

    Never infers ownership from side, size, ordering, or PnL: the ids must
    be stated in ``hedge_state`` (and agree with ``binding`` when both are
    present). Any violation raises :class:`ReplayAmbiguityError`.
    """
    if not isinstance(hedge_state, Mapping):
        raise ReplayAmbiguityError("hedge_state must be a JSON object")
    status = str(hedge_state.get("structure_status", ""))
    if status.lower() in _BAD_STRUCTURE or bool(hedge_state.get("unresolved", False)):
        raise ReplayAmbiguityError(
            f"hedge structure unresolved (status={status!r}); "
            "hedge actions blocked until reconciled"
        )
    main_id = hedge_state.get("main_position_id")
    hedge_id = hedge_state.get("hedge_position_id")
    main_side = hedge_state.get("main_side")
    hedge_side = hedge_state.get("hedge_side")
    if hedge_id and not main_id:
        raise ReplayAmbiguityError("orphan HEDGE without MAIN: ownership unresolved")
    if main_id and hedge_id and main_id == hedge_id:
        raise ReplayAmbiguityError("MAIN and HEDGE share one position id")
    if main_id and hedge_id and main_side and hedge_side and main_side == hedge_side:
        raise ReplayAmbiguityError(
            f"MAIN/HEDGE on the same side ({main_side!r}); ownership ambiguous"
        )
    ratio = hedge_state.get("hedge_ratio", hedge_state.get("target_hedge_ratio"))
    if ratio is not None:
        try:
            ratio_f = float(ratio)
        except (TypeError, ValueError):
            raise ReplayAmbiguityError(
                f"hedge ratio {ratio!r} is not numeric"
            ) from None
        if not 0.0 <= ratio_f <= 1.0:
            raise ReplayAmbiguityError(
                f"hedge ratio {ratio_f} outside [0, 1]"
            )
    if binding is not None:
        for key in ("main_position_id", "hedge_position_id"):
            doc_val = hedge_state.get(key)
            bind_val = binding.get(key)
            if doc_val and bind_val and doc_val != bind_val:
                raise ReplayAmbiguityError(
                    f"{key} disagrees: hedge_state={doc_val!r} "
                    f"binding={bind_val!r}"
                )


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"unparseable JSON in {path}: {exc}") from exc
    if not isinstance(obj, Mapping):
        raise ReplayError(f"{path} must contain a JSON object")
    return obj


def _read_jsonl(path: Path, *, what: str) -> list[Mapping[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[Mapping[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(
                f"unparseable JSON in {path}:{lineno} ({what}): {exc}"
            ) from exc
        if not isinstance(obj, Mapping):
            raise ReplayError(
                f"{path}:{lineno} ({what}) must be a JSON object"
            )
        rows.append(obj)
    return rows


def load_trade_dir(
    strategy_home: str | Path, correlation_id: str
) -> dict[str, Any]:
    """Read the durable per-trade directory (missing files -> None/empty)."""
    trade_dir = Path(strategy_home) / "brooks_state" / TRADES_DIR / correlation_id
    return {
        "binding": _read_json(trade_dir / "binding.json"),
        "original_trade_intent": _read_json(trade_dir / "original_trade_intent.json"),
        "latest_management_intent": _read_json(
            trade_dir / "latest_management_intent.json"
        ),
        "hedge_state": _read_json(trade_dir / "hedge_state.json"),
        "management_history": _read_jsonl(
            trade_dir / "management_history.jsonl", what="management history"
        ),
        "executions": _read_jsonl(trade_dir / "executions.jsonl", what="executions"),
    }


def replay_lifecycle(
    strategy_home: str | Path, correlation_id: str
) -> LifecycleReplay:
    """Replay one lifecycle from durable files on disk.

    Reads ``brooks_state/events.jsonl`` plus the per-trade directory,
    then delegates to :func:`replay_events`. Raises
    :class:`ReplayNotFoundError` when nothing exists for the id and
    :class:`ReplayAmbiguityError` on any ambiguity.
    """
    if not correlation_id:
        raise ReplayError("correlation_id must be a non-empty string")
    state_dir = Path(strategy_home) / "brooks_state"
    try:
        raw = (state_dir / EVENTS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    events: list[Mapping[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(
                f"unparseable JSON in events.jsonl:{lineno}: {exc}"
            ) from exc
        if not isinstance(obj, Mapping):
            raise ReplayError(f"events.jsonl:{lineno} must be a JSON object")
        events.append(obj)
    trade = load_trade_dir(strategy_home, correlation_id)
    replay = replay_events(
        events,
        correlation_id,
        binding=trade["binding"],
        hedge_state=trade["hedge_state"],
        management_history=trade["management_history"],
        executions=trade["executions"],
    )
    # Prefer the durable thesis files when present; they must agree with
    # the event-stream thesis or the run is ambiguous.
    durable_thesis = trade["original_trade_intent"]
    if durable_thesis is not None and replay.original_trade_intent is not None:
        if _intent_identity(
            durable_thesis
        ) != _intent_identity(replay.original_trade_intent):
            raise ReplayAmbiguityError(
                f"durable original_trade_intent.json disagrees with the "
                f"event stream for {correlation_id!r}"
            )
    return replay


def build_restart_evidence(
    strategy_home: str | Path, correlation_id: str
) -> RestartEvidence:
    """Reconstruct restart evidence for one lifecycle, failing closed.

    The MAIN/HEDGE thesis comes only from ``binding.json``,
    ``hedge_state.json``, and ``original_trade_intent.json``. Anything
    missing that is required, or anything ambiguous, raises instead of
    guessing, so a restarted run can never resume the wrong position.
    """
    replay = replay_lifecycle(strategy_home, correlation_id)
    trade = load_trade_dir(strategy_home, correlation_id)
    binding = trade["binding"]
    if binding is None:
        raise ReplayAmbiguityError(
            f"cannot resume {correlation_id!r}: binding.json missing"
        )
    thesis = trade["original_trade_intent"] or replay.original_trade_intent
    if thesis is None:
        raise ReplayAmbiguityError(
            f"cannot resume {correlation_id!r}: original thesis unrecoverable"
        )
    main_id = binding.get("main_position_id")
    hedge_doc = trade["hedge_state"]
    hedge_id = (hedge_doc or {}).get(
        "hedge_position_id", binding.get("hedge_position_id")
    )
    if hedge_id and not main_id:
        raise ReplayAmbiguityError(
            f"cannot resume {correlation_id!r}: orphan HEDGE without MAIN"
        )
    execs = trade["executions"]
    mgmt = trade["management_history"]
    durable_mgmt = trade["latest_management_intent"]
    return RestartEvidence(
        correlation_id=correlation_id,
        symbol=replay.symbol or str(binding.get("symbol", "")),
        main_position_id=main_id,
        hedge_position_id=hedge_id,
        thesis=dict(thesis),
        hedge_state=dict(hedge_doc) if hedge_doc else None,
        last_execution_cursor=dict(execs[-1]) if execs else None,
        management_cursor=(
            dict(durable_mgmt)
            if durable_mgmt is not None
            else (dict(mgmt[-1]) if mgmt else None)
        ),
        replay_hash=replay.replay_hash,
    )


def format_replay_text(replay: LifecycleReplay) -> str:
    """Render a deterministic, human-readable evidence summary."""
    lines = [
        f"lifecycle {replay.correlation_id} "
        f"symbol={replay.symbol or '?'} "
        f"events={len(replay.events)} hash={replay.replay_hash[:12]}",
    ]
    for section in SECTION_ORDER:
        events = replay.sections.get(section, ())
        if not events:
            continue
        lines.append(f"== {section} ({len(events)}) ==")
        for ev in events:
            lines.append(
                f"  {ev.created_at_ms} {ev.type} "
                f"id={ev.event_id} cause={ev.causation_id or '-'}"
            )
    if replay.original_trade_intent is not None:
        lines.append(f"thesis: {_intent_identity(replay.original_trade_intent)}")
    lines.append(f"final: {json.dumps(replay.final_state, sort_keys=True)}")
    return "\n".join(lines)


__all__ = [
    "HEDGE_STATE_SCHEMA_V1",
    "LifecycleReplay",
    "ReplayAmbiguityError",
    "ReplayError",
    "ReplayEvent",
    "ReplayNotFoundError",
    "RestartEvidence",
    "build_restart_evidence",
    "format_replay_text",
    "load_trade_dir",
    "parse_event",
    "replay_events",
    "replay_lifecycle",
    "validate_linkage",
    "verify_ownership",
]
