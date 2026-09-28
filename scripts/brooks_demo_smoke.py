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
import logging
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

ALLOWED_CONNECTOR = "binance_perpetual_demo"
ACCOUNT = "master_account"
OBSYMBOL = "BTC-USDT"
SMOKE_SYMBOL = "ETH-USDT"
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
    return {
        "positions": by_symbol,
        "active_orders": len(orows),
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


async def step_s1(client, args) -> dict:
    """Real Trader observation on live closed bars. Never fabricated."""
    from condor.brooks.adapters import HummingbotCandleSource
    from condor.brooks.agent_runner import RoleRunError, bind_symbol_tools, run_role
    from condor.brooks.contracts import TradeIntentV2
    from condor.brooks.market_tools import TraderMarketTools

    banner("S1", "REAL TRADER OBSERVATION (live demo market data, production classes)")
    source = HummingbotCandleSource(client, ALLOWED_CONNECTOR)
    spot = await source.fetch_candles(OBSYMBOL, "1h", 6)
    if len(spot) < 2:
        return {"status": "FAIL-CLOSED", "reason": "fewer than 2 closed H1 bars"}
    decision_time_ms = spot[-1]["close_time_ms"]
    log(f"H1 tail closed bars: n={len(spot)} last_close_ms={decision_time_ms}")
    frozen = HummingbotCandleSource(
        client, ALLOWED_CONNECTOR, now_fn=lambda: decision_time_ms
    )
    tools = TraderMarketTools(source=frozen, decision_time_ms=decision_time_ms)
    windows = {}
    for label, interval in (("H4", "4h"), ("H1", "1h"), ("M15", "15m")):
        bars = await tools.get_closed_candles(OBSYMBOL, interval, 30)
        windows[label] = {"interval": interval, "bars": bars}
        log(f"{label}: {len(bars)} seeded closed bars ending {bars[-1]['close_time_ms']}")
    if windows["H1"]["bars"][-1]["close_time_ms"] != decision_time_ms:
        return {"status": "FAIL-CLOSED",
                "reason": "H1 window does not end at the decision bar"}
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
            "Bounded 30-bar seeds per timeframe are embedded below; the full "
            "120-bar depth is available through your read tools. Decide "
            "honestly; NO_TRADE is acceptable evidence."
        ),
        "timeframes": windows,
    }
    intent = None
    last_err: Exception | None = None
    for attempt in (1, 2, 3):  # read-only model retry only; never a venue-write retry
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
                f"{'retrying' if attempt < 3 else 'giving up'}")
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


def smoke_entry_intent(symbol: str, mark: Decimal, now_ms: int) -> dict:
    from condor.brooks.contracts import TradeIntentV2

    trig = mark.quantize(Decimal("0.1")) if symbol == OBSYMBOL else mark.quantize(Decimal("0.01"))
    inv = (trig * Decimal("0.98")).quantize(Decimal("0.1")) if symbol == OBSYMBOL else (trig * Decimal("0.98")).quantize(Decimal("0.01"))
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
        "symbol": symbol,
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


def smoke_hold_decision(now_ms: int, main_id: str) -> dict:
    from condor.brooks.contracts import ManagementDecisionV2

    decision = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": now_ms,
        "action": "HOLD",
        "position_ids": [main_id],
        "reason": "[demo-smoke] controlled HOLD fixture (demo only, zero writes)",
        "evidence": {
            "observations": ["[demo-smoke] venue MAIN matches the smoke binding"],
            "evidence_for": ["[demo-smoke] no intervention condition"],
            "evidence_against": ["[demo-smoke] fresh venue move could change the read"],
        },
        "risk": {
            "exposure_before": ["[demo-smoke] smoke MAIN only"],
            "exposure_after": ["[demo-smoke] unchanged"],
            "protection_status": "adequate",
            "costs_considered": ["[demo-smoke] demo fees and funding"],
            "uncertainty": "low",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": None,
        "market_analysis_request": None,
        "conditions_that_change_action": ["[demo-smoke] venue divergence"],
    }
    return ManagementDecisionV2.model_validate(decision).model_dump(mode="json")


def smoke_hedge_decision(
    now_ms: int, action: str, target: str, main_id: str, hedge_id: str | None
) -> dict:
    from condor.brooks.contracts import ManagementDecisionV2

    decision = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": now_ms,
        "action": action,
        "position_ids": [main_id],
        "reason": f"[demo-smoke] controlled {action} fixture to {target} (demo only)",
        "evidence": {
            "observations": ["[demo-smoke] fixture observation"],
            "evidence_for": ["[demo-smoke] lifecycle step"],
            "evidence_against": ["[demo-smoke] venue move could change the read"],
        },
        "risk": {
            "exposure_before": ["[demo-smoke] smoke MAIN plus recorded hedge"],
            "exposure_after": ["[demo-smoke] target ratio " + target],
            "protection_status": "unknown",
            "costs_considered": ["[demo-smoke] demo fees only"],
            "uncertainty": "high",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": {
            "objective": f"[demo-smoke] {action} to {target} on demo only",
            "target_hedge_ratio": target,
            "main_position_id": main_id,
            "hedge_position_id": hedge_id,
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
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    logging.getLogger("condor.brooks.gm").setLevel(logging.INFO)

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
        build_gm_factory,
        build_pm_load_context,
        build_watcher_provider,
    )
    from condor.brooks.gm import GMRejected
    from condor.brooks.position_watcher import PositionWatcher

    client = HummingbotAPIClient(
        base_url=f"http://{creds['host']}:{creds['port']}",
        username=creds["username"],
        password=creds["password"],
    )
    await client.init()
    results: dict = {"correlation_id": cid}
    smoke_opened = False

    class SmokeAbort(Exception):
        pass

    def read_live_binding() -> dict | None:
        path = state_root / "trades" / cid / "binding.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        # ---- S0 baseline ----
        banner("S0", "BASELINE (own read; unrelated state is never touched)")
        symbols = [OBSYMBOL, SMOKE_SYMBOL]
        base = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
        base_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
        log(f"BTC positions={json.dumps(base['positions'][OBSYMBOL])}")
        log(f"ETH positions={json.dumps(base['positions'][SMOKE_SYMBOL])} "
            f"active_orders={base['active_orders']} active_execs={json.dumps(base_active)} "
            f"marks={base['marks']} mode={base['position_mode']}")
        log(f"balances={json.dumps(base['balances'])}")
        if base["positions"][SMOKE_SYMBOL] or any(
            e["trading_pair"] == SMOKE_SYMBOL for e in base_active
        ):
            log("S0 FAIL-CLOSED: smoke symbol is not flat; aborting before any write")
            results["s0"] = {"status": "FAIL-CLOSED"}
            return 2
        results["baseline"] = {**base, "active_executors": base_active}

        # ---- S1 ----
        try:
            results["s1"] = await step_s1(client, args)
        except Exception as exc:  # noqa: BLE001 - record and continue, zero writes
            log(f"S1 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            results["s1"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:300]}
        log(f"S1 result: {results['s1'].get('status')}")

        # Shared production GM factory (policy sized for a tiny demo MAIN:
        # ~0.03% risk with a 2% stop targets ~$150 notional on ETH).
        class _Policy:
            risk_per_trade_pct = Decimal("0.0003")
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
        gm = gm_factory(SMOKE_SYMBOL)
        reader = HummingbotAccountReader(client, state_root, CONTROLLER)
        log("GM policy: risk=0.03% lev=5 smoke symbol=ETH-USDT (BTC residuals untouched)")

        # ---- S2 ----
        banner("S2", "CONTROLLED DEMO MAIN (fixture marked smoke, demo only)")
        try:
            now_ms = time.time_ns() // 1_000_000
            eth_mark = Decimal((await client.market_data.get_prices(
                connector_name=ALLOWED_CONNECTOR, trading_pairs=[SMOKE_SYMBOL]
            )).get("prices", {}).get(SMOKE_SYMBOL))
            intent = smoke_entry_intent(SMOKE_SYMBOL, eth_mark, now_ms)
            binding = await gm.execute_entry(intent, correlation_id=cid)
            log(f"S2 entry: executor={binding.get('main_executor_id')} "
                f"planned_qty={binding.get('planned_quantity')} "
                f"side={binding.get('main_side')} status={binding.get('status')}")
            for _ in range(12):
                if binding.get("main_position_id"):
                    break
                await asyncio.sleep(10)
                binding = await gm.reconcile_main(cid)
            if not binding.get("main_position_id") or binding.get("status") != "reconciled":
                raise SmokeAbort(f"reconciliation failed: {binding}")
            smoke_opened = True
            results["s2"] = {"status": "PASS", "binding": binding}
            log(f"S2 reconciled: main_position_id={binding.get('main_position_id')} "
                f"status={binding.get('status')}")
            seen: list = []
            provider = build_watcher_provider(
                client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
                controller_id=CONTROLLER, symbols=[SMOKE_SYMBOL], state_root=state_root,
            )
            watcher = PositionWatcher(provider, seen.append)
            await watcher.poll()
            kinds = [e.get("type") if isinstance(e, dict) else getattr(e.type, "value", str(e.type))
                     for e in seen]
            results["s2"]["watcher_events"] = kinds
            log(f"S2 watcher events: {kinds}")
            if "POSITION_OPENED" not in kinds:
                raise SmokeAbort("watcher did not report POSITION_OPENED")
            results["s2"]["active_after"] = await active_executors(
                client, ACCOUNT, ALLOWED_CONNECTOR)
        except (GMRejected, SmokeAbort) as exc:
            log(f"S2 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            results["s2"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:500]}
        log(f"S2 result: {results['s2'].get('status')}")

        # ---- S3 ----
        banner("S3", "PM PATH (production context + HOLD fixture -> ZERO writes)")
        try:
            if results["s2"].get("status") != "PASS":
                raise SmokeAbort("skipped: no smoke MAIN bound")
            from condor.brooks.adapters import HummingbotCandleSource
            from condor.brooks.pm import PositionManager

            pm_load = build_pm_load_context(
                client, account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
                controller_id=CONTROLLER, state_root=state_root,
            )
            loaded = await pm_load(cid)
            if not loaded:
                raise SmokeAbort("pm_load_context returned None despite bound MAIN")
            log(f"pm_load_context -> snapshot symbol={loaded['symbol']} "
                f"positions={len(loaded['positions'])} margin={loaded['margin_health']}")
            saved: list = []
            published: list = []

            async def _save(corr: str, decision) -> None:
                saved.append((corr, decision))

            async def _record(corr: str, record: dict) -> None:
                pass

            async def hold_runner(role, prompt, output_model, market_tools, *,
                                  agent_key, timeout_sec, max_tool_calls, user_id):
                assert role == "POSITION_MANAGER", role
                owned = [p["position_id"] for p in prompt.get("positions", [])]
                return smoke_hold_decision(prompt["decision_time_ms"], owned[0])

            async def _active(symbol) -> list:
                return []

            pm = PositionManager(
                runner=hold_runner, load_context=pm_load, save_decision=_save,
                publish=published.append,
                candle_source=HummingbotCandleSource(client, ALLOWED_CONNECTOR),
                record_market_read=_record, list_active_correlations=_active,
                agent_key=args.agent_key,
            )
            pm_out = await pm.handle_event({
                "type": "PM_TIMER", "correlation_id": cid, "symbol": SMOKE_SYMBOL,
                "created_at_ms": time.time_ns() // 1_000_000, "payload": {},
            })
            hold_rec = await gm.execute_management(
                correlation_id=cid, action="HOLD", decision_id=f"{cid}-hold")
            active_now = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
            log(f"PM decision: action={pm_out.action} saved={len(saved)} "
                f"published={len(published)} GM HOLD={hold_rec} "
                f"active_execs={len(active_now)}")
            if (pm_out.action != "HOLD" or len(saved) != 1
                    or hold_rec.get("status") != "no_write"
                    or len(active_now) != len(results["s2"].get("active_after", []))):
                raise SmokeAbort("S3 zero-write invariant broken")
            results["s3"] = {"status": "PASS", "pm_action": pm_out.action,
                             "hold": hold_rec}
        except (GMRejected, SmokeAbort) as exc:
            log(f"S3 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            results["s3"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:500]}
        log(f"S3 result: {results['s3'].get('status')}")

        # ---- S4 ----
        banner("S4", "HEDGE LIFECYCLE (full decision payloads incl. hedge_plan)")
        s4_steps = []
        try:
            if results["s2"].get("status") != "PASS":
                raise SmokeAbort("skipped: no smoke MAIN bound")

            from condor.brooks.hedge import build_hedge_state

            async def hedge_snapshot():
                snap = await reader.read(
                    account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR,
                    symbol=SMOKE_SYMBOL,
                )
                legs = list(snap.positions or [])
                hs = build_hedge_state(
                    legs,
                    main_position_id=snap.main_position_id,
                    hedge_position_id=snap.hedge_position_id,
                    as_of_ms=snap.as_of_ms,
                )
                return snap, hs

            async def wait_converged(label: str, expected_short: str) -> None:
                for _ in range(48):
                    rows = (await venue_snapshot(
                        client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL]
                    ))["positions"][SMOKE_SYMBOL]
                    shorts = [r for r in rows if r.get("side") == "SHORT"]
                    merged = (len(shorts) == 1 and abs(Decimal(str(shorts[0]["amount"])))
                              == Decimal(expected_short))
                    if expected_short == "0" and not shorts:
                        log(f"S4 {label}: converged rows={json.dumps(rows)}")
                        return
                    live = read_live_binding() or {}
                    try:
                        fetched = await client.executors.get_executor(
                            executor_id=live.get("hedge_executor_id") or "")
                        exec_seen = bool(fetched)
                    except Exception:
                        exec_seen = False
                    if merged and exec_seen:
                        log(f"S4 {label}: converged rows={json.dumps(rows)}")
                        return
                    await asyncio.sleep(10)
                log(f"S4 {label}: not converged within bound; continuing")

            def raw_hedge_view(rows: list) -> tuple[Decimal, Decimal, Decimal | None]:
                short = sum(abs(Decimal(str(r.get("amount", 0))))
                            for r in rows if r.get("side") == "SHORT")
                long = sum(abs(Decimal(str(r.get("amount", 0))))
                           for r in rows if r.get("side") == "LONG")
                return short, long, (short / long if long else None)

            for step, (action, target) in enumerate(
                (("HEDGE", "0.30"), ("INCREASE_HEDGE", "0.50"),
                 ("REDUCE_HEDGE", "0.20"), ("REMOVE_HEDGE", "0"))
            ):
                live = read_live_binding()
                before_snap, before_hs = await hedge_snapshot()
                pre_rows = (await venue_snapshot(
                    client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL]
                ))["positions"][SMOKE_SYMBOL]
                try:
                    pre_fetch = await client.executors.get_executor(
                        executor_id=(live.get("hedge_executor_id") or ""))
                    pre_fetch_seen = bool(pre_fetch)
                except Exception as exc:
                    pre_fetch_seen = f"ERR {type(exc).__name__}"
                log(f"S4 {action} pre-step: reader_hedge={before_hs.hedge_size} "
                    f"rows={json.dumps(pre_rows)} hedge_exec_fetch={pre_fetch_seen}")
                dec = smoke_hedge_decision(
                    time.time_ns() // 1_000_000, action, target,
                    live["main_position_id"], live.get("hedge_position_id"),
                )
                rec = await gm.execute_management(
                    correlation_id=cid, decision_id=f"{cid}-s4-{step}-{action}",
                    decision=dec,
                )
                after_snap, after_hs = await hedge_snapshot()
                filled = Decimal(str(rec.get("filled_quantity", "0")))
                before_size = Decimal(str(before_hs.hedge_size))
                opening = action in ("HEDGE", "INCREASE_HEDGE")
                expected_size = (before_size + filled) if opening else (before_size - filled)
                short_sum, long_sum, ratio_now = raw_hedge_view(
                    (await venue_snapshot(
                        client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL]
                    ))["positions"][SMOKE_SYMBOL])
                for _ in range(12):
                    if short_sum == expected_size:
                        break
                    await asyncio.sleep(10)
                    short_sum, long_sum, ratio_now = raw_hedge_view(
                        (await venue_snapshot(
                            client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL]
                        ))["positions"][SMOKE_SYMBOL])
                eth_rows = (await venue_snapshot(
                    client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL]
                ))["positions"][SMOKE_SYMBOL]
                log(f"S4 {action}->{target}: qty={rec.get('quantity')} "
                    f"filled={rec.get('filled_quantity')} side={rec.get('side')} "
                    f"assessment={rec.get('assessment')} hedge {before_size}->{short_sum} "
                    f"ratio={ratio_now} reader={after_hs.hedge_size} venue={json.dumps(eth_rows)}")
                if (rec.get("assessment") != "confirmed" or short_sum != expected_size
                        or (action != "REMOVE_HEDGE" and ratio_now is not None
                            and abs(ratio_now - Decimal(target)) > Decimal("0.025"))
                        or (action == "REMOVE_HEDGE" and short_sum != 0)):
                    raise SmokeAbort(f"{action}: delta/ratio mismatch: {rec}")
                s4_steps.append({"action": action, "target": target,
                                 "status": "PASS", "record": rec,
                                 "hedge_size": str(short_sum),
                                 "hedge_ratio": str(ratio_now)})
                await wait_converged(action, str(short_sum))
            results["s4"] = {"steps": s4_steps, "status": "PASS"}
        except (GMRejected, SmokeAbort) as exc:
            log(f"S4 FAIL-CLOSED: {type(exc).__name__}: {exc}")
            results["s4"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:500],
                             "steps": s4_steps}
        log(f"S4 result: {results['s4'].get('status')}")

        # ---- S5 (guaranteed): close the smoke MAIN if it was ever opened ----
        banner("S5", "CLEANUP (management CLOSE of the smoke MAIN)")
        try:
            live = read_live_binding()
            if smoke_opened and live and live.get("main_executor_id"):
                rec = await gm.execute_management(
                    correlation_id=cid, action="CLOSE",
                    decision_id=f"{cid}-close")
                log(f"S5 CLOSE record: {json.dumps(rec, sort_keys=True)}")
                for _ in range(12):
                    snap = await venue_snapshot(
                        client, ACCOUNT, ALLOWED_CONNECTOR, [SMOKE_SYMBOL])
                    mine = [e for e in await active_executors(
                        client, ACCOUNT, ALLOWED_CONNECTOR)
                        if e.get("controller_id") == CONTROLLER]
                    if not snap["positions"][SMOKE_SYMBOL] and not mine:
                        break
                    await asyncio.sleep(5)
                smoke_opened = bool(
                    snap["positions"][SMOKE_SYMBOL] or mine)
                results["s5"] = {"status": "PASS" if not smoke_opened else "FAIL-CLOSED",
                                 "record": rec}
            else:
                log("S5: no smoke MAIN open; verifying venue flat instead")
                results["s5"] = {"status": "PASS",
                                 "note": "nothing opened, nothing to close"}
        except GMRejected as exc:
            log(f"S5 FAIL-CLOSED: GMRejected: {exc}")
            results["s5"] = {"status": "FAIL-CLOSED", "reason": str(exc)[:500]}
        final = await venue_snapshot(client, ACCOUNT, ALLOWED_CONNECTOR, symbols)
        final_active = await active_executors(client, ACCOUNT, ALLOWED_CONNECTOR)
        btc_same = (final["positions"][OBSYMBOL] == base["positions"][OBSYMBOL]
                    and final["active_orders"] == base["active_orders"])
        eth_flat = (final["positions"][SMOKE_SYMBOL] == []
                    and not any(e.get("controller_id") == CONTROLLER
                                for e in final_active))
        log(f"final BTC={json.dumps(final['positions'][OBSYMBOL])} "
            f"ETH={json.dumps(final['positions'][SMOKE_SYMBOL])} "
            f"active_orders={final['active_orders']} "
            f"active_execs={json.dumps(final_active)}")
        log(f"BTC baseline match: {btc_same} ETH flat + no smoke execs: {eth_flat}")
        results["s5"]["baseline_match"] = btc_same and eth_flat
        results["final"] = {**final, "active_executors": final_active}

        banner("SUMMARY", "per-step results (PASS = observed, FAIL-CLOSED = safe abort)")
        for key in ("s1", "s2", "s3", "s4", "s5"):
            r = results.get(key, {})
            log(f"{key.upper()}: {r.get('status')}")

        ok = (all(results.get(k, {}).get("status") == "PASS"
                  for k in ("s1", "s2", "s3", "s4", "s5"))
              and not smoke_opened and btc_same and eth_flat)
        if not ok:
            log("SMOKE RESULT: INCOMPLETE OR INVARIANT VIOLATION (see above)")
            return 2
        log("SMOKE RESULT: done; full lifecycle on demo with real writes, "
            "venue returned to baseline")
        return 0
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort session teardown
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
