"""Lifecycle release requires repeated fresh venue proof, including on restart."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from condor.brooks.adapters import (
    HummingbotAccountReader,
    HummingbotPositionReconciler,
    build_watcher_provider,
    read_bindings,
)
from condor.brooks.events import EventType
from condor.brooks.gm import BrooksGM, GMRejected
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.supervisor import GMConsumer
from tests.brooks_e2e_harness import (
    ACCOUNT,
    CONNECTOR,
    CONTROLLER,
    SYMBOL,
    E2EWorld,
    h1_due_now,
    make_entry_intent,
    open_main_position,
    run,
    seed_reconciled_hedge,
)


def _bindings(world: E2EWorld) -> list[dict]:
    return read_bindings(
        world.root,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
    )


def _flat_main(world: E2EWorld, correlation_id: str, *, status="CLOSED") -> dict:
    binding = open_main_position(world, correlation_id, h1_due_now())
    main_executor_id = binding["main_executor_id"]
    world.venue.positions.clear()
    for row in world.venue.executor_rows:
        if row.get("executor_id") == main_executor_id:
            row["status"] = status
    return binding


def _orphan_hedge(world: E2EWorld, correlation_id: str) -> dict:
    binding = open_main_position(world, correlation_id, h1_due_now())
    main_id = binding["main_position_id"]
    main_executor_id = binding["main_executor_id"]
    binding = seed_reconciled_hedge(world, correlation_id, "0.20")
    hedge_id = binding["hedge_position_id"]
    world.venue.positions[:] = [
        row for row in world.venue.positions if row.get("position_id") == hedge_id
    ]
    for row in world.venue.executor_rows:
        if row.get("executor_id") == main_executor_id:
            row["status"] = "CLOSED"
    world.venue.executor_rows.append(
        {
            "executor_id": binding["hedge_executor_id"],
            "account_name": ACCOUNT,
            "connector_name": CONNECTOR,
            "controller_id": CONTROLLER,
            "trading_pair": SYMBOL,
            "position_id": hedge_id,
            "status": "TERMINATED",
        }
    )
    assert main_id and hedge_id
    return binding


def _restart_gm(world: E2EWorld, execution) -> BrooksGM:
    reader = HummingbotAccountReader(world.venue, world.root, CONTROLLER)
    reconciler = HummingbotPositionReconciler(world.venue, CONTROLLER)
    return BrooksGM(
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        state_root=world.root,
        policy=world.gm.policy,
        reader=reader,
        execution=execution,
        reconciler=reconciler,
    )


def test_flat_terminal_main_closes_archives_ids_and_allows_new_entry(tmp_path):
    world = E2EWorld(tmp_path)
    old = _flat_main(world, "old-trade")

    closed = run(world.gm.reconcile_lifecycle("old-trade"))

    assert closed["status"] == "closed"
    assert closed["closed_main_position_id"] == old["main_position_id"]
    assert closed["closed_main_executor_id"] == old["main_executor_id"]
    assert closed["closed_executor_id"] == old["main_executor_id"]
    assert closed["main_position_id"] is None
    assert closed["main_executor_id"] is None
    assert _bindings(world) == []

    new = run(
        world.gm.execute_entry(
            make_entry_intent(h1_due_now()), correlation_id="new-trade"
        )
    )
    assert new["status"] == "submitted"
    assert new["correlation_id"] == "new-trade"


def test_restart_flat_first_watcher_poll_invokes_lifecycle_callback(tmp_path):
    world = E2EWorld(tmp_path)
    _flat_main(world, "restart-flat")
    closed_events = world.bus.subscribe({EventType.BINDING_CLOSED})
    restarted_gm = _restart_gm(world, world.port)
    restarted_consumer = GMConsumer(
        gm_factory=lambda symbol: restarted_gm,
        publish=world.bus,
    )
    provider = build_watcher_provider(
        world.venue,
        account_name=ACCOUNT,
        connector_name=CONNECTOR,
        controller_id=CONTROLLER,
        symbols=[SYMBOL],
        state_root=world.root,
    )
    restarted_watcher = PositionWatcher(
        provider,
        world.bus,
        on_snapshot=restarted_consumer.reconcile_bound_snapshot,
    )

    run(restarted_watcher.poll())

    (event,) = world.drain(closed_events)
    assert event.type == EventType.BINDING_CLOSED
    assert event.correlation_id == "restart-flat"
    assert event.payload["binding"]["status"] == "closed"
    assert _bindings(world) == []


@pytest.mark.parametrize("status", ["TERMINATED", "RUNNING"])
def test_executor_terminal_before_initial_position_reconciliation(tmp_path, status):
    world = E2EWorld(tmp_path)
    binding = _flat_main(world, "closed-before-reconcile", status=status)
    path = world.root / "trades" / binding["correlation_id"] / "binding.json"
    binding.update(status="submitted", main_position_id=None)
    path.write_text(json.dumps(binding))
    if status == "RUNNING":
        with pytest.raises(GMRejected, match="incomplete or unresolved"):
            run(world.gm.reconcile_lifecycle(binding["correlation_id"]))
        assert _bindings(world)[0]["status"] == "submitted"
    else:
        closed = run(world.gm.reconcile_lifecycle(binding["correlation_id"]))
        assert closed["status"] == "closed"
        assert closed["closed_main_executor_id"] == binding["main_executor_id"]
        assert _bindings(world) == []


@pytest.mark.parametrize(
    ("venue_status", "settled"),
    [
        ("RUNNING", False),
        ("SHUTTING_DOWN", False),
        (3, False),
        (99, False),
        (4, True),
    ],
)
def test_flat_main_releases_only_after_terminal_executor_status(
    tmp_path, venue_status, settled
):
    world = E2EWorld(tmp_path)
    binding = _flat_main(world, f"status-{venue_status}", status=venue_status)

    result = run(world.gm.reconcile_lifecycle(binding["correlation_id"]))

    assert (result["status"] == "closed") is settled
    if settled:
        assert result["closed_main_executor_id"] == binding["main_executor_id"]
        assert _bindings(world) == []
    else:
        assert result["status"] == "reconciled"
        assert result["main_executor_id"] == binding["main_executor_id"]
        assert _bindings(world)[0]["correlation_id"] == binding["correlation_id"]


@pytest.mark.parametrize(
    ("reader_call", "result"),
    [
        ("positions", {"pagination": {"next_cursor": "next"}}),
        ("positions", {"data": [], "next_cursor": "next"}),
        ("positions", {"data": [], "pagination": {"next_cursor": "next"}}),
        ("executors", {"pagination": {"next_cursor": "next"}}),
        ("executors", {"data": [], "next_cursor": "next"}),
        ("executors", {"data": [], "pagination": {"next_cursor": "next"}}),
        ("orders", {"pagination": {"next_cursor": "next"}}),
        ("orders", {"data": [], "next_cursor": "next"}),
        ("orders", {"data": [], "pagination": {"next_cursor": "next"}}),
    ],
)
def test_incomplete_or_truncated_reader_evidence_never_releases_binding(
    tmp_path, reader_call, result
):
    world = E2EWorld(tmp_path)
    binding = _flat_main(world, f"bad-read-{reader_call}-{len(result)}")

    async def incomplete(**kwargs):
        return result

    if reader_call == "positions":
        world.venue.trading.get_positions = incomplete
    elif reader_call == "orders":
        world.venue.trading.get_active_orders = incomplete
    else:
        world.venue.executors.search_executors = incomplete

    with pytest.raises(GMRejected):
        run(world.gm.reconcile_lifecycle(binding["correlation_id"]))

    current = json.loads(
        (world.root / "trades" / binding["correlation_id"] / "binding.json").read_text()
    )
    assert current["status"] == "reconciled"
    assert "closed_at_ms" not in current
    assert _bindings(world)[0]["correlation_id"] == binding["correlation_id"]


def test_exactly_owned_orphan_hedge_receives_one_close_then_reconciles(tmp_path):
    world = E2EWorld(tmp_path)
    binding = _orphan_hedge(world, "orphan-close")

    first = run(world.gm.reconcile_lifecycle("orphan-close"))

    writes = [(kind, args) for kind, args in world.port.calls if kind == "hedge"]
    assert len(writes) == 1
    kwargs = writes[0][1]
    assert kwargs["position_action"] == "CLOSE"
    assert kwargs["quantity"] == Decimal("0.20")
    assert kwargs["side"] == "BUY"
    assert first["status"] == "main_closed"
    cleanup = json.loads(
        (world.root / "trades" / "orphan-close" / "lifecycle_cleanup.json").read_text()
    )
    assert cleanup["status"] == "submitted"
    assert cleanup["hedge_position_id"] == binding["hedge_position_id"]

    # The simulated close executor is still running until the venue reports its
    # terminal state; the second fresh proof can then archive the binding.
    cleanup_executor_id = cleanup["executor_id"]
    for row in world.venue.executor_rows:
        if row.get("executor_id") == cleanup_executor_id:
            row["status"] = "CLOSED"
    closed = run(world.gm.reconcile_lifecycle("orphan-close"))
    assert closed["status"] == "closed"
    assert len([row for row in world.port.calls if row[0] == "hedge"]) == 1


def test_shadow_lifecycle_cleanup_performs_zero_venue_writes(tmp_path):
    world = E2EWorld(tmp_path)
    _orphan_hedge(world, "shadow-cleanup")
    before = len(world.port.calls)

    result = run(world.gm.reconcile_lifecycle("shadow-cleanup", shadow_mode=True))

    assert result["status"] == "main_closed"
    assert len(world.port.calls) == before
    assert not (
        world.root / "trades" / "shadow-cleanup" / "lifecycle_cleanup.json"
    ).exists()


def test_orphan_hedge_timeout_record_is_not_retried_after_restart(tmp_path):
    world = E2EWorld(tmp_path)
    _orphan_hedge(world, "timeout-cleanup")
    attempts: list[dict] = []

    class TimeoutAfterSubmit:
        controller_id = CONTROLLER

        def __init__(self, calls):
            self.calls = calls

        async def execute_hedge(self, **kwargs):
            self.calls.append(dict(kwargs))
            raise TimeoutError("venue acknowledgement timed out")

    with pytest.raises(TimeoutError):
        run(
            _restart_gm(world, TimeoutAfterSubmit(attempts)).reconcile_lifecycle(
                "timeout-cleanup"
            )
        )

    record_path = world.root / "trades" / "timeout-cleanup" / "lifecycle_cleanup.json"
    record = json.loads(record_path.read_text())
    assert record["status"] == "reconciliation_required"
    assert len(attempts) == 1

    restarted = _restart_gm(world, TimeoutAfterSubmit(attempts))
    result = run(restarted.reconcile_lifecycle("timeout-cleanup"))
    assert result["status"] == "main_closed"
    assert len(attempts) == 1


def test_positive_close_ack_does_not_release_a_still_open_main(tmp_path):
    world = E2EWorld(tmp_path)
    binding = open_main_position(world, "close-ack", h1_due_now())

    async def acknowledge_without_filling(*, executor_id):
        world.port.calls.append(("close-ack", {"executor_id": executor_id}))
        return executor_id

    world.port.close_main = acknowledge_without_filling
    acknowledgement = run(
        world.gm.execute_management(
            correlation_id="close-ack",
            decision_id="close-ack-1",
            action="CLOSE",
        )
    )
    assert acknowledgement["status"] == "submitted"
    assert world.venue.positions[0]["position_id"] == binding["main_position_id"]

    after = run(world.gm.reconcile_lifecycle("close-ack"))

    assert after["status"] == "reconciled"
    assert after["main_position_id"] == binding["main_position_id"]
    assert _bindings(world)[0]["correlation_id"] == "close-ack"


@pytest.mark.parametrize(
    "ambiguity", ["extra_exposure", "unbound_hedge", "foreign_hedge_executor"]
)
def test_ambiguous_orphan_hedge_never_receives_close(tmp_path, ambiguity):
    world = E2EWorld(tmp_path)
    binding = _orphan_hedge(world, f"ambiguous-{ambiguity}")

    if ambiguity == "extra_exposure":
        world.venue.positions.append(
            {
                "position_id": "unbound-extra",
                "trading_pair": SYMBOL,
                "position_side": "LONG",
                "net_amount_base": "0.01",
                "current_price": world.venue.mark_price,
            }
        )
    elif ambiguity == "unbound_hedge":
        path = world.root / "trades" / binding["correlation_id"] / "binding.json"
        current = json.loads(path.read_text())
        current["hedge_executor_id"] = None
        path.write_text(json.dumps(current, sort_keys=True))
    else:
        hedge_executor = binding["hedge_executor_id"]
        row = next(
            row
            for row in world.venue.executor_rows
            if row.get("executor_id") == hedge_executor
        )
        row["account_name"] = "foreign-account"

    with pytest.raises(GMRejected):
        run(world.gm.reconcile_lifecycle(binding["correlation_id"]))

    assert not [kind for kind, _ in world.port.calls if kind == "hedge"]
    current = json.loads(
        (world.root / "trades" / binding["correlation_id"] / "binding.json").read_text()
    )
    assert current["status"] == "reconciled"
    assert not (
        world.root / "trades" / binding["correlation_id"] / "lifecycle_cleanup.json"
    ).exists()


def test_disagreeing_hedge_quantities_never_write_or_release(tmp_path):
    world = E2EWorld(tmp_path)
    binding = _orphan_hedge(world, "disagree-quantity")
    original_read = world.venue.trading.get_positions
    reads = 0

    async def alternating_quantity(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            for row in world.venue.positions:
                if row.get("position_id") == binding["hedge_position_id"]:
                    row["net_amount_base"] = "0.21"
        return await original_read(**kwargs)

    world.venue.trading.get_positions = alternating_quantity

    with pytest.raises(GMRejected, match="reads disagree"):
        run(world.gm.reconcile_lifecycle("disagree-quantity"))

    assert not [kind for kind, _ in world.port.calls if kind == "hedge"]
    assert _bindings(world)[0]["status"] == "reconciled"


def test_disagreeing_executor_statuses_never_write_or_release(tmp_path):
    world = E2EWorld(tmp_path)
    binding = _orphan_hedge(world, "disagree-status")
    original_read = world.venue.executors.search_executors
    reads = 0

    async def alternating_executor_status(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            for row in world.venue.executor_rows:
                if row.get("executor_id") == binding["hedge_executor_id"]:
                    row["status"] = "RUNNING"
        return await original_read(**kwargs)

    world.venue.executors.search_executors = alternating_executor_status

    with pytest.raises(GMRejected, match="reads disagree"):
        run(world.gm.reconcile_lifecycle("disagree-status"))

    assert not [kind for kind, _ in world.port.calls if kind == "hedge"]
    assert _bindings(world)[0]["status"] == "reconciled"


def test_idless_executor_pointer_resolves_hedge_after_main_executor_disappears(
    tmp_path,
):
    world = E2EWorld(tmp_path)
    binding = _orphan_hedge(world, "idless-orphan")
    main_executor_id = binding["main_executor_id"]
    hedge_executor_id = binding["hedge_executor_id"]
    binding_path = world.root / "trades" / "idless-orphan" / "binding.json"
    binding["main_position_id"] = f"executor:{main_executor_id}"
    binding["hedge_position_id"] = f"executor:{hedge_executor_id}"
    binding_path.write_text(json.dumps(binding, sort_keys=True))

    # Model an idless venue leg and an executor that disappeared from a complete
    # search. The retained HEDGE executor is explicitly scoped and terminal.
    hedge_row = world.venue.positions[0]
    hedge_row.pop("position_id")
    world.venue.executor_rows[:] = [
        row
        for row in world.venue.executor_rows
        if row.get("executor_id") != main_executor_id
    ]

    class CaptureCleanup:
        controller_id = CONTROLLER

        def __init__(self):
            self.calls = []

        async def execute_hedge(self, **kwargs):
            self.calls.append(kwargs)
            return "cleanup-executor"

    execution = CaptureCleanup()
    result = run(_restart_gm(world, execution).reconcile_lifecycle("idless-orphan"))

    assert result["status"] == "main_closed"
    assert len(execution.calls) == 1
    assert execution.calls[0]["position_action"] == "CLOSE"
    assert execution.calls[0]["quantity"] == Decimal("0.20")
    cleanup = json.loads(
        (world.root / "trades" / "idless-orphan" / "lifecycle_cleanup.json").read_text()
    )
    assert cleanup["hedge_position_id"] == f"executor:{hedge_executor_id}"
    assert cleanup["status"] == "submitted"

    # FakeVenue has no direct get_executor method: this exercises the complete
    # search-absence => MISSING path, not proof of Hummingbot's HTTP-404 shape.
