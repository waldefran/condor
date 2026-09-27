"""Brooks GM and Hummingbot port: deterministic writes without a live venue."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from condor.brooks.execution import ExecutionRejected, HummingbotExecutionPort
from condor.brooks.gm import (
    AccountSnapshot,
    BrooksGM,
    GMPolicy,
    GMRejected,
    VenueRules,
    compile_main,
)


def D(value):
    return Decimal(str(value))


def intent(decision="ENTER_LONG"):
    return {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": decision,
        "symbol": "BTC-USDT",
        "decision_time_ms": int(time.time() * 1000),
        "trigger": {"price": "100"},
        "invalidation": {"price": "95" if decision == "ENTER_LONG" else "105"},
    }


def policy(**changes):
    return replace(GMPolicy(D("0.01"), 2, D("2"), 2, D("2"), 3600), **changes)


def snapshot(**changes):
    return replace(
        AccountSnapshot(
            as_of_ms=int(time.time() * 1000),
            equity=D(1000),
            available_margin=D(500),
            mark_price=D(100),
            gross_exposure=D(0),
            open_positions=0,
            rules=VenueRules(D("0.01"), D("0.01"), D(10), 5),
        ),
        **changes,
    )


class FakeReader:
    def __init__(self, state):
        self.state = state
        self.calls = 0

    async def read(self, **kwargs):
        self.calls += 1
        return self.state


class FakePort:
    def __init__(self):
        self.controller_id = "brooks"
        self.calls = []
        self.raise_on_open = False

    async def open_main(self, **kwargs):
        self.calls.append(("open", kwargs))
        if self.raise_on_open:
            raise TimeoutError("unknown venue outcome")
        return "exec-main"

    async def reduce_main(self, **kwargs):
        self.calls.append(("reduce", kwargs))
        return "exec-reduce"

    async def close_main(self, **kwargs):
        self.calls.append(("close", kwargs))
        return kwargs["executor_id"]


def gm(tmp_path, state=None, port=None):
    reader = FakeReader(state or snapshot())
    port = port or FakePort()
    return (
        BrooksGM(
            account_name="demo",
            connector_name="binance_perpetual",
            state_root=tmp_path,
            policy=policy(),
            reader=reader,
            execution=port,
        ),
        reader,
        port,
    )


def test_sizing_quantizes_down_and_builds_fractional_barriers():
    plan = compile_main(
        intent(),
        snapshot(mark_price=D("100.5")),
        policy(),
        now_ms=int(time.time() * 1000),
    )
    assert plan.quantity == D("1.81")
    assert plan.quantity * (D("100.5") - D("95")) <= D(10)
    assert plan.stop_loss_pct == D("5.5") / D("100.5")
    assert plan.take_profit_pct == plan.stop_loss_pct * 2
    assert plan.margin_required == plan.notional / 2


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"available_margin": D(1)}, "margin"),
        ({"gross_exposure": D(1900)}, "gross exposure"),
        ({"open_positions": 2}, "max positions"),
        ({"structure_status": "unknown_role"}, "ownership"),
        ({"mark_price": D(110)}, "moved"),
        ({"as_of_ms": 1}, "stale"),
        ({"rules": VenueRules(D("10"), D("0.01"), D(10), 5)}, "minimum"),
        ({"rules": VenueRules(D("0.01"), D("0.01"), D(1000), 5)}, "notional"),
        ({"rules": VenueRules(D("0.01"), D("0.01"), D(10), 1)}, "leverage"),
    ],
)
def test_entry_fails_closed_on_account_and_rule_limits(change, reason):
    with pytest.raises(GMRejected, match=reason):
        compile_main(
            intent(), snapshot(**change), policy(), now_ms=int(time.time() * 1000)
        )


def test_bad_stop_and_no_trade_do_not_open(tmp_path):
    with pytest.raises(GMRejected, match="wrong side"):
        compile_main(
            {**intent(), "invalidation": {"price": "101"}}, snapshot(), policy()
        )
    gate, reader, port = gm(tmp_path)
    assert (
        asyncio.run(gate.execute_entry(intent("NO_TRADE"), correlation_id="c1")) is None
    )
    assert reader.calls == 0 and port.calls == []


def test_entry_persists_original_intent_and_binding_before_write(tmp_path):
    gate, reader, port = gm(tmp_path)
    original = intent()
    result = asyncio.run(gate.execute_entry(original, correlation_id="c1"))
    assert result["main_executor_id"] == "exec-main"
    assert result["status"] == "submitted"
    assert port.calls[0][1]["quantity"] == D(2)
    assert (
        json.loads((tmp_path / "trades/c1/original_trade_intent.json").read_text())
        == original
    )
    with pytest.raises(GMRejected, match="already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert len(port.calls) == 1 and reader.calls == 1


def test_ambiguous_entry_keeps_reservation_and_never_retries(tmp_path):
    port = FakePort()
    port.raise_on_open = True
    gate, _, _ = gm(tmp_path, port=port)
    with pytest.raises(TimeoutError):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert (
        json.loads((tmp_path / "trades/c1/binding.json").read_text())["status"]
        == "submitting"
    )
    with pytest.raises(GMRejected, match="already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="c1"))
    assert len(port.calls) == 1


def test_parallel_entries_for_same_symbol_serialize_and_reserve(tmp_path):
    gate, reader, port = gm(tmp_path)

    async def run():
        return await asyncio.gather(
            gate.execute_entry(intent(), correlation_id="a"),
            gate.execute_entry(intent(), correlation_id="b"),
            return_exceptions=True,
        )

    # Reader changes after the first accepted write as a venue would.
    original = port.open_main

    async def open_and_update(**kwargs):
        result = await original(**kwargs)
        reader.state = snapshot(structure_status="single_main", open_positions=1)
        return result

    port.open_main = open_and_update
    result = asyncio.run(run())
    assert len([x for x in result if isinstance(x, dict)]) == 1
    assert len(port.calls) == 1
    assert reader.calls == 1


def test_second_correlation_blocks_while_first_binding_is_unreconciled(tmp_path):
    gate, reader, port = gm(tmp_path)
    asyncio.run(gate.execute_entry(intent(), correlation_id="a"))
    with pytest.raises(GMRejected, match="MAIN already submitted"):
        asyncio.run(gate.execute_entry(intent(), correlation_id="b"))
    assert reader.calls == 1 and len(port.calls) == 1


def test_stale_intent_is_rejected():
    old = {**intent(), "decision_time_ms": 1}
    with pytest.raises(GMRejected, match="stale"):
        compile_main(old, snapshot(), policy())


