# Brooks DEMO restart validation — evidence (Wave 5, real process boundary)

Runner: `scripts/brooks_restart_demo.py` (production Brooks classes only:
adapters, GM, watcher, PM context, PositionManager, contracts; demo-only
writes, fixtures marked `demo-restart-<ts>` + `{"smoke": true,
"restart_test": true, "demo": true}`).
Validates the "Real restart validation plan" in
`docs/brooks_review_findings.md` (Section 2) on symbol **SOL-USDT**
(isolated from the smoke's ETH/BTC).

Passing run: 2026-09-28 ~10:03–10:25 UTC, correlation
`demo-restart-1790585031`, state root `/tmp/brooks-restart-1790585031`.
Two separate OS processes; Phase A exited before Phase B started.
Commands (from repo root, shared venv):

```sh
PYTHONPATH=/home/valdemaster/orca/workspaces/condor/brooks-restart \
  /home/valdemaster/brooks-condor/condor/.venv/bin/python scripts/brooks_restart_demo.py \
  --phase A --state-root /tmp/brooks-restart-1790585031 --correlation-id demo-restart-1790585031
# (process exits; fresh process:)
PYTHONPATH=/home/valdemaster/orca/workspaces/condor/brooks-restart \
  /home/valdemaster/brooks-condor/condor/.venv/bin/python scripts/brooks_restart_demo.py \
  --phase B --state-root /tmp/brooks-restart-1790585031 --correlation-id demo-restart-1790585031
```

Venue: `http://localhost:8000` server `local`, account `master_account`,
connector `binance_perpetual_demo` (demo, no real funds). Credentials read
from the Condor config; never printed or committed. Controller id
`brooks-restart-demo`. Executor ids below truncated to 8 chars.

## Results (all PASS, exit 0 both phases)

| Step | Result | Observed |
|------|--------|----------|
| A0 baseline | recorded | SOL-USDT flat (`[]`); active orders 0; active executors 0; mode HEDGE; mark SOL 118.03; BTC residuals untouched (SHORT −0.0007 @83843.7, LONG +0.0007 @84379.9, SHORT −0.0007 @83843.7); ETH flat |
| A1 controlled MAIN | **PASS** | Fixture ENTER_LONG (trigger = live SOL mark 118.03, ~9% stop, risk 0.05% → **0.49 SOL** ≈ $57.8) → MAIN executor `HmwxBQKH…` opened → binding reconciled (`executor:HmwxBQKH…`, status `reconciled`) → watcher poll emitted **POSITION_OPENED** (+ORDER_CHANGED) |
| A2 first HEDGE 0.30 | **PASS** | Full ManagementDecisionV2 incl. hedge_plan → SELL OPEN 0.14, `assessment=confirmed` first attempt, hedge 0→0.14, ratio 0.2857; venue [LONG 0.49, SHORT −0.14] |
| A3 durable summary | recorded | `PHASE_A_DURABLE_STATE` JSON printed (correlation, executor ids, binding/hedge paths + SHA256, hedge ids, as_of); trade dir: binding.json, executions.jsonl, hedge_state.json, management/, original_trade_intent.json. Process exited WITHOUT cleanup |
| B0 load durable | **PASS** | Fresh process; exactly 1 correlation dir; binding status `reconciled`; hedge_state main=0.49 hedge=0.14 ratio=0.2856 |
| B1 census | recorded | SOL [LONG 0.49, SHORT −0.14]; 1 active SOL executor (the MAIN `HmwxBQKH…`); BTC rows byte-identical to A0 |
| B2 reconcile | **PASS** | Fresh reader/GM stack: MAIN + HEDGE ownership match durable binding, `structure=single_main`, hedge 0.49/0.14 ratio 0.2857 |
| B3 watcher | **PASS** | Seeded resume (1 snapshot, fingerprint `edbcc62b…`): resume poll emitted **zero** events — no spurious OPENED/CLOSED |
| B4 PM→GM REDUCE | **PASS** | `pm_load_context` → SOL, 2 positions, SAFE; production PositionManager emitted REDUCE_HEDGE 0.15 (saved=1, published=1); GM executed BUY CLOSE 0.06: attempt 0 `ambiguous` (post-write read lag), attempt 1 `confirmed` via bounded corroboration; **filled=0.06 == requested == venue delta (0.14→0.08)**; ratio 0.1633 (target 0.15 ± 0.03) |
| B5 no-new-trade audit | **PASS** | Correlation dirs 1→1; main executor id unchanged; SOL active-executor ids before == after (`new=[]` — the REDUCE order-executor had already terminated; zero new active executors) |
| B6 cleanup | **PASS** | REMOVE_HEDGE → BUY CLOSE 0.08 `confirmed` first attempt; CLOSE MAIN 0.49 submitted; final: SOL `[]`, zero SOL executors, BTC/ETH byte-identical to baseline |
| Independent re-check | confirmed | Separate venue query after the run: SOL `[]`, 0 active executors venue-wide, 0 active orders, BTC rows identical |

## Sizing deviation from the plan (forced by live venue rules)

The plan nominally sizes the MAIN at 0.05 SOL with a 0.30 hedge leg
(≈$1.80 at SOL ~$118–120). The live venue serves `min_notional_size =
5.0` USDT, and the production GM fail-closes every hedge OPEN delta and
every REDUCE_HEDGE delta below minimum notional — a 0.05 SOL MAIN cannot
pass A2, let alone the B4 REDUCE. The runner therefore sizes the MAIN via
the production risk policy (0.05% risk, ~9% stop → 0.49 SOL ≈ $58
notional) so HEDGE 0.30 (~$16.5), REDUCE 0.30→0.15 (~$7.1) and REMOVE all
clear the $5.00 floor. Still a tiny demo fixture; no production code was
touched to accommodate it.

## Incidents during the run (both handled, both recorded)

1. **PM decision-time fail-closed (script bug, first Phase B attempt).**
   The scripted PM runner stamped its own clock instead of echoing
   `prompt["decision_time_ms"]`; production `PositionManager.handle_event`
   correctly raised `ValueError: PM decision time differs from its read
   snapshot` with **zero venue writes**. Fixed in the script (echo the
   prompt time, same pattern as the smoke S3 HOLD runner); no production
   change.
2. **Venue TIME_LIMIT auto-close during the idle gap (first correlation
   `demo-restart-1790571938`).** The position executor's triple-barrier
   `time_limit` (3600 s, from the script policy) expired while the session
   was idle; the venue closed the MAIN LONG (`close_type: TIME_LIMIT`,
   close 119.09, pnl −0.40) leaving the SHORT −0.14 orphaned with both
   executors TERMINATED. Recovery: closed the orphan SHORT with a single
   venue-level BUY 0.14 MARKET `position_action=CLOSE` (SOL only, own
   fixture), verified SOL flat, then re-ran A→B back-to-back under a fresh
   correlation with the script policy `time_limit_sec = 86400` (script-side
   config only; production frozen). The passing run above is that fresh
   correlation. Lesson for future runs: keep the A→B gap well inside the
   executor time limit, or set the script policy limit accordingly.

## Redacted terminal output (passing run)

Phase A exit 0; Phase B exit 0 (captured via `$?`, not the pipe status).

```text
A0 SOL positions=[] ... active_orders=0 active_execs=[] marks={SOL 118.03, ...} mode=HEDGE
A1 entry: executor=HmwxBQKH… planned_qty=0.49 side=LONG status=reconciled
A1 watcher events: ['POSITION_OPENED', 'ORDER_CHANGED']
A2 HEDGE->0.30: qty=0.14 filled=0.14 side=SELL assessment=confirmed hedge_size 0->0.14 ratio=0.2857
B2 reader: structure=single_main hedge main=0.49 hedge=0.14 ratio=0.2857
B3 watcher events on resume poll: []
pm_load_context -> symbol=SOL-USDT positions=2 margin=SAFE
PM decision: action=REDUCE_HEDGE saved=1 published=1
B4 REDUCE->0.15: qty=0.06 filled=0.06 side=BUY assessment=confirmed short 0.14->0.08 venue_delta=0.06 ratio=0.1633
B5 correlation_dirs 1->1 main_executor unchanged new=[]
B6 REMOVE_HEDGE: assessment=confirmed filled=0.08 ... B6 CLOSE: quantity 0.49 submitted
final SOL=[] SOL_execs=[] BTC_same=True ETH_same=True
```
