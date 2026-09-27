"""Tests for condor.brooks.replay (FEAT-015).

Isolated: stdlib + pytest only, filesystem via tmp_path, no Hummingbot
API, no sibling brooks modules required.
"""

import json
from pathlib import Path

import pytest

from condor.brooks import replay
from condor.brooks.replay import (
    ReplayAmbiguityError,
    ReplayError,
    ReplayNotFoundError,
    build_restart_evidence,
    format_replay_text,
    parse_event,
    replay_events,
    replay_lifecycle,
    validate_linkage,
    verify_ownership,
)

CID = "corr-1"
SYMBOL = "BTC-USDT"


def _ev(event_id, type_, ts, causation_id=None, payload=None, cid=CID):
    return {
        "event_id": event_id,
        "type": type_,
        "created_at_ms": ts,
        "symbol": SYMBOL,
        "correlation_id": cid,
        "causation_id": causation_id,
        "payload": payload or {},
    }


def _lifecycle_events():
    return [
        _ev("e1", "H1_BAR_CLOSED", 1000),
        _ev(
            "e2",
            "TRADER_INTENT_CREATED",
            1001,
            causation_id="e1",
            payload={"action": "ENTER_LONG", "symbol": SYMBOL},
        ),
        _ev(
            "e3",
            "MARKET_CONTEXT_UPDATED",
            1002,
            payload={"trend": "up"},
        ),
        _ev("e4", "GM_ENTRY_APPROVED", 1003, causation_id="e2"),
        _ev("e5", "EXECUTION_SUBMITTED", 1004, causation_id="e4"),
        _ev("e6", "EXECUTION_CONFIRMED", 1005, causation_id="e5"),
        _ev("e7", "POSITION_OPENED", 1006, causation_id="e6"),
        _ev(
            "e8",
            "MANAGEMENT_INTENT_CREATED",
            1007,
            causation_id="e7",
            payload={"action": "HOLD"},
        ),
        _ev("e9", "FILL", 1008, causation_id="e6", payload={"qty": "1.0"}),
        _ev(
            "e10",
            "HEDGE_OPENED",
            1009,
            causation_id="e8",
            payload={"hedge_position_id": "hedge-456"},
        ),
    ]


def _write_state(home: Path, events, trade_files=None):
    state = home / "brooks_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    tdir = state / "trades" / CID
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "binding.json").write_text(
        json.dumps(
            {
                "correlation_id": CID,
                "symbol": SYMBOL,
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
            }
        ),
        encoding="utf-8",
    )
    (tdir / "original_trade_intent.json").write_text(
        json.dumps({"action": "ENTER_LONG", "symbol": SYMBOL}), encoding="utf-8"
    )
    (tdir / "hedge_state.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.hedge-state.v1",
                "structure_status": "ok",
                "unresolved": False,
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
                "main_side": "LONG",
                "hedge_side": "SHORT",
                "hedge_ratio": "0.30",
            }
        ),
        encoding="utf-8",
    )
    (tdir / "management_history.jsonl").write_text(
        json.dumps({"action": "HOLD"}) + "\n", encoding="utf-8"
    )
    (tdir / "executions.jsonl").write_text(
        json.dumps({"execution_id": "x1", "kind": "OPEN_MAIN"}) + "\n",
        encoding="utf-8",
    )
    for name, obj in (trade_files or {}).items():
        (tdir / name).write_text(json.dumps(obj), encoding="utf-8")


# --- happy path -----------------------------------------------------------


def test_replay_sections_and_thesis(tmp_path):
    _write_state(tmp_path, _lifecycle_events())
    result = replay_lifecycle(tmp_path, CID)
    assert result.correlation_id == CID
    assert result.symbol == SYMBOL
    assert len(result.events) == 10
    assert [e.event_id for e in result.sections["trader"]] == ["e1", "e2"]
    assert [e.event_id for e in result.sections["htf"]] == ["e3"]
    assert [e.event_id for e in result.sections["gm"]] == ["e4"]
    assert [e.event_id for e in result.sections["execution"]] == ["e5", "e6"]
    assert [e.event_id for e in result.sections["pm"]] == ["e7", "e8"]
    assert [e.event_id for e in result.sections["hedge"]] == ["e10"]
    assert [e.event_id for e in result.sections["fills"]] == ["e9"]
    assert result.original_trade_intent == {"action": "ENTER_LONG", "symbol": SYMBOL}
    assert result.binding["main_position_id"] == "main-123"
    assert result.hedge_state["hedge_position_id"] == "hedge-456"
    assert result.final_state["fill_count"] == 1
    assert result.final_state["execution_count"] == 1
    assert len(result.replay_hash) == 64


def test_replay_deterministic_hash_and_order(tmp_path):
    events = _lifecycle_events()
    _write_state(tmp_path, list(reversed(events)))
    first = replay_lifecycle(tmp_path, CID)
    # File order must not matter: replay sorts by (created_at_ms, event_id).
    assert [e.event_id for e in first.events] == [f"e{i}" for i in range(1, 11)]
    second = replay_lifecycle(tmp_path, CID)
    assert first.replay_hash == second.replay_hash
    altered = list(reversed(events)) + [
        _ev("e11", "FILL", 1010, causation_id="e6", payload={"qty": "0.1"})
    ]
    _write_state(tmp_path, altered)
    assert replay_lifecycle(tmp_path, CID).replay_hash != first.replay_hash


def test_other_correlation_ids_ignored(tmp_path):
    events = _lifecycle_events() + [
        _ev("zx", "TRADER_INTENT_CREATED", 1001, cid="other",
            payload={"action": "ENTER_SHORT"})
    ]
    _write_state(tmp_path, events)
    result = replay_lifecycle(tmp_path, CID)
    assert len(result.events) == 10
    # Same-intent repeats are fine; only conflicting thesis is ambiguous.
    dup = _lifecycle_events() + [
        _ev(
            "e2b",
            "TRADER_INTENT_CREATED",
            1002,
            causation_id="e1",
            payload={"action": "ENTER_LONG", "symbol": SYMBOL},
        )
    ]
    _write_state(tmp_path, dup)
    assert replay_lifecycle(tmp_path, CID).original_trade_intent["action"] == (
        "ENTER_LONG"
    )


def test_restart_evidence_reconstructs_thesis(tmp_path):
    _write_state(tmp_path, _lifecycle_events())
    evidence = build_restart_evidence(tmp_path, CID)
    assert evidence.correlation_id == CID
    assert evidence.main_position_id == "main-123"
    assert evidence.hedge_position_id == "hedge-456"
    assert evidence.thesis == {"action": "ENTER_LONG", "symbol": SYMBOL}
    assert evidence.last_execution_cursor == {
        "execution_id": "x1",
        "kind": "OPEN_MAIN",
    }
    assert evidence.management_cursor == {"action": "HOLD"}
    assert len(evidence.replay_hash) == 64


def test_format_replay_text_is_deterministic(tmp_path):
    _write_state(tmp_path, _lifecycle_events())
    result = replay_lifecycle(tmp_path, CID)
    text = format_replay_text(result)
    assert "lifecycle corr-1" in text
    assert "== trader (2) ==" in text
    assert "== fills (1) ==" in text
    assert format_replay_text(result) == text


def test_missing_trade_dir_still_replays_events(tmp_path):
    state = tmp_path / "brooks_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "events.jsonl").write_text(
        json.dumps(_ev("e1", "TRADER_INTENT_CREATED", 1000,
                       payload={"action": "ENTER_LONG"})) + "\n",
        encoding="utf-8",
    )
    result = replay_lifecycle(tmp_path, CID)
    assert result.binding is None
    assert result.original_trade_intent == {"action": "ENTER_LONG"}


# --- fail closed: linkage --------------------------------------------------


def test_duplicate_event_id_fails_closed():
    events = [_ev("e1", "H1_BAR_CLOSED", 1000), _ev("e1", "H1_BAR_CLOSED", 1001)]
    with pytest.raises(ReplayAmbiguityError, match="duplicate event_id"):
        replay_events(events, CID)


def test_dangling_causation_id_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="dangling causation_id"):
        replay_events([_ev("e1", "H1_BAR_CLOSED", 1000, causation_id="ghost")], CID)


def test_self_causation_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="causes itself"):
        replay_events([_ev("e1", "H1_BAR_CLOSED", 1000, causation_id="e1")], CID)


def test_malformed_envelope_rejected():
    with pytest.raises(ReplayError, match="missing required fields"):
        parse_event({"type": "H1_BAR_CLOSED"})
    with pytest.raises(ReplayError, match="created_at_ms"):
        parse_event(_ev("e1", "H1_BAR_CLOSED", "not-an-int"))
    with pytest.raises(ReplayError):
        validate_linkage([]) if False else parse_event(
            _ev("e1", "H1_BAR_CLOSED", 1000, causation_id="")
        )


def test_unknown_correlation_id_not_found(tmp_path):
    _write_state(tmp_path, _lifecycle_events())
    with pytest.raises(ReplayNotFoundError):
        replay_lifecycle(tmp_path, "nope")


def test_malformed_jsonl_line_fails(tmp_path):
    state = tmp_path / "brooks_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "events.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="unparseable JSON"):
        replay_lifecycle(tmp_path, CID)


def test_mixed_symbols_fail_closed():
    events = [
        _ev("e1", "TRADER_INTENT_CREATED", 1000, payload={"action": "ENTER_LONG"}),
        dict(
            _ev("e2", "TRADER_INTENT_CREATED", 1001,
                payload={"action": "ENTER_LONG"}),
            symbol="ETH-USDT",
        ),
    ]
    with pytest.raises(ReplayAmbiguityError, match="spans symbols"):
        replay_events(events, CID)


# --- fail closed: thesis ---------------------------------------------------


def test_conflicting_original_intents_fail_closed():
    events = [
        _ev("e1", "TRADER_INTENT_CREATED", 1000,
            payload={"action": "ENTER_LONG", "symbol": SYMBOL}),
        _ev("e2", "TRADER_INTENT_CREATED", 1001,
            payload={"action": "ENTER_SHORT", "symbol": SYMBOL}),
    ]
    with pytest.raises(ReplayAmbiguityError, match="conflicting original"):
        replay_events(events, CID)


def test_durable_thesis_disagreement_fails_closed(tmp_path):
    _write_state(
        tmp_path,
        _lifecycle_events(),
        trade_files={
            "original_trade_intent.json": {
                "action": "ENTER_SHORT",
                "symbol": SYMBOL,
            }
        },
    )
    with pytest.raises(ReplayAmbiguityError, match="disagrees with the event stream"):
        replay_lifecycle(tmp_path, CID)


# --- fail closed: ownership -----------------------------------------------


def test_same_side_main_hedge_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="same side"):
        verify_ownership(
            {
                "structure_status": "ok",
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
                "main_side": "LONG",
                "hedge_side": "LONG",
            }
        )


def test_orphan_hedge_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="orphan HEDGE"):
        verify_ownership(
            {"structure_status": "ok", "hedge_position_id": "hedge-456"}
        )


def test_unresolved_structure_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="unresolved"):
        verify_ownership({"structure_status": "unknown", "unresolved": True})


@pytest.mark.parametrize("ratio", ["1.5", "-0.1", "2", "abc"])
def test_ratio_out_of_range_fails_closed(ratio):
    with pytest.raises(ReplayAmbiguityError):
        verify_ownership(
            {
                "structure_status": "ok",
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
                "main_side": "LONG",
                "hedge_side": "SHORT",
                "hedge_ratio": ratio,
            }
        )


def test_binding_disagreement_fails_closed():
    with pytest.raises(ReplayAmbiguityError, match="disagrees"):
        verify_ownership(
            {
                "structure_status": "ok",
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
                "main_side": "LONG",
                "hedge_side": "SHORT",
            },
            {"main_position_id": "main-999"},
        )


def test_replay_with_bad_hedge_state_fails_closed(tmp_path):
    _write_state(
        tmp_path,
        _lifecycle_events(),
        trade_files={
            "hedge_state.json": {
                "structure_status": "ok",
                "main_position_id": "main-123",
                "hedge_position_id": "hedge-456",
                "main_side": "LONG",
                "hedge_side": "LONG",
            }
        },
    )
    with pytest.raises(ReplayAmbiguityError):
        replay_lifecycle(tmp_path, CID)


def test_restart_without_binding_fails_closed(tmp_path):
    _write_state(tmp_path, _lifecycle_events())
    (tmp_path / "brooks_state" / "trades" / CID / "binding.json").unlink()
    with pytest.raises(ReplayAmbiguityError, match="binding.json missing"):
        build_restart_evidence(tmp_path, CID)


def test_restart_without_thesis_fails_closed(tmp_path):
    state = tmp_path / "brooks_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "events.jsonl").write_text(
        json.dumps(_ev("e1", "POSITION_OPENED", 1000)) + "\n", encoding="utf-8"
    )
    tdir = state / "trades" / CID
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "binding.json").write_text(
        json.dumps({"main_position_id": "main-123"}), encoding="utf-8"
    )
    with pytest.raises(ReplayAmbiguityError, match="thesis unrecoverable"):
        build_restart_evidence(tmp_path, CID)


# --- isolation --------------------------------------------------------------


def test_module_is_stdlib_only():
    import ast

    import condor.brooks.replay as mod

    assert "condor.brooks" not in [
        name for name in dir(mod) if name.startswith("condor")
    ]
    source = (Path(mod.__file__).parent / "replay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.add((node.module or "").split(".")[0])
    assert top_level <= {
        "__future__",
        "hashlib",
        "json",
        "dataclasses",
        "pathlib",
        "typing",
    }, f"non-stdlib top-level imports: {sorted(top_level)}"
