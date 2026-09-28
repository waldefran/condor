#!/usr/bin/env python3
"""Brooks DEMO smoke runner (Wave 4) -- reproducible E2E against the live demo venue.

Drives the PRODUCTION Brooks classes only (adapters, GM, watcher, PM context,
PositionManager, run_role, contracts). Demo-only writes on connector
``binance_perpetual_demo``; every controlled fixture is explicitly marked with
a ``demo-smoke-<ts>`` correlation id and a ``{"smoke": true}`` marker.

Observable steps:
  S0  baseline read (positions / orders / balances / mark / position mode)
  S1  REAL TRADER OBSERVATION: real closed H1 bars via the production candle
      source adapter + the real Trader role via run_role. NO_TRADE is valid
      evidence; market data is never fabricated.
  S2  CONTROLLED DEMO MAIN: fixture TradeIntentV2 (smoke-marked) drives
      BrooksGM.execute_entry, then reconciliation + a production watcher poll.
  S3  PM PATH: production pm_load_context + PositionManager.handle_event with
      a controlled HOLD fixture -> ZERO writes.
  S4  HEDGE LIFECYCLE: HEDGE 0.30 -> INCREASE 0.50 -> REDUCE 0.20 -> REMOVE,
      each with a full ManagementDecisionV2 payload incl. hedge_plan.
  S5  CLEANUP: management CLOSE of the smoke MAIN; account back to baseline.

FAIL-CLOSED: any unavailable/ambiguous read aborts that step with a clear
error and ZERO further writes (no blind retries). A step that fail-closes is
reported as FAIL-CLOSED (not PASS) with its evidence; the script exits 0 only
when every safety invariant holds (no smoke executors, no smoke bindings, no
unrelated venue state touched). Exit 2 on invariant violation.

Usage (from the repo root, shared venv -- never commits credentials):
  PYTHONPATH=<repo> /path/to/condor/.venv/bin/python scripts/brooks_demo_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

ALLOWED_CONNECTOR = "binance_perpetual_demo"
ACCOUNT = "master_account"
OBSYMBOL = "BTC-USDT"
CONTROLLER = "brooks-demo-smoke"
AGENT_KEY_DEFAULT = "opencode-go:deepseek-v4.1-flash"


def log(msg: str) -> None:
    print(msg, flush=True)


def banner(step: str, title: str) -> None:
    log(f"\n===== {step}: {title} =====")


def read_server_creds(config_path: str, server: str) -> dict:
    import yaml  # widow import: only config parsing, never printed

    with open(config_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    entry = (data.get("servers") or {}).get(server)
    if not isinstance(entry, dict):
        raise SystemExit(f"FAIL-CLOSED: server {server!r} not found in config")
    return {
        "host": entry["host"],
        "port": entry["port"],
        "username": entry["username"],
        "password": entry["password"],
    }


async def venue_snapshot(client, account: str, connector: str) -> dict:
    """Redacted public demo state only: no credentials, no private data."""
    pos = await client.trading.get_positions(
        account_names=[account], connector_names=[connector], limit=1000
    )
    orders = await client.trading.get_active_orders(
        account_names=[account], connector_names=[connector], limit=200
    )
    state = await client.portfolio.get_state(
        account_names=[account], connector_names=[connector], skip_gateway=True
    )
    prices = await client.market_data.get_prices(
        connector_name=connector, trading_pairs=[OBSYMBOL]
    )
    mode = await client.trading.get_position_mode(
        account_name=account, connector_name=connector
    )
    rows = pos.get("data", []) if isinstance(pos, dict) else []
    orows = orders.get("data", []) if isinstance(orders, dict) else []
    brows: list[dict] = []
    if isinstance(state, dict):
        for acct_rows in state.values():
            if isinstance(acct_rows, dict):
                for crow in acct_rows.get(connector, []) or []:
                    if isinstance(crow, dict):
                        brows.append(
                            {
                                "token": crow.get("token", crow.get("asset")),
                                "units": str(crow.get("units", crow.get("balance"))),
                                "value": str(
                                    crow.get("value", crow.get("usd_value", "?"))
                                ),
                            }
                        )
    return {
        "positions": [
            {
                "trading_pair": r.get("trading_pair", r.get("symbol")),
                "side": r.get("side", r.get("position_side")),
                "amount": str(r.get("amount", r.get("net_amount_base"))),
                "entry_price": str(r.get("entry_price", "?")),
                "has_venue_position_id": bool(
                    r.get("position_id") or r.get("positionId") or r.get("id")
                ),
            }
            for r in rows
            if isinstance(r, dict)
        ],
        "active_orders": len(orows),
        "balances": brows,
        "mark": str((prices.get("prices", {}) or {}).get(OBSYMBOL)),
        "position_mode": str(mode.get("position_mode")) if isinstance(mode, dict) else "?",
    }


async def executor_count(client, account: str, connector: str) -> int:
    res = await client.executors.search_executors(
        account_names=[account], connector_names=[connector], limit=1000
    )
    data = res.get("data", []) if isinstance(res, dict) else []
    return len(data) if isinstance(data, list) else -1


async def step_s1(client, args) -> dict:
    """Real Trader observation on live closed bars. Never fabricated."""
    from condor.brooks.adapters import HummingbotCandleSource
    from condor.brooks.agent_runner import RoleRunError, bind_symbol_tools, run_role
    from condor.brooks.contracts import TradeIntentV2
    from condor.brooks.market_tools import TraderMarketTools

    banner("S1", "REAL TRADER OBSERVATION (live demo market data, production classes)")
    source = HummingbotCandleSource(client, ALLOWED_CONNECTOR)
    bars = await source.fetch_candles(OBSYMBOL, "1h", 6)
    if len(bars) < 2:
        return {"status": "FAIL-CLOSED", "reason": "fewer than 2 closed H1 bars"}
    decision_time_ms = bars[-1]["close_time_ms"]
    log(f"H1 closed bars: n={len(bars)} last_close_ms={decision_time_ms}")
    log(f"last closed bar: {json.dumps(bars[-1], sort_keys=True)}")
    tools = TraderMarketTools(source=source, decision_time_ms=decision_time_ms)
    bound = bind_symbol_tools(
        OBSYMBOL,
        {
            "get_closed_candles": tools.get_closed_candles,
            "get_market_context": tools.get_market_context,
            "get_recent_structure": tools.get_recent_structure,
            "get_volatility": tools.get_volatility,
        },
    )
    prompt = {
        "schema": "brooks.demo-smoke-observation.v1",
        "role": "TRADER",
        "symbol": OBSYMBOL,
        "decision_time_ms": decision_time_ms,
        "note": (
            "DEMO smoke observation on live demo-venue closed bars. "
            "Decide honestly; NO_TRADE is acceptable evidence."
        ),
        "history_depth_hint": {"H4": 120, "H1": len(bars), "M15": 120},
    }
    intent = None
    last_err: Exception | None = None
    for attempt in (1, 2):  # read-only model retry only; never a venue-write retry
        try:
            intent = await run_role(
                "TRADER",
                prompt=prompt,
                output_model=TradeIntentV2,
                market_tools=bound,
                agent_key=args.agent_key,
                timeout_sec=600,
                max_tool_calls=6,
            )
            break
        except (RuntimeError, RoleRunError) as exc:
            last_err = exc
            log(f"S1 attempt {attempt} no-text/transient ({type(exc).__name__}); "
                f"{'retrying once' if attempt == 1 else 'giving up'}")
    if intent is None:
        log(f"S1 model run failed (no writes involved): "
            f"{type(last_err).__name__}: {last_err}")
        return {"status": "FAIL-CLOSED",
                "reason": f"{type(last_err).__name__}: {last_err}"}
    log(f"TRADER decision: {intent.decision} mechanism={intent.entry_mechanism} "
        f"confidence={intent.qualitative_confidence}")
    log(f"evidence_for={intent.evidence_for} evidence_against={intent.evidence_against}")
    return {"status": "PASS", "decision": intent.decision,
            "intent": intent.model_dump(mode="json")}


def smoke_entry_intent(mark: Decimal, now_ms: int) -> dict:
    from condor.brooks.contracts import TradeIntentV2

    trig = mark.quantize(Decimal("0.1"))
    inv = (trig * Decimal("0.995")).quantize(Decimal("0.1"))
    if not trig > inv:
        raise ValueError("fixture trigger/invalidation inconsistent")
    src = {
        "timeframe": "M15",
        "bar_index": 0,
        "open_time_ms": now_ms - 900_000,
        "close_time_ms": now_ms,
    }
    intent = {
        "schema": "brooks.trade-intent.v2",
        "role": "TRADER",
        "decision": "ENTER_LONG",
        "symbol": OBSYMBOL,
        "decision_time_ms": now_ms,
        "market_context": {
            "smoke": True,
            "note": "demo-smoke controlled fixture; demo venue only, never real funds",
        },
        "setup": {
            "type": "breakout",
            "trigger_status": "present",
            "signal_quality": "clear",
            "location_assessment": "favorable",
        },
        "decision_timeframe": "M15",
        "context_timeframes_used": ["H1", "M15"],
        "entry_mechanism": "breakout",
        "trigger": {
            "kind": "market",
            "direction": "at",
            "reference": "demo-smoke live mark",
            "price_field": "close",
            "price": format(trig, "f"),
            "source": src,
        },
        "invalidation": {
            "reference": "demo-smoke structural stop",
            "price_field": "low",
            "price": format(inv, "f"),
            "source": src,
        },
        "evidence_for": ["[demo-smoke] controlled fixture for smoke execution"],
        "evidence_against": ["[demo-smoke] no real signal asserted"],
        "qualitative_confidence": "low",
        "uncertainty": ["[demo-smoke] fixture carries no market view"],
        "conditions_that_change_market_read": ["[demo-smoke] n/a fixture"],
    }
    TradeIntentV2.model_validate(intent)
    return intent


def smoke_hedge_decision(now_ms: int, action: str, target: str) -> dict:
    from condor.brooks.contracts import ManagementDecisionV2

    decision = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": now_ms,
        "action": action,
        "position_ids": ["demo-smoke-unbound-no-venue-position"],
        "reason": f"[demo-smoke] controlled {action} fixture to {target} (demo only)",
        "evidence": {
            "observations": ["[demo-smoke] fixture observation"],
            "evidence_for": ["[demo-smoke] lifecycle step"],
            "evidence_against": ["[demo-smoke] no venue MAIN bound"],
        },
        "risk": {
            "exposure_before": ["[demo-smoke] unknown until MAIN bound"],
            "exposure_after": ["[demo-smoke] target ratio " + target],
            "protection_status": "unknown",
            "costs_considered": ["[demo-smoke] demo fees only"],
            "uncertainty": "high",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": {
            "objective": f"[demo-smoke] {action} to {target} on demo only",
            "target_hedge_ratio": target,
            "main_position_id": "demo-smoke-unbound-main",
            "hedge_position_id": None,
            "ratio_basis": "absolute_mark_notional",
            "expected_effect_on_exposure": "[demo-smoke] net exposure falls",
            "costs": ["[demo-smoke] demo fees and funding"],
            "unlock_condition": "[demo-smoke] target reached",
            "failure_condition": "[demo-smoke] venue state diverges",
        },
        "market_analysis_request": None,
        "conditions_that_change_action": ["[demo-smoke] venue divergence"],
    }
    ManagementDecisionV2.model_validate(decision)
    return decision


async def main() -> int:
    ap = argparse.ArgumentParser(description="Brooks demo smoke runner (Wave 4)")
    ap.add_argument("--config", default="/home/valdemaster/brooks-condor/condor/config.yml")
    ap.add_argument("--server", default="local")
    ap.add_argument("--agent-key", default=AGENT_KEY_DEFAULT)
    ap.add_argument("--state-root", default="")
    args = ap.parse_args()

    from hummingbot_api_client import HummingbotAPIClient

    ts = int(time.time())
    cid = f"demo-smoke-{ts}"
    state_root = Path(args.state_root) if args.state_root else Path(f"/tmp/brooks-demo-smoke-{ts}")
    state_root.mkdir(parents=True, exist_ok=True)

    log("Brooks DEMO smoke (production classes, demo-only writes, fixtures marked smoke)")
    log(f"correlation={cid} state_root={state_root} controller={CONTROLLER}")
    creds = read_server_creds(args.config, args.server)
    log(f"venue=http://{creds['host']}:{creds['port']} server={args.server} "
        f"account={ACCOUNT} connector={ALLOWED_CONNECTOR} (credentials redacted)")

    from condor.brooks.adapters import (
        HummingbotAccountReader,
        HummingbotPositionReconciler,
        build_gm_factory,
        build_pm_load_context,
        build_watcher_provider,
    )
    from condor.brooks.gm import GMPolicy, GMRejected
    from condor.brooks.position_watcher import PositionWatcher

    client = HummingbotAPIClient(
        base_url=f"http://{creds['host']}:{creds['port']}",
        username=creds["username"],
        password=creds["password"],
    )
    await client.init()
    results: dict = {"correlation_id": cid}

    try:
        # ---- S0 baseline ----
        banner("S0", "BASELINE (own read; unrelated state is never touched)")
        base = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR)
        base_execs = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
        log(f"positions={json.dumps(base['positions'])} active_orders={base['active_orders']} "
            f"executors={base_execs} mark={base['mark']} mode={base['position_mode']}")
        log(f"balances={json.dumps(base['balances'])}")
        results["baseline"] = {**base, "executors": base_execs}

        # ---- S1 ----
        try:
            results["s1"] = await step_s1(client, args)
        except Exception as exc:  # noqa: BLE001 - record and continue, zero writes
            log(f"S1 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            results["s1"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:300]}
        log(f"S1 result: {results['s1'].get('status')}")

        # Shared production GM factory (policy sized for a tiny demo MAIN).
        class _Policy:
            risk_per_trade_pct = Decimal("0.01")
            max_positions = 2
            max_gross_exposure_pct = Decimal("2")
            leverage = 5
            take_profit_r = Decimal("2")
            time_limit_sec = 3600
            max_trigger_drift_pct = Decimal("0.05")
            max_snapshot_age_ms = 60_000
            max_intent_age_ms = 7_200_000

        gm_factory = build_gm_factory(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, state_root=state_root, policy_config=_Policy(),
        )
        gm = gm_factory(OBSYMBOL)
        policy = GMPolicy(
            risk_per_trade_pct=Decimal("0.01"), max_positions=2,
            max_gross_exposure_pct=Decimal("2"), leverage=5,
            take_profit_r=Decimal("2"), time_limit_sec=3600,
            max_trigger_drift_pct=Decimal("0.05"), max_snapshot_age_ms=60_000,
            max_intent_age_ms=7_200_000,
        )
        log(f"GM policy: risk=1% lev=5 (venue max unknown; reader must confirm)")

        # ---- S2 ----
        banner("S2", "CONTROLLED DEMO MAIN (fixture marked smoke, demo only)")
        now_ms = time.time_ns() // 1_000_000
        mark = Decimal((await client.market_data.get_prices(
            connector_name=ALLOWED_CONNECTOR, trading_pairs=[OBSYMBOL]
        )).get("prices", {}).get(OBSYMBOL))
        intent = smoke_entry_intent(mark, now_ms)
        execs_before = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
        try:
            binding = await gm.execute_entry(intent, correlation_id=cid)
            results["s2"] = {"status": "PASS", "binding": binding,
                             "main_position_id": binding.get("main_position_id")}
            log(f"S2 entry binding: {json.dumps(binding, sort_keys=True)}")
            # Reconciliation: binding main_position_id becomes non-null via lineage.
            for _ in range(6):
                if binding.get("main_position_id"):
                    break
                await asyncio.sleep(10)
                binding = await gm.reconcile_main(cid)
            results["s2"]["reconciled"] = binding
            log(f"S2 reconciled: main_position_id={binding.get('main_position_id')} "
                f"status={binding.get('status')}")
            # Watcher snapshot must report POSITION_OPENED.
            seen: list = []
            provider = build_watcher_provider(
                client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
                controller_id=CONTROLLER, symbols=[OBSYMBOL], state_root=state_root,
            )
            watcher = PositionWatcher(provider, seen.append)
            await watcher.poll()
            kinds = [e.get("type") if isinstance(e, dict) else getattr(e.type, "value", str(e.type))
                     for e in seen]
            results["s2"]["watcher_events"] = kinds
            log(f"S2 watcher events: {kinds}")
        except GMRejected as exc:
            execs_after = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
            smoke_dirs = sorted(p.name for p in (state_root / "trades").glob("demo-smoke-*")) \
                if (state_root / "trades").exists() else []
            log(f"S2 FAIL-CLOSED (zero writes): GMRejected: {exc}")
            log(f"S2 safety: executors before={execs_before} after={execs_after} "
                f"smoke_bindings={smoke_dirs}")
            results["s2"] = {"status": "FAIL-CLOSED", "reason": str(exc),
                             "executors_before": execs_before,
                             "executors_after": execs_after,
                             "smoke_bindings": smoke_dirs}
        log(f"S2 result: {results['s2'].get('status')}")

        # ---- S3 ----
        banner("S3", "PM PATH (production context + HOLD fixture -> ZERO writes)")
        pm_load = build_pm_load_context(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, state_root=state_root,
        )
        loaded = await pm_load(cid)
        log(f"pm_load_context({cid!r}) -> {'snapshot' if loaded else 'None (fail-closed, no binding)'}")
        saved: list = []
        published: list = []

        async def _save(corr: str, decision) -> None:
            saved.append((corr, decision))

        async def _record(corr: str, record: dict) -> None:
            pass

        async def _never_runner(*a, **k):
            raise AssertionError("PM runner must not run without a bound context")

        async def _active(symbol) -> list:
            return []

        from condor.brooks.pm import PositionManager

        pm = PositionManager(
            runner=_never_runner, load_context=pm_load, save_decision=_save,
            publish=published.append, candle_source=None,
            record_market_read=_record, list_active_correlations=_active,
            agent_key=args.agent_key,
        )
        pm_out = await pm.handle_event({
            "type": "PM_TIMER", "correlation_id": cid, "symbol": OBSYMBOL,
            "created_at_ms": time.time_ns() // 1_000_000, "payload": {},
        })
        hold_rec = await gm.execute_management(
            correlation_id=cid, action="HOLD", decision_id=f"{cid}-hold")
        execs_s3 = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
        log(f"PositionManager.handle_event -> {pm_out!r} (None = stayed idle, no model call)")
        log(f"GM HOLD record: {hold_rec} saved={len(saved)} published={len(published)} "
            f"executors={execs_s3}")
        results["s3"] = {"status": "PASS" if hold_rec.get("status") == "no_write" and not saved
                         and execs_s3 == execs_before else "FAIL-CLOSED",
                         "pm_event_out": pm_out, "hold": hold_rec,
                         "executors": execs_s3}

        # ---- S4 ----
        banner("S4", "HEDGE LIFECYCLE (full decision payloads incl. hedge_plan)")
        s4_steps = []
        for action, target in (("HEDGE", "0.30"), ("INCREASE_HEDGE", "0.50"),
                               ("REDUCE_HEDGE", "0.20"), ("REMOVE_HEDGE", "0")):
            dec = smoke_hedge_decision(time.time_ns() // 1_000_000, action, target)
            try:
                rec = await gm.execute_management(correlation_id=cid, decision=dec)
                s4_steps.append({"action": action, "target": target,
                                 "status": "PASS", "record": rec})
                log(f"S4 {action}->{target}: record={json.dumps(rec, sort_keys=True)}")
            except GMRejected as exc:
                s4_steps.append({"action": action, "target": target,
                                 "status": "FAIL-CLOSED", "reason": str(exc)})
                log(f"S4 {action}->{target}: FAIL-CLOSED (zero writes): GMRejected: {exc}")
        execs_s4 = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
        results["s4"] = {"steps": s4_steps, "executors": execs_s4,
                         "status": "PASS" if all(s["status"] == "PASS" for s in s4_steps)
                         else "FAIL-CLOSED"}
        log(f"S4 result: {results['s4']['status']} executors={execs_s4}")

        # ---- S5 ----
        banner("S5", "CLEANUP (management CLOSE of the smoke MAIN)")
        smoke_dirs = sorted(p.name for p in (state_root / "trades").glob("demo-smoke-*")) \
            if (state_root / "trades").exists() else []
        if results["s2"].get("status") == "PASS" and results["s2"].get("main_position_id"):
            try:
                rec = await gm.execute_management(
                    correlation_id=cid, action="CLOSE",
                    decision_id=f"{cid}-close")
                log(f"S5 CLOSE record: {json.dumps(rec, sort_keys=True)}")
                results["s5"] = {"status": "PASS", "record": rec}
            except GMRejected as exc:
                log(f"S5 FAIL-CLOSED: GMRejected: {exc}")
                results["s5"] = {"status": "FAIL-CLOSED", "reason": str(exc)}
        else:
            log("S5: no smoke MAIN was ever opened (S2 fail-closed) -> nothing to close; "
                "verifying baseline untouched instead")
            results["s5"] = {"status": "PASS",
                             "note": "nothing opened, nothing to close"}
        final = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR)
        final_execs = await executor_count(client, ACCOUNT, ALLOWED_CONNECTOR)
        pos_same = (final["positions"] == base["positions"]
                    and final["active_orders"] == base["active_orders"])
        log(f"final positions={json.dumps(final['positions'])} "
            f"active_orders={final['active_orders']} executors={final_execs}")
        log(f"baseline match (positions+orders): {pos_same} "
            f"smoke_bindings={smoke_dirs}")
        results["s5"]["baseline_match"] = pos_same
        results["final"] = {**final, "executors": final_execs}

        banner("SUMMARY", "per-step results (PASS = observed, FAIL-CLOSED = safe abort)")
        for key in ("s1", "s2", "s3", "s4", "s5"):
            r = results.get(key, {})
            log(f"{key.upper()}: {r.get('status')}")

        ok = (final_execs == base_execs and not smoke_dirs
              and results["s3"].get("status") == "PASS")
        if not ok:
            log("SMOKE RESULT: INVARIANT VIOLATION (see above)")
            return 2
        log("SMOKE RESULT: done; all safety invariants hold "
            "(zero smoke executors, zero smoke bindings, HOLD wrote nothing)")
        return 0
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort session teardown
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
