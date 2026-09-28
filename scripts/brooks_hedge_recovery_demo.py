#!/usr/bin/env python3
"""Brooks hedge recovery DEMO runner -- real orders on the live demo venue.

Drives the PRODUCTION Brooks classes (GM, adapters, position readers) against
connector ``binance_perpetual_demo`` and submits REAL demo orders. It forces
the exact failure the recovery path exists for:

  R0  baseline read (ETH must be flat; BTC state is never touched)
  R1  REAL MAIN: fixture TradeIntentV2 -> BrooksGM.execute_entry + reconcile
  R2  REAL HEDGE then FROZEN READS: the real hedge order is sent through the
      Hummingbot execution port, while a harness reader wrapper replays the
      pre-write snapshot for every post-write read. The write therefore lands
      on the venue but the GM cannot corroborate it and wedges the binding at
      ``reconciliation_required``. New hedge writes are rejected (fail closed).
  R3  REAL RECOVERY: the wrapper is unfrozen and ``BrooksGM.reconcile_hedge``
      reads the real venue, rebuilds MAIN/HEDGE ownership and confirms the
      pending record. No new order is submitted by recovery.
  R4  REAL FOLLOW-UP: a new INCREASE_HEDGE decision compiles against the
      recovered state and clears the venue minimum notional.
  R5  CLEANUP: REMOVE_HEDGE + CLOSE MAIN; ETH flat, no runner executors.

Only the reader used by the GM is wrapped (a read-only fault injection); the
execution port, the venue and the reconciliation are production code. The
recovery is the code under test: R3 asserts it submits no order and no new
executor appears.

Usage (from the repo root):
  PYTHONPATH=. /path/to/condor/.venv/bin/python scripts/brooks_hedge_recovery_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

ALLOWED_CONNECTOR = "binance_perpetual_demo"
ACCOUNT = "master_account"
SYMBOL = "ETH-USDT"
UNTOUCHABLE = ("BTC-USDT",)
CONTROLLER = "brooks-recovery-demo"


def log(msg: str) -> None:
    print(msg, flush=True)


def banner(step: str, title: str) -> None:
    log(f"\n===== {step}: {title} =====")


def short(value: str | None) -> str:
    if not isinstance(value, str):
        return "?"
    return value if len(value) <= 18 else value[:15] + "..."


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


async def venue_snapshot(client, symbols: list[str]) -> dict:
    """Redacted public demo state only: no credentials, no private data."""
    pos = await client.trading.get_positions(
        account_names=[ACCOUNT], connector_names=[ALLOWED_CONNECTOR], limit=1000
    )
    state = await client.portfolio.get_state(
        account_names=[ACCOUNT], connector_names=[ALLOWED_CONNECTOR], skip_gateway=True
    )
    prices = await client.market_data.get_prices(
        connector_name=ALLOWED_CONNECTOR, trading_pairs=symbols
    )
    mode = await client.trading.get_position_mode(
        account_name=ACCOUNT, connector_name=ALLOWED_CONNECTOR
    )
    rows = pos.get("data", []) if isinstance(pos, dict) else []
    by_symbol: dict[str, list] = {s: [] for s in symbols}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pair = row.get("trading_pair", row.get("symbol"))
        by_symbol.setdefault(str(pair), []).append(
            {
                "side": row.get("side", row.get("position_side")),
                "amount": str(row.get("amount", row.get("net_amount_base"))),
                "entry_price": str(row.get("entry_price", "?")),
                "has_venue_position_id": bool(
                    row.get("position_id") or row.get("positionId") or row.get("id")
                ),
            }
        )
    balances = []
    if isinstance(state, dict):
        for acct_rows in state.values():
            if isinstance(acct_rows, dict):
                for crow in acct_rows.get(ALLOWED_CONNECTOR, []) or []:
                    if isinstance(crow, dict) and str(
                        crow.get("token", crow.get("asset", ""))
                    ).upper().split("-")[0] in ("USDT", "USDC"):
                        balances.append(
                            {
                                "token": crow.get("token", crow.get("asset")),
                                "value": str(crow.get("value", crow.get("usd_value"))),
                            }
                        )
    marks = prices.get("prices", {}) or {} if isinstance(prices, dict) else {}
    return {
        "positions": by_symbol,
        "balances": balances,
        "marks": {s: str(marks.get(s)) for s in symbols},
        "position_mode": (
            str(mode.get("position_mode")) if isinstance(mode, dict) else "?"
        ),
    }


def _is_active_executor(row: dict) -> bool:
    if row.get("is_active") is True:
        return True
    if (
        row.get("is_active") is None
        and str(row.get("status") or "").upper() == "RUNNING"
    ):
        return True
    return False


async def runner_executor_ids(client) -> set[str]:
    """All executor ids of this runner's controller, any status."""
    res = await client.executors.search_executors(
        account_names=[ACCOUNT], connector_names=[ALLOWED_CONNECTOR], limit=1000
    )
    data = res.get("data", []) if isinstance(res, dict) else []
    return {
        str(row.get("executor_id") or row.get("id"))
        for row in data
        if isinstance(row, dict) and str(row.get("controller_id")) == CONTROLLER
    }


async def active_runner_executors(client) -> list[dict]:
    res = await client.executors.search_executors(
        account_names=[ACCOUNT], connector_names=[ALLOWED_CONNECTOR], limit=1000
    )
    data = res.get("data", []) if isinstance(res, dict) else []
    return [
        {
            "id": short(row.get("executor_id") or row.get("id")),
            "status": row.get("status"),
        }
        for row in data
        if isinstance(row, dict)
        and str(row.get("controller_id")) == CONTROLLER
        and _is_active_executor(row)
    ]


def entry_intent(mark: Decimal, now_ms: int) -> dict:
    from condor.brooks.contracts import TradeIntentV2

    trig = mark.quantize(Decimal("0.01"))
    stop = (trig * Decimal("0.98")).quantize(Decimal("0.01"))
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
        "symbol": SYMBOL,
        "decision_time_ms": now_ms,
        "market_context": {
            "smoke": True,
            "recovery_test": True,
            "note": "demo-recovery controlled fixture; demo venue only",
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
            "reference": "demo-recovery live mark",
            "price_field": "close",
            "price": format(trig, "f"),
            "source": src,
        },
        "invalidation": {
            "reference": "demo-recovery structural stop (2%)",
            "price_field": "low",
            "price": format(stop, "f"),
            "source": src,
        },
        "evidence_for": ["[demo-recovery] controlled fixture for recovery validation"],
        "evidence_against": ["[demo-recovery] no real signal asserted"],
        "qualitative_confidence": "low",
        "uncertainty": ["[demo-recovery] fixture carries no market view"],
        "conditions_that_change_market_read": ["[demo-recovery] n/a fixture"],
    }
    TradeIntentV2.model_validate(intent)
    return intent


def hedge_decision(
    now_ms: int, action: str, target: str, main_id: str, hedge_id: str | None
) -> dict:
    from condor.brooks.contracts import ManagementDecisionV2

    decision = {
        "schema": "brooks.management-decision.v2",
        "role": "POSITION_MANAGER",
        "decision_time_ms": now_ms,
        "action": action,
        "position_ids": [main_id],
        "reason": f"[demo-recovery] controlled {action} fixture to {target} (demo only)",
        "evidence": {
            "observations": ["[demo-recovery] fixture observation"],
            "evidence_for": ["[demo-recovery] recovery lifecycle step"],
            "evidence_against": ["[demo-recovery] venue move could change the read"],
        },
        "risk": {
            "exposure_before": ["[demo-recovery] MAIN plus recorded hedge"],
            "exposure_after": ["[demo-recovery] target ratio " + target],
            "protection_status": "unknown",
            "costs_considered": ["[demo-recovery] demo fees only"],
            "uncertainty": "high",
        },
        "execution": {"orders": [], "cancel_order_ids": [], "replace_orders": []},
        "hedge_plan": {
            "objective": f"[demo-recovery] {action} to {target} on demo only",
            "target_hedge_ratio": target,
            "main_position_id": main_id,
            "hedge_position_id": hedge_id,
            "ratio_basis": "absolute_mark_notional",
            "expected_effect_on_exposure": "[demo-recovery] net exposure falls",
            "costs": ["[demo-recovery] demo fees and funding"],
            "unlock_condition": "[demo-recovery] target reached",
            "failure_condition": "[demo-recovery] venue state diverges",
        },
        "market_analysis_request": None,
        "conditions_that_change_action": ["[demo-recovery] venue divergence"],
    }
    ManagementDecisionV2.model_validate(decision)
    return decision


def make_policy():
    from decimal import Decimal as D

    class _Policy:
        # Same tiny sizing as the demo smoke: ~$150 notional on ETH so every
        # hedge delta clears the $5.00 venue min_notional.
        risk_per_trade_pct = D("0.0003")
        max_positions = 2
        max_gross_exposure_pct = D("2")
        leverage = 5
        take_profit_r = D("2")
        time_limit_sec = 3600
        max_trigger_drift_pct = D("0.05")
        max_snapshot_age_ms = 60_000
        max_intent_age_ms = 7_200_000

    return _Policy()


class FrozenReader:
    """Harness fault injection: replay the first read while frozen.

    The real reader performs every read; only the GM's post-write reads are
    forced to replay the pre-write snapshot, exactly like a venue whose
    position index never catches up. No venue write is affected.
    """

    def __init__(self, real):
        self.real = real
        self.freeze = False
        self.frozen = None

    async def read(self, **kwargs):
        if self.freeze:
            if self.frozen is None:
                self.frozen = await self.real.read(**kwargs)
            return self.frozen
        return await self.real.read(**kwargs)


def read_binding(state_root: Path, cid: str) -> dict | None:
    path = state_root / "trades" / cid / "binding.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def raw_short_size(rows: list[dict]) -> Decimal:
    return sum(
        abs(Decimal(str(row.get("amount", 0))))
        for row in rows
        if row.get("side") == "SHORT"
    )


def raw_long_size(rows: list[dict]) -> Decimal:
    return sum(
        abs(Decimal(str(row.get("amount", 0))))
        for row in rows
        if row.get("side") == "LONG"
    )


async def wait_for_size(client, expected: Decimal, label: str) -> None:
    for _ in range(12):
        rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
        if raw_short_size(rows) == expected:
            log(f"{label}: venue converged to {expected}")
            return
        await asyncio.sleep(5)
    rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
    raise RuntimeError(
        f"{label}: venue short {raw_short_size(rows)} != expected {expected}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="Brooks hedge recovery demo runner")
    ap.add_argument(
        "--config", default="/home/valdemaster/brooks-condor/condor/config.yml"
    )
    ap.add_argument("--server", default="local")
    ap.add_argument("--state-root", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    logging.getLogger("condor.brooks.gm").setLevel(logging.INFO)

    from hummingbot_api_client import HummingbotAPIClient

    ts = int(time.time())
    cid = f"demo-recovery-{ts}"
    state_root = (
        Path(args.state_root)
        if args.state_root
        else Path(f"/tmp/brooks-recovery-demo-{ts}")
    )
    state_root.mkdir(parents=True, exist_ok=True)
    results: dict = {"correlation_id": cid, "state_root": str(state_root)}

    log("Brooks DEMO hedge recovery (REAL demo orders, reader fault injection only)")
    log(f"correlation={cid} state_root={state_root} controller={CONTROLLER}")
    creds = read_server_creds(args.config, args.server)
    log(
        f"venue=http://{creds['host']}:{creds['port']} server={args.server} "
        f"account={ACCOUNT} connector={ALLOWED_CONNECTOR} (credentials redacted)"
    )

    from condor.brooks import gm as gm_module
    from condor.brooks.adapters import HummingbotAccountReader, build_gm_factory
    from condor.brooks.gm import GMRejected
    from condor.brooks.hedge import build_hedge_state

    # Harness speedup: the wedge replicates 6 corroborating reads; the frozen
    # replay makes them all return instantly, so the production 10s pacing is
    # unnecessary. Recovery keeps the production pacing (a real second read).
    gm_module._HEDGE_RECORROBORATE_DELAY_SEC = 0

    client = HummingbotAPIClient(
        base_url=f"http://{creds['host']}:{creds['port']}",
        username=creds["username"],
        password=creds["password"],
    )
    await client.init()
    main_opened = False
    try:
        # ---- R0 baseline ----
        banner("R0", "BASELINE (ETH must be flat; BTC never touched)")
        base = await venue_snapshot(client, [SYMBOL, *UNTOUCHABLE])
        base_active = await active_runner_executors(client)
        base_btc = base["positions"].get(UNTOUCHABLE[0], [])
        log(f"ETH positions={json.dumps(base['positions'][SYMBOL])}")
        log(f"BTC positions={json.dumps(base_btc)}")
        log(
            f"marks={base['marks']} mode={base['position_mode']} balances={base['balances']}"
        )
        if base["positions"][SYMBOL]:
            log("R0 FAIL-CLOSED: ETH-USDT is not flat; aborting before any write")
            return 2
        if base["position_mode"] != "HEDGE":
            log("R0 FAIL-CLOSED: position mode is not HEDGE; aborting")
            return 2
        results["baseline"] = base

        gm_factory = build_gm_factory(
            client,
            account_name=ACCOUNT,
            connector_name=ALLOWED_CONNECTOR,
            controller_id=CONTROLLER,
            state_root=state_root,
            policy_config=make_policy(),
        )
        gm = gm_factory(SYMBOL)
        real_reader = HummingbotAccountReader(client, state_root, CONTROLLER)
        frozen = FrozenReader(real_reader)
        gm.reader = frozen

        # ---- R1 REAL MAIN ----
        banner("R1", "REAL MAIN (controlled fixture, real demo order)")
        now_ms = time.time_ns() // 1_000_000
        eth_mark = Decimal(
            (
                await client.market_data.get_prices(
                    connector_name=ALLOWED_CONNECTOR, trading_pairs=[SYMBOL]
                )
            )
            .get("prices", {})
            .get(SYMBOL)
        )
        binding = await gm.execute_entry(
            entry_intent(eth_mark, now_ms), correlation_id=cid
        )
        log(
            f"R1 entry: executor={short(binding.get('main_executor_id'))} "
            f"qty={binding.get('planned_quantity')} side={binding.get('main_side')}"
        )
        for _ in range(12):
            if binding.get("main_position_id"):
                break
            await asyncio.sleep(10)
            binding = await gm.reconcile_main(cid)
        if not binding.get("main_position_id") or binding.get("status") != "reconciled":
            raise RuntimeError(f"MAIN reconciliation failed: {binding}")
        main_opened = True
        results["r1_main"] = {"status": "PASS", "binding": binding}
        log(
            f"R1 reconciled: main_position_id={short(binding.get('main_position_id'))} "
            f"status={binding.get('status')}"
        )

        # ---- R2 REAL HEDGE + FROZEN READS -> reconciliation_required ----
        banner("R2", "REAL HEDGE with frozen post-write reads (wedge on purpose)")
        live = read_binding(state_root, cid) or {}
        pre_rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
        frozen.freeze = True
        frozen.frozen = None
        wedged_error = None
        hedge_kwargs = dict(
            correlation_id=cid,
            decision_id=f"{cid}-wedge-HEDGE",
            decision=hedge_decision(
                time.time_ns() // 1_000_000,
                "HEDGE",
                "0.30",
                live["main_position_id"],
                live.get("hedge_position_id"),
            ),
        )
        try:
            await gm.execute_management(**hedge_kwargs)
            raise RuntimeError("wedge did not trigger: hedge unexpectedly confirmed")
        except GMRejected as exc:
            wedged_error = str(exc)
        finally:
            frozen.freeze = False
        binding = read_binding(state_root, cid) or {}
        record_path = (
            state_root / "trades" / cid / "management" / f"{cid}-wedge-HEDGE.json"
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        expected_size = Decimal(str(record["expected_hedge_size"]))
        post_rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
        venue_short = raw_short_size(post_rows)
        for _ in range(12):  # the demo fill may lag the frozen reads
            if venue_short == expected_size:
                break
            await asyncio.sleep(5)
            post_rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
            venue_short = raw_short_size(post_rows)
        log(
            f"R2 wedge: error={short(wedged_error)} binding_status={binding.get('status')} "
            f"record_status={record.get('status')} "
            f"assessment={record.get('assessment_status')} "
            f"executor={short(record.get('executor_id'))} "
            f"venue_short={venue_short} pre_rows={json.dumps(pre_rows)} "
            f"post_rows={json.dumps(post_rows)}"
        )
        if binding.get("status") != "reconciliation_required":
            raise RuntimeError(f"binding did not wedge: {binding.get('status')}")
        if record.get("status") != "reconciliation_required":
            raise RuntimeError(f"record did not wedge: {record.get('status')}")
        if record.get("assessment_status") not in ("ambiguous", "partial"):
            raise RuntimeError(
                f"unexpected wedge assessment: {record.get('assessment_status')}"
            )
        if venue_short <= 0:
            raise RuntimeError("real hedge order did not land on the venue")
        if venue_short != expected_size:
            raise RuntimeError(
                f"venue hedge {venue_short} != recorded expected {expected_size}"
            )
        execs_wedged = await runner_executor_ids(client)

        # Writes stay blocked while wedged.
        try:
            await gm.execute_management(
                correlation_id=cid,
                decision_id=f"{cid}-blocked-INCREASE",
                decision=hedge_decision(
                    time.time_ns() // 1_000_000,
                    "INCREASE_HEDGE",
                    "0.50",
                    live["main_position_id"],
                    binding.get("hedge_position_id"),
                ),
            )
            raise RuntimeError("wedged binding allowed a new hedge write")
        except GMRejected as exc:
            if "reconcile" not in str(exc):
                raise
            log(f"R2 blocked follow-up as expected: {short(str(exc))}")
        if await runner_executor_ids(client) != execs_wedged:
            raise RuntimeError("blocked follow-up created an executor")
        results["r2_wedge"] = {
            "status": "PASS",
            "error": wedged_error,
            "binding_status": binding["status"],
            "record_status": record["status"],
            "assessment_status": record.get("assessment_status"),
            "hedge_executor_id": record.get("executor_id"),
            "expected_hedge_size": str(expected_size),
            "venue_short": str(venue_short),
        }

        # ---- R3 REAL RECOVERY (no new order) ----
        banner("R3", "REAL RECOVERY via reconcile_hedge (read-only, no new order)")
        recovery = await gm.reconcile_hedge(cid)
        binding = read_binding(state_root, cid) or {}
        hedge_state = json.loads(
            (state_root / "trades" / cid / "hedge_state.json").read_text(
                encoding="utf-8"
            )
        )
        executions = read_records(state_root / "trades" / cid / "executions.jsonl")
        venue_rows = (await venue_snapshot(client, [SYMBOL]))["positions"][SYMBOL]
        execs_after = await runner_executor_ids(client)
        log(
            f"R3 recovery: status={recovery.get('status')} "
            f"reason={short(recovery.get('reason'))} "
            f"binding={binding.get('status')} hedge_id={short(binding.get('hedge_position_id'))} "
            f"hedge_size={binding.get('hedge_size')} state={hedge_state.get('hedge_size')} "
            f"venue_short={raw_short_size(venue_rows)} execs_delta="
            f"{sorted(execs_after - execs_wedged)}"
        )
        if recovery.get("status") != "confirmed":
            raise RuntimeError(f"recovery did not confirm: {recovery}")
        if binding.get("status") != "reconciled":
            raise RuntimeError(f"binding not reconciled: {binding}")
        if Decimal(str(binding.get("hedge_size"))) != expected_size:
            raise RuntimeError(
                f"binding hedge_size {binding.get('hedge_size')} != {expected_size}"
            )
        if Decimal(str(hedge_state.get("hedge_size"))) != expected_size:
            raise RuntimeError(
                f"hedge_state {hedge_state.get('hedge_size')} != {expected_size}"
            )
        if raw_short_size(venue_rows) != expected_size:
            await wait_for_size(client, expected_size, "R3")
        if execs_after != execs_wedged:
            raise RuntimeError(
                f"recovery submitted an order: new executors {execs_after - execs_wedged}"
            )
        recovered_entries = [row for row in executions if row.get("recovered")]
        if not recovered_entries or recovered_entries[-1].get("status") != "confirmed":
            raise RuntimeError(
                f"executions audit missing recovered entry: {executions}"
            )
        results["r3_recovery"] = {
            "status": "PASS",
            "recovery": recovery,
            "binding_status": binding["status"],
            "hedge_size": str(expected_size),
            "executors_added": 0,
        }

        # ---- R4 REAL FOLLOW-UP (new decision after recovery) ----
        banner("R4", "REAL INCREASE to 0.50 after recovery (new decision)")
        before = Decimal(str(hedge_state["hedge_size"]))
        rec = await gm.execute_management(
            correlation_id=cid,
            decision_id=f"{cid}-r4-INCREASE",
            decision=hedge_decision(
                time.time_ns() // 1_000_000,
                "INCREASE_HEDGE",
                "0.50",
                live["main_position_id"],
                binding.get("hedge_position_id"),
            ),
        )
        after_binding = read_binding(state_root, cid) or {}
        after_size = Decimal(str(after_binding.get("hedge_size")))
        log(
            f"R4 INCREASE->0.50: qty={rec.get('quantity')} filled={rec.get('filled_quantity')} "
            f"assessment={rec.get('assessment')} hedge {before}->{after_size}"
        )
        if rec.get("assessment") != "confirmed" or after_size <= before:
            raise RuntimeError(f"follow-up INCREASE failed: {rec}")
        results["r4_followup"] = {
            "status": "PASS",
            "record": rec,
            "hedge_size": str(after_size),
        }

        # ---- R5 cleanup ----
        banner("R5", "CLEANUP (REMOVE_HEDGE + CLOSE MAIN; venue back to baseline)")
        removal = await gm.execute_management(
            correlation_id=cid,
            decision_id=f"{cid}-r5-REMOVE",
            decision=hedge_decision(
                time.time_ns() // 1_000_000,
                "REMOVE_HEDGE",
                "0",
                live["main_position_id"],
                (read_binding(state_root, cid) or {}).get("hedge_position_id"),
            ),
        )
        close_rec = await gm.execute_management(
            correlation_id=cid, action="CLOSE", decision_id=f"{cid}-r5-CLOSE"
        )
        final = None
        for _ in range(24):
            final = await venue_snapshot(client, [SYMBOL, *UNTOUCHABLE])
            mine = await active_runner_executors(client)
            if not final["positions"][SYMBOL] and not mine:
                break
            await asyncio.sleep(5)
        final_btc = final["positions"].get(UNTOUCHABLE[0], []) if final else []
        log(
            f"R5 REMOVE assessment={removal.get('assessment')} "
            f"close={close_rec.get('status')} final_ETH={json.dumps(final_btc if final is None else final['positions'][SYMBOL])} "
            f"BTC_same={final_btc == base_btc} active_runner={await active_runner_executors(client)}"
        )
        if removal.get("assessment") != "confirmed":
            raise RuntimeError(f"REMOVE failed: {removal}")
        if final is None or final["positions"][SYMBOL]:
            raise RuntimeError("ETH position still open after cleanup")
        if final_btc != base_btc:
            raise RuntimeError("BTC baseline changed; unrelated state was touched")
        main_opened = False  # cleanup verified: ETH flat, no runner executors
        results["r5_cleanup"] = {
            "status": "PASS",
            "remove": removal,
            "close": close_rec,
            "btc_same": True,
        }

        banner("SUMMARY", "REAL demo writes exercised; recovery submitted no order")
        for key in ("r1_main", "r2_wedge", "r3_recovery", "r4_followup", "r5_cleanup"):
            log(f"{key.upper()}: {results.get(key, {}).get('status')}")
        (state_root / "recovery_summary.json").write_text(
            json.dumps(results, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        log(f"evidence: {state_root / 'recovery_summary.json'}")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        log(f"FAIL-CLOSED: {type(exc).__name__}: {exc}")
        try:
            live = read_binding(state_root, cid) or {}
            log(f"binding at abort: {json.dumps(live, default=str)}")
        except Exception:
            pass
        return 2
    finally:
        if main_opened:
            log(
                "NOTE: cleanup incomplete; manual check required for "
                f"controller={CONTROLLER} state_root={state_root}"
            )
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
