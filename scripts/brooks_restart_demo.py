#!/usr/bin/env python3
"""Brooks DEMO restart validation (Wave 5) -- REAL process-boundary restart test.

Validates the "Real restart validation plan" in docs/brooks_review_findings.md
against the live demo venue on symbol SOL-USDT (isolated from the smoke's
ETH/BTC). Drives the PRODUCTION Brooks classes only (adapters, GM, watcher,
PM context, PositionManager, contracts). Demo-only writes on connector
``binance_perpetual_demo``; every controlled fixture is explicitly marked with
a ``demo-restart-<ts>`` correlation id and a ``{"smoke": true}`` marker.

Two explicit phases so the restart is a REAL process boundary:

  Phase A (process 1): baseline read (SOL-USDT must be flat; abort if not),
    open a small fixture-marked smoke MAIN via the production BrooksGM +
    HummingbotExecutionPort on the demo, reconcile the binding, open a HEDGE
    leg (target 0.30), persist binding + hedge state, print a structured
    summary of the durable state, then exit WITHOUT cleanup.

  Phase B (fresh process): instantiate a NEW supervisor/GM/reader stack from
    the SAME durable state root, re-read the venue, reconcile MAIN and HEDGE
    ownership, verify the watcher recognizes the state, wake the PM path
    (production context builder for the correlation), then execute
    REDUCE_HEDGE (0.30 -> 0.15) through the full GM management path and
    confirm with exact venue deltas. Assert NO accidental new trade was
    opened at any point in Phase B. Finally CLEANUP: REMOVE_HEDGE then CLOSE
    the MAIN, confirm SOL-USDT is flat and no smoke executors remain.

SIZING NOTE: the plan's nominal sizes (0.05 SOL MAIN) predate the live venue
rule read: ``min_notional_size`` is 5.0 USDT, and the GM rejects every hedge
OPEN delta and every REDUCE_HEDGE delta below venue minimum notional. At
SOL ~$120 a 0.05 SOL MAIN implies a $1.80 hedge leg, which the production GM
rejects fail-closed. The script therefore sizes the MAIN to ~0.48 SOL
(~$58 notional at risk 0.05% with a 9% stop) so that HEDGE 0.30 (~$17),
REDUCE 0.30->0.15 (~$8.6) and REMOVE all clear the $5.00 floor. Still a tiny
demo fixture; the deviation is recorded in the evidence doc.

FAIL-CLOSED: any unavailable/ambiguous read aborts that phase with a clear
error and ZERO further writes (no blind retries). Exit 0 only when every
safety invariant holds. Exit 2 on invariant violation / fail-closed abort.

Usage (from the repo root, shared venv -- never commits credentials):
  # Phase A (process 1 -- opens positions, exits WITHOUT cleanup):
  PYTHONPATH=<repo> /path/to/condor/.venv/bin/python scripts/brooks_restart_demo.py \\
      --phase A --state-root /tmp/brooks-restart-<ts> --correlation-id demo-restart-<ts>
  # Phase B (fresh process -- reconciles, reduces, cleans up):
  PYTHONPATH=<repo> /path/to/condor/.venv/bin/python scripts/brooks_restart_demo.py \\
      --phase B --state-root /tmp/brooks-restart-<ts> --correlation-id demo-restart-<ts>

RULES: demo only (binance_perpetual_demo, master_account); NEVER touch BTC
(residual rows) or ETH; never print/commit credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

ALLOWED_CONNECTOR = "binance_perpetual_demo"
ACCOUNT = "master_account"
SYMBOL = "SOL-USDT"
UNTOUCHABLE = ("BTC-USDT", "ETH-USDT")
CONTROLLER = "brooks-restart-demo"
AGENT_KEY_DEFAULT = "opencode-go:deepseek-v4.1-flash"


def log(msg: str) -> None:
    print(msg, flush=True)


def banner(step: str, title: str) -> None:
    log(f"\n===== {step}: {title} =====")


def short(x: str | None) -> str:
    s = str(x or "")
    return s[:8] + "…" if len(s) > 8 else s


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


async def venue_snapshot(client, account: str, connector: str, symbols: list[str]) -> dict:
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
        connector_name=connector, trading_pairs=symbols
    )
    mode = await client.trading.get_position_mode(
        account_name=account, connector_name=connector
    )
    rows = pos.get("data", []) if isinstance(pos, dict) else []
    orows = orders.get("data", []) if isinstance(orders, dict) else []
    by_symbol: dict[str, list] = {s: [] for s in symbols}
    for r in rows:
        if not isinstance(r, dict):
            continue
        entry = {
            "trading_pair": r.get("trading_pair", r.get("symbol")),
            "side": r.get("side", r.get("position_side")),
            "amount": str(r.get("amount", r.get("net_amount_base"))),
            "entry_price": str(r.get("entry_price", "?")),
            "has_venue_position_id": bool(
                r.get("position_id") or r.get("positionId") or r.get("id")
            ),
        }
        by_symbol.setdefault(entry["trading_pair"], []).append(entry)
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
    marks = prices.get("prices", {}) or {} if isinstance(prices, dict) else {}
    sol_orders = [
        o for o in orows
        if isinstance(o, dict)
        and (o.get("trading_pair") or o.get("symbol")) in symbols
    ]
    return {
        "positions": by_symbol,
        "active_orders": len(orows),
        "active_orders_for_symbols": len(sol_orders),
        "balances": brows,
        "marks": {s: str(marks.get(s)) for s in symbols},
        "position_mode": str(mode.get("position_mode")) if isinstance(mode, dict) else "?",
    }


def _is_active_executor(row: dict) -> bool:
    if row.get("is_active") is True:
        return True
    if row.get("is_active") is None and str(row.get("status") or "").upper() == "RUNNING":
        return True
    return False


async def active_executors(client, account: str, connector: str) -> list[dict]:
    """Only live executors (RUNNING/is_active): closed ones are history."""
    res = await client.executors.search_executors(
        account_names=[account], connector_names=[connector], limit=1000
    )
    data = res.get("data", []) if isinstance(res, dict) else []
    return [
        {
            "id": r.get("executor_id") or r.get("id"),
            "status": r.get("status"),
            "controller_id": r.get("controller_id"),
            "trading_pair": r.get("trading_pair"),
        }
        for r in data
        if isinstance(r, dict) and _is_active_executor(r)
    ]


def sol_executors(execs: list[dict]) -> list[dict]:
    return [e for e in execs if e.get("trading_pair") == SYMBOL]


def restart_entry_intent(symbol: str, mark: Decimal, now_ms: int) -> dict:
    """Fixture ENTER_LONG sized by the GM policy (risk 0.05%, ~9% stop).

    The intent carries no quantity: the production GM risk-sizes from equity.
    Trigger = live mark, stop = 9% below mark -> ~0.48 SOL at SOL ~$120,
    clearing the $5.00 venue min_notional on every later hedge delta.
    """
    from condor.brooks.contracts import TradeIntentV2

    trig = mark.quantize(Decimal("0.01"))
    stop = (mark * Decimal("0.91")).quantize(Decimal("0.01"))
    if not trig > stop:
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
        "symbol": symbol,
        "decision_time_ms": now_ms,
        "market_context": {
            "smoke": True,
            "restart_test": True,
            "demo": True,
            "note": "demo-restart controlled fixture; demo venue only, never real funds",
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
            "reference": "demo-restart live mark",
            "price_field": "close",
            "price": format(trig, "f"),
            "source": src,
        },
        "invalidation": {
            "reference": "demo-restart structural stop (~9%)",
            "price_field": "low",
            "price": format(stop, "f"),
            "source": src,
        },
        "evidence_for": ["[demo-restart] controlled fixture for restart validation"],
        "evidence_against": ["[demo-restart] no real signal asserted"],
        "qualitative_confidence": "low",
        "uncertainty": ["[demo-restart] fixture carries no market view"],
        "conditions_that_change_market_read": ["[demo-restart] n/a fixture"],
    }
    TradeIntentV2.model_validate(intent)
    return intent


def restart_hedge_decision(
    now_ms: int, action: str, target: str, main_id: str, hedge_id: str | None
) -> dict:
    from condor.brooks.contracts import ManagementDecisionV2

    decision = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": now_ms,
        "action": action,
        "position_ids": [main_id],
        "reason": f"[demo-restart] controlled {action} fixture to {target} (demo only)",
        "evidence": {
            "observations": ["[demo-restart] fixture observation"],
            "evidence_for": ["[demo-restart] restart lifecycle step"],
            "evidence_against": ["[demo-restart] venue move could change the read"],
        },
        "risk": {
            "exposure_before": ["[demo-restart] restart MAIN plus recorded hedge"],
            "exposure_after": ["[demo-restart] target ratio " + target],
            "protection_status": "unknown",
            "costs_considered": ["[demo-restart] demo fees only"],
            "uncertainty": "high",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": {
            "objective": f"[demo-restart] {action} to {target} on demo only",
            "target_hedge_ratio": target,
            "main_position_id": main_id,
            "hedge_position_id": hedge_id,
            "ratio_basis": "absolute_mark_notional",
            "expected_effect_on_exposure": "[demo-restart] net exposure falls",
            "costs": ["[demo-restart] demo fees and funding"],
            "unlock_condition": "[demo-restart] target reached",
            "failure_condition": "[demo-restart] venue state diverges",
        },
        "market_analysis_request": None,
        "conditions_that_change_action": ["[demo-restart] venue divergence"],
    }
    ManagementDecisionV2.model_validate(decision)
    return decision


def make_policy():
    from decimal import Decimal as D

    class _Policy:
        # ~0.05% risk with a ~9% stop targets ~$55-60 notional on SOL so
        # that every hedge delta clears the $5.00 venue min_notional.
        risk_per_trade_pct = D("0.0005")
        max_positions = 2
        max_gross_exposure_pct = D("2")
        leverage = 5
        take_profit_r = D("2")
        time_limit_sec = 86400  # venue triple-barrier; must outlive the A->B gap
        max_trigger_drift_pct = D("0.05")
        max_snapshot_age_ms = 60_000
        max_intent_age_ms = 7_200_000

    return _Policy()


def read_binding(state_root: Path, cid: str) -> dict | None:
    path = state_root / "trades" / cid / "binding.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def phase_a(client, args, state_root: Path, cid: str) -> int:
    from condor.brooks.adapters import (
        HummingbotAccountReader,
        build_gm_factory,
        build_watcher_provider,
    )
    from condor.brooks.gm import GMRejected
    from condor.brooks.position_watcher import PositionWatcher

    class RestartAbort(Exception):
        pass

    results: dict = {"correlation_id": cid, "phase": "A"}
    main_opened = False
    try:
        # ---- A0 baseline: SOL must be flat ----
        banner("A0", "BASELINE (SOL must be flat; BTC/ETH never touched)")
        symbols = [SYMBOL, *UNTOUCHABLE]
        base = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
        base_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
        log(f"SOL positions={json.dumps(base['positions'][SYMBOL])}")
        log(f"BTC positions={json.dumps(base['positions']['BTC-USDT'])}")
        log(f"ETH positions={json.dumps(base['positions']['ETH-USDT'])}")
        log(f"active_orders={base['active_orders']} "
            f"(for SOL/BTC/ETH: {base['active_orders_for_symbols']}) "
            f"active_execs={json.dumps(base_active)} "
            f"marks={base['marks']} mode={base['position_mode']}")
        log(f"balances={json.dumps(base['balances'])}")
        if base["positions"][SYMBOL] or sol_executors(base_active):
            log("A0 FAIL-CLOSED: SOL-USDT is not flat; aborting before any write")
            return 2
        if base["position_mode"] != "HEDGE":
            log("A0 FAIL-CLOSED: position mode is not HEDGE; aborting")
            return 2
        results["baseline"] = {**base, "active_executors": base_active}

        gm_factory = build_gm_factory(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, state_root=state_root, policy_config=make_policy(),
        )
        gm = gm_factory(SYMBOL)
        reader = HummingbotAccountReader(client, state_root, CONTROLLER)
        log("GM policy: risk=0.05% lev=5 restart symbol=SOL-USDT (BTC/ETH untouched)")

        # ---- A1 controlled MAIN ----
        banner("A1", "CONTROLLED DEMO MAIN (fixture marked smoke/restart_test, demo only)")
        try:
            now_ms = time.time_ns() // 1_000_000
            sol_mark = Decimal((await client.market_data.get_prices(
                connector_name=ALLOWED_CONNECTOR, trading_pairs=[SYMBOL]
            )).get("prices", {}).get(SYMBOL))
            intent = restart_entry_intent(SYMBOL, sol_mark, now_ms)
            binding = await gm.execute_entry(intent, correlation_id=cid)
            log(f"A1 entry: executor={short(binding.get('main_executor_id'))} "
                f"planned_qty={binding.get('planned_quantity')} "
                f"side={binding.get('main_side')} status={binding.get('status')}")
            for _ in range(12):
                if binding.get("main_position_id"):
                    break
                await asyncio.sleep(10)
                binding = await gm.reconcile_main(cid)
            if not binding.get("main_position_id") or binding.get("status") != "reconciled":
                raise RestartAbort(f"reconciliation failed: {binding}")
            main_opened = True
            results["a1"] = {"status": "PASS", "binding": binding}
            log(f"A1 reconciled: main_position_id={short(binding.get('main_position_id'))} "
                f"status={binding.get('status')}")
            seen: list = []
            provider = build_watcher_provider(
                client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
                controller_id=CONTROLLER, symbols=[SYMBOL], state_root=state_root,
            )
            watcher = PositionWatcher(provider, seen.append)
            await watcher.poll()
            kinds = [e.get("type") if isinstance(e, dict) else getattr(e.type, "value", str(e.type))
                     for e in seen]
            results["a1"]["watcher_events"] = kinds
            log(f"A1 watcher events: {kinds}")
            if "POSITION_OPENED" not in kinds:
                raise RestartAbort("watcher did not report POSITION_OPENED")
        except (GMRejected, RestartAbort) as exc:
            log(f"A1 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            return 2
        log("A1 result: PASS")

        # ---- A2 first HEDGE 0.30 ----
        banner("A2", "FIRST HEDGE 0.30 (full decision payload incl. hedge_plan)")
        try:
            from condor.brooks.hedge import build_hedge_state

            live = read_binding(state_root, cid) or {}
            snap = await reader.read(
                account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR, symbol=SYMBOL)
            legs = list(snap.positions or [])
            before = build_hedge_state(
                legs, main_position_id=snap.main_position_id,
                hedge_position_id=snap.hedge_position_id, as_of_ms=snap.as_of_ms)
            log(f"A2 pre: reader_hedge={before.hedge_size} main={before.main_size}")
            dec = restart_hedge_decision(
                time.time_ns() // 1_000_000, "HEDGE", "0.30",
                live["main_position_id"], live.get("hedge_position_id"))
            rec = await gm.execute_management(
                correlation_id=cid, decision_id=f"{cid}-a2-HEDGE", decision=dec)
            snap2 = await reader.read(
                account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR, symbol=SYMBOL)
            legs2 = list(snap2.positions or [])
            after = build_hedge_state(
                legs2, main_position_id=snap2.main_position_id,
                hedge_position_id=snap2.hedge_position_id, as_of_ms=snap2.as_of_ms)
            venue = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                     )["positions"][SYMBOL]
            log(f"A2 HEDGE->0.30: qty={rec.get('quantity')} "
                f"filled={rec.get('filled_quantity')} side={rec.get('side')} "
                f"assessment={rec.get('assessment')} hedge {before.hedge_size}->{after.hedge_size} "
                f"ratio={after.hedge_ratio} venue={json.dumps(venue)}")
            if rec.get("assessment") != "confirmed" or Decimal(str(after.hedge_size)) <= 0:
                raise RestartAbort(f"HEDGE delta/ratio mismatch: {rec}")
            ratio = Decimal(str(after.hedge_ratio))
            if abs(ratio - Decimal("0.30")) > Decimal("0.03"):
                raise RestartAbort(f"HEDGE ratio off target: {after.hedge_ratio}")
            results["a2"] = {"status": "PASS", "record": rec,
                             "hedge_size": str(after.hedge_size),
                             "hedge_ratio": str(after.hedge_ratio)}
        except (GMRejected, RestartAbort) as exc:
            log(f"A2 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            return 2
        log("A2 result: PASS")

        # ---- A3 persist + structured durable summary, exit WITHOUT cleanup ----
        banner("A3", "DURABLE STATE SUMMARY (process exits WITHOUT cleanup)")
        trade_dir = state_root / "trades" / cid
        binding = read_binding(state_root, cid) or {}
        hedge_path = trade_dir / "hedge_state.json"
        hedge_doc = json.loads(hedge_path.read_text(encoding="utf-8")) if hedge_path.exists() else None
        mgmt_dir = trade_dir / "management"
        mgmt_files = sorted(p.name for p in mgmt_dir.glob("*.json")) if mgmt_dir.exists() else []
        summary = {
            "correlation_id": cid,
            "symbol": SYMBOL,
            "connector": ALLOWED_CONNECTOR,
            "account": ACCOUNT,
            "controller": CONTROLLER,
            "binding_path": str(trade_dir / "binding.json"),
            "binding_sha256": sha256_file(trade_dir / "binding.json"),
            "intent_sha256": sha256_file(trade_dir / "original_trade_intent.json"),
            "hedge_state_sha256": sha256_file(hedge_path) if hedge_path.exists() else None,
            "main_executor_id": binding.get("main_executor_id"),
            "main_position_id": binding.get("main_position_id"),
            "main_side": binding.get("main_side"),
            "planned_quantity": binding.get("planned_quantity"),
            "binding_status": binding.get("status"),
            "hedge_executor_id": binding.get("hedge_executor_id"),
            "hedge_position_id": binding.get("hedge_position_id"),
            "hedge_state": hedge_doc,
            "management_records": mgmt_files,
            "as_of_ms": time.time_ns() // 1_000_000,
        }
        (state_root / "phase_a_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        log("PHASE_A_DURABLE_STATE=" + json.dumps(summary, sort_keys=True))
        log(f"trade_dir files: {sorted(p.name for p in trade_dir.iterdir())}")
        results["a3"] = {"status": "PASS"}
        log("PHASE A RESULT: done; MAIN+HEDGE open on demo, durable state persisted, "
            "exiting WITHOUT cleanup (fresh process must run Phase B)")
        return 0
    finally:
        pass


async def phase_b(client, args, state_root: Path, cid: str) -> int:
    from condor.brooks.adapters import (
        HummingbotAccountReader,
        HummingbotCandleSource,
        build_gm_factory,
        build_pm_load_context,
        build_watcher_provider,
        read_bindings,
    )
    from condor.brooks.gm import GMRejected
    from condor.brooks.hedge import build_hedge_state
    from condor.brooks.pm import PositionManager
    from condor.brooks.position_watcher import PositionWatcher, position_fingerprint

    class RestartAbort(Exception):
        pass

    own_executor_ids: list[str] = []  # executors created by THIS phase (expected writes)
    try:
        # ---- B0 load durable state ----
        banner("B0", "LOAD DURABLE STATE (fresh process, same state root)")
        trade_dir = state_root / "trades" / cid
        if not (trade_dir / "binding.json").exists():
            log("B0 FAIL-CLOSED: no durable binding for correlation; aborting")
            return 2
        durable = read_binding(state_root, cid) or {}
        hedge_doc = json.loads((trade_dir / "hedge_state.json").read_text(encoding="utf-8"))
        corr_dirs = sorted(p.name for p in (state_root / "trades").iterdir() if p.is_dir())
        log(f"correlation_dirs={corr_dirs}")
        log(f"durable binding: main_exec={short(durable.get('main_executor_id'))} "
            f"main_pos={short(durable.get('main_position_id'))} "
            f"hedge_exec={short(durable.get('hedge_executor_id'))} "
            f"hedge_pos={short(durable.get('hedge_position_id'))} "
            f"status={durable.get('status')}")
        log(f"durable hedge_state: main={hedge_doc.get('main_size')} "
            f"hedge={hedge_doc.get('hedge_size')} ratio={hedge_doc.get('hedge_ratio')}")
        if len(corr_dirs) != 1 or corr_dirs[0] != cid:
            log("B0 FAIL-CLOSED: unexpected correlation dirs; aborting")
            return 2
        if durable.get("status") not in ("reconciled", "submitted"):
            log("B0 FAIL-CLOSED: durable binding status unexpected; aborting")
            return 2
        pre_main_exec = durable.get("main_executor_id")

        # ---- B1 pre-reconciliation venue census (no-accidental-trade baseline) ----
        banner("B1", "PRE-RECONCILIATION CENSUS (no-accidental-trade baseline)")
        symbols = [SYMBOL, *UNTOUCHABLE]
        pre_snap = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
        pre_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
        pre_sol_execs = sol_executors(pre_active)
        log(f"SOL positions={json.dumps(pre_snap['positions'][SYMBOL])}")
        log(f"SOL active_execs={json.dumps(pre_sol_execs)}")
        log(f"BTC rows={json.dumps(pre_snap['positions']['BTC-USDT'])}")
        if not pre_snap["positions"][SYMBOL]:
            log("B1 FAIL-CLOSED: SOL flat at Phase B start (nothing to reconcile); aborting")
            return 2
        pre_sol_exec_ids = sorted(e["id"] for e in pre_sol_execs)
        pre_corr_count = len(corr_dirs)

        gm_factory = build_gm_factory(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, state_root=state_root, policy_config=make_policy(),
        )
        gm = gm_factory(SYMBOL)
        reader = HummingbotAccountReader(client, state_root, CONTROLLER)

        # ---- B2 venue re-query + ownership reconciliation ----
        banner("B2", "VENUE RE-QUERY & OWNERSHIP RECONCILIATION (fresh stack)")
        snap = await reader.read(
            account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR, symbol=SYMBOL)
        legs = list(snap.positions or [])
        hs = build_hedge_state(
            legs, main_position_id=snap.main_position_id,
            hedge_position_id=snap.hedge_position_id, as_of_ms=snap.as_of_ms)
        log(f"reader: main_id={short(snap.main_position_id)} "
            f"hedge_id={short(snap.hedge_position_id)} structure={snap.structure_status}")
        log(f"hedge_state: main={hs.main_size} hedge={hs.hedge_size} ratio={hs.hedge_ratio}")
        if snap.main_position_id != durable.get("main_position_id"):
            log("B2 FAIL-CLOSED: MAIN ownership mismatch vs durable binding; aborting")
            return 2
        if snap.hedge_position_id != durable.get("hedge_position_id"):
            log("B2 FAIL-CLOSED: HEDGE ownership mismatch vs durable binding; aborting")
            return 2
        if snap.structure_status not in ("ok", "single_main"):
            log(f"B2 FAIL-CLOSED: structure_status={snap.structure_status}; aborting")
            return 2
        if hs.hedge_size is None or Decimal(str(hs.hedge_size)) <= 0:
            log("B2 FAIL-CLOSED: HEDGE leg unresolved after restart; aborting")
            return 2
        log("B2 result: PASS (MAIN+HEDGE ownership reconciled from durable state)")

        # ---- B3 watcher recognition (seeded resume: no spurious transitions) ----
        banner("B3", "WATCHER STATE VERIFICATION (seeded resume)")
        provider = build_watcher_provider(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, symbols=[SYMBOL], state_root=state_root,
        )
        seed = await provider()
        log(f"seed snapshots={len(seed)} fingerprints="
            f"{[position_fingerprint(s)[:12] + '…' for s in seed]}")
        seen: list = []
        watcher = PositionWatcher(provider, seen.append, initial_snapshots=seed)
        await watcher.poll()
        kinds = [e.get("type") if isinstance(e, dict) else getattr(e.type, "value", str(e.type))
                 for e in seen]
        log(f"B3 watcher events on resume poll: {kinds}")
        bad = [k for k in kinds if k in ("POSITION_OPENED", "POSITION_CLOSED",
                                         "HEDGE_OPENED", "HEDGE_REMOVED")]
        if bad:
            log(f"B3 FAIL-CLOSED: spurious resume transitions {bad}; aborting")
            return 2
        log("B3 result: PASS (watcher recognizes resumed state, no spurious transitions)")

        # ---- B4 PM wake + REDUCE_HEDGE 0.30 -> 0.15 via PM -> GM ----
        banner("B4", "PM WAKE & REDUCE_HEDGE (production PM -> production GM)")
        pm_load = build_pm_load_context(
            client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER, state_root=state_root,
        )
        loaded = await pm_load(cid)
        if not loaded:
            log("B4 FAIL-CLOSED: pm_load_context returned None; aborting")
            return 2
        log(f"pm_load_context -> symbol={loaded['symbol']} "
            f"positions={len(loaded['positions'])} margin={loaded.get('margin_health')}")
        if loaded["symbol"] != SYMBOL:
            log("B4 FAIL-CLOSED: PM context symbol mismatch; aborting")
            return 2

        owned_main = durable["main_position_id"]
        saved: list = []
        published: list = []

        async def _save(corr: str, decision) -> None:
            saved.append((corr, decision))

        async def _record(corr: str, record: dict) -> None:
            pass

        def _reduce_payload(decision_time_ms: int):
            from condor.brooks.contracts import ManagementDecisionV2
            raw = restart_hedge_decision(
                decision_time_ms, "REDUCE_HEDGE", "0.15",
                owned_main, durable.get("hedge_position_id"))
            return ManagementDecisionV2.model_validate(raw).model_dump(mode="json")

        async def reduce_runner(role, prompt, output_model, market_tools, *,
                                agent_key, timeout_sec, max_tool_calls, user_id):
            assert role == "POSITION_MANAGER", role
            # The production PM requires decision_time_ms == the read
            # snapshot's decision_time_ms; echo the prompt time (same as the
            # S3 HOLD pattern in the smoke runner).
            return _reduce_payload(int(prompt["decision_time_ms"]))

        async def _active(symbol) -> list:
            return [b.get("correlation_id") for b in read_bindings(
                state_root, account_name=ACCOUNT,
                connector_name=ALLOWED_CONNECTOR, controller_id=CONTROLLER)]

        pm = PositionManager(
            runner=reduce_runner, load_context=pm_load, save_decision=_save,
            publish=published.append,
            candle_source=HummingbotCandleSource(client, ALLOWED_CONNECTOR),
            record_market_read=_record, list_active_correlations=_active,
            agent_key=args.agent_key,
        )
        pm_out = await pm.handle_event({
            "type": "PM_TIMER", "correlation_id": cid, "symbol": SYMBOL,
            "created_at_ms": time.time_ns() // 1_000_000, "payload": {},
        })
        log(f"PM decision: action={pm_out.action} saved={len(saved)} published={len(published)}")
        if pm_out.action != "REDUCE_HEDGE" or len(saved) != 1:
            log("B4 FAIL-CLOSED: PM did not emit REDUCE_HEDGE; aborting with zero writes")
            return 2

        # Exact venue short size before the write (for delta proof).
        pre_rows = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                    )["positions"][SYMBOL]
        pre_short = sum(abs(Decimal(str(r.get("amount", 0))))
                        for r in pre_rows if r.get("side") == "SHORT")
        pm_decision = saved[0][1]
        if not isinstance(pm_decision, dict):
            pm_decision = pm_decision.model_dump(mode="json")
        rec = await gm.execute_management(
            correlation_id=cid, decision_id=f"{cid}-b4-REDUCE", decision=pm_decision)
        own_executor_ids.append(rec.get("hedge_executor_id") or rec.get("executor_id") or "")
        # Corroborate with settled reads (venue races writes for ~a minute).
        post_short = None
        filled = Decimal(str(rec.get("filled_quantity", "0")))
        for _ in range(18):
            rows = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                    )["positions"][SYMBOL]
            shorts = [r for r in rows if r.get("side") == "SHORT"]
            if len(shorts) == 1 and abs(Decimal(str(shorts[0]["amount"]))) == pre_short - filled:
                post_short = abs(Decimal(str(shorts[0]["amount"])))
                break
            await asyncio.sleep(10)
        rows = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                )["positions"][SYMBOL]
        snap3 = await reader.read(
            account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR, symbol=SYMBOL)
        hs3 = build_hedge_state(
            list(snap3.positions or []), main_position_id=snap3.main_position_id,
            hedge_position_id=snap3.hedge_position_id, as_of_ms=snap3.as_of_ms)
        venue_delta = (pre_short - post_short) if post_short is not None else None
        log(f"B4 REDUCE->0.15: qty={rec.get('quantity')} filled={rec.get('filled_quantity')} "
            f"side={rec.get('side')} assessment={rec.get('assessment')} "
            f"short {pre_short}->{post_short} venue_delta={venue_delta} "
            f"reader_hedge={hs3.hedge_size} ratio={hs3.hedge_ratio} venue={json.dumps(rows)}")
        if rec.get("assessment") != "confirmed":
            log(f"B4 FAIL-CLOSED: REDUCE not confirmed: {rec}; aborting to cleanup")
            return 2
        if venue_delta is None or venue_delta != filled:
            log("B4 FAIL-CLOSED: venue delta != filled_quantity; aborting to cleanup")
            return 2
        if abs(Decimal(str(hs3.hedge_ratio)) - Decimal("0.15")) > Decimal("0.03"):
            log(f"B4 FAIL-CLOSED: ratio off target: {hs3.hedge_ratio}")
            return 2
        log("B4 result: PASS (REDUCE_HEDGE via PM->GM, exact venue delta)")

        # ---- B5 accidental-trade audit ----
        banner("B5", "ACCIDENTAL TRADE AUDIT (zero new MAINs / bindings)")
        post_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
        post_sol_ids = sorted(e["id"] for e in sol_executors(post_active))
        corr_dirs2 = sorted(p.name for p in (state_root / "trades").iterdir() if p.is_dir())
        live = read_binding(state_root, cid) or {}
        new_ids = [i for i in post_sol_ids if i not in pre_sol_exec_ids]
        log(f"correlation_dirs before/after: {pre_corr_count}->{len(corr_dirs2)} {corr_dirs2}")
        log(f"main_executor before/after: {short(pre_main_exec)}->{short(live.get('main_executor_id'))}")
        log(f"SOL exec ids before={json.dumps(pre_sol_exec_ids)}")
        log(f"SOL exec ids after ={json.dumps(post_sol_ids)} new={json.dumps(new_ids)}")
        unexpected = [i for i in new_ids if i not in own_executor_ids]
        if len(corr_dirs2) != pre_corr_count or live.get("main_executor_id") != pre_main_exec:
            log("B5 FAIL-CLOSED: new binding or MAIN executor detected; aborting to cleanup")
            return 2
        if unexpected:
            log(f"B5 FAIL-CLOSED: unexpected new SOL executors {unexpected}; aborting to cleanup")
            return 2
        log("B5 result: PASS (only expected hedge-writer executor appeared; no new MAIN)")

        # ---- B6 guaranteed cleanup: REMOVE_HEDGE then CLOSE ----
        banner("B6", "CLEANUP (REMOVE_HEDGE then CLOSE the MAIN)")
        try:
            live = read_binding(state_root, cid) or {}
            dec_rm = restart_hedge_decision(
                time.time_ns() // 1_000_000, "REMOVE_HEDGE", "0",
                live["main_position_id"], live.get("hedge_position_id"))
            rec_rm = await gm.execute_management(
                correlation_id=cid, decision_id=f"{cid}-b6-REMOVE", decision=dec_rm)
            log(f"B6 REMOVE_HEDGE: {json.dumps(rec_rm, sort_keys=True)}")
            if rec_rm.get("assessment") != "confirmed":
                raise RestartAbort(f"REMOVE_HEDGE not confirmed: {rec_rm}")
            for _ in range(18):
                rows = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                        )["positions"][SYMBOL]
                if not [r for r in rows if r.get("side") == "SHORT"]:
                    break
                await asyncio.sleep(10)
            rows = (await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, [SYMBOL])
                    )["positions"][SYMBOL]
            if [r for r in rows if r.get("side") == "SHORT"]:
                raise RestartAbort(f"SHORT leg remains: {rows}")
            rec_close = await gm.execute_management(
                correlation_id=cid, action="CLOSE", decision_id=f"{cid}-b6-CLOSE")
            log(f"B6 CLOSE record: {json.dumps(rec_close, sort_keys=True)}")
            for _ in range(18):
                snap_f = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
                mine = [e for e in await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
                        if e.get("trading_pair") == SYMBOL]
                if not snap_f["positions"][SYMBOL] and not mine:
                    break
                await asyncio.sleep(10)
            final = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
            final_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
            sol_flat = (final["positions"][SYMBOL] == []
                        and not [e for e in final_active if e.get("trading_pair") == SYMBOL])
            btc_same = (final["positions"]["BTC-USDT"] == pre_snap["positions"]["BTC-USDT"])
            eth_same = (final["positions"]["ETH-USDT"] == pre_snap["positions"]["ETH-USDT"])
            log(f"final SOL={json.dumps(final['positions'][SYMBOL])} "
                f"SOL_execs={json.dumps([e for e in final_active if e.get('trading_pair') == SYMBOL])} "
                f"BTC_same={btc_same} ETH_same={eth_same}")
            if not (sol_flat and btc_same and eth_same):
                log("B6 FAIL-CLOSED: venue not returned to baseline")
                return 2
        except (GMRejected, RestartAbort) as exc:
            log(f"B6 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            return 2
        log("B6 result: PASS (SOL flat, no SOL executors, BTC/ETH untouched)")
        log("PHASE B RESULT: done; restart reconciled, REDUCE via PM->GM with exact "
            "venue delta, no accidental trades, venue returned to baseline")
        return 0
    except Exception as exc:  # noqa: BLE001 - surface, then fail closed
        log(f"PHASE B UNEXPECTED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2


async def async_main(args) -> int:
    from hummingbot_api_client import HummingbotAPIClient

    state_root = Path(args.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    cid = args.correlation_id
    log("Brooks DEMO restart validation (production classes, demo-only writes, "
        "fixtures marked smoke/restart_test)")
    log(f"phase={args.phase} correlation={cid} state_root={state_root} "
        f"controller={CONTROLLER}")
    creds = read_server_creds(args.config, args.server)
    log(f"venue=http://{creds['host']}:{creds['port']} server={args.server} "
        f"account={ACCOUNT} connector={ALLOWED_CONNECTOR} symbol={SYMBOL} "
        "(credentials redacted)")
    client = HummingbotAPIClient(
        base_url=f"http://{creds['host']}:{creds['port']}",
        username=creds["username"],
        password=creds["password"],
    )
    await client.init()
    try:
        if args.phase == "A":
            return await phase_a(client, args, state_root, cid)
        return await phase_b(client, args, state_root, cid)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort session teardown
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Brooks demo restart validation (Wave 5)")
    ap.add_argument("--phase", choices=("A", "B"), required=True)
    ap.add_argument("--state-root", required=True)
    ap.add_argument("--correlation-id", required=True)
    ap.add_argument("--config", default="/home/valdemaster/brooks-condor/condor/config.yml")
    ap.add_argument("--server", default="local")
    ap.add_argument("--agent-key", default=AGENT_KEY_DEFAULT)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    logging.getLogger("condor.brooks.gm").setLevel(logging.INFO)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
