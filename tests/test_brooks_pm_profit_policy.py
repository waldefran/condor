"""PM discretionary hedge passes the real GM before the 5R emergency guard."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from condor.brooks import gm as gm_module
from condor.brooks.contracts import ManagementDecisionV2
from condor.brooks.events import BrooksEvent, EventType
from scripts.brooks_walkforward import WalkForward, utc_ms
from tests.brooks_e2e_harness import hedge_decision
from tests.test_brooks_first_long_replay import _args


@pytest.mark.asyncio
@pytest.mark.skipif(not Path('/tmp/brooks-walkforward-10d-data/ETH_USDT_1m.jsonl').exists(),
                   reason='historical integration requires the immutable M1 dataset')
@pytest.mark.parametrize('as_of,profitable', [
    ('2026-09-21T20:30:00Z', True),
    ('2026-09-23T20:30:00Z', False),
])
async def test_pm_can_lock_profitable_or_losing_main_before_five_r(tmp_path, monkeypatch, as_of, profitable):
    args = _args(tmp_path)
    args.only_trade = 'ETH-USDT-1h-1790002799999'
    args.start, args.end = '2026-09-21T15:00:00Z', '2026-09-24T15:00:00Z'
    args.max_unhedged_loss_r = '5'
    runner = WalkForward(args)
    monkeypatch.setattr(gm_module, 'time', SimpleNamespace(time=lambda: runner.now / 1000))
    try:
        await runner.advance_to(utc_ms(as_of) - 1)
        state = runner.venue.long_policy_context(args.only_trade)
        from decimal import Decimal
        assert Decimal(state['projected_exit_net']) > -Decimal(state['allowed_loss_usdt'])
        assert (Decimal(state['projected_exit_net']) > 0) is profitable
        assert not (runner.root / 'long_lock_events.jsonl').exists()
        before_fills = len(runner.venue.fills)

        async def model(role, **kwargs):
            assert role == 'POSITION_MANAGER'
            packet = kwargs['prompt']
            policy = packet['management_policy']['applicable_risk_behavior']
            assert policy['discretionary_hedge_timing'] == 'any_management_wake'
            assert policy['hedge_cost_policy'] == 'account_in_net_never_standalone_veto'
            assert policy['unlock_policy'] == 'fresh_closed_m15_recovery_structure'
            main = packet['hedge_state']['main_position_id']
            decision = hedge_decision(packet['decision_time_ms'], main, 'HEDGE', '1', None)
            decision['hedge_plan']['objective'] = 'Protect open profit or contain observed premise deterioration now.'
            decision['hedge_plan']['costs'] = ['Nonzero entry/exit fees are accounted in operation net, not a standalone veto.']
            return ManagementDecisionV2.model_validate(decision)

        runner.pm.runner = model
        event = BrooksEvent(EventType.PM_TIMER, args.symbol, {},
                            correlation_id=args.only_trade, created_at_ms=runner.now)
        result, _ = await runner.scoped('pm-discretionary-lock', lambda: runner.pm.handle_event(event.to_dict()))
        assert result.action == 'HEDGE'
        await runner.drain()
        outcome = json.loads((runner.root / 'management_outcomes.jsonl').read_text().splitlines()[-1])
        assert outcome['gm_result']['type'] == 'GM_MANAGEMENT_APPROVED'
        assert outcome['gm_result']['payload']['result']['assessment'] == 'confirmed'
        state = runner.venue.long_policy_context(args.only_trade)
        assert state['main_quantity'] == state['hedge_quantity'] == '0.159'
        assert len(runner.venue.fills) == before_fills + 1
        assert len(runner.venue.entry_submissions) == 1
        assert len(runner.venue.trades) == 1 and runner.venue.trades[0].closed_at_ms is None
        assert not (runner.root / 'long_lock_events.jsonl').exists()
    finally:
        runner.events.close()
        runner.store.flush()
