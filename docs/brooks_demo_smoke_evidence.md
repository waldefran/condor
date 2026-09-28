# Brooks DEMO smoke — evidence (Wave 4, full lifecycle PASS)

Runner: `scripts/brooks_demo_smoke.py` (production Brooks classes only, demo-only
writes, fixtures marked `demo-smoke-<ts>` + `{"smoke": true}`).
Passing run: 2026-09-28 ~01:49–02:05 UTC, exit code **0**, correlation
`demo-smoke-1790570941`. Command (from repo root, shared venv):

```sh
PYTHONPATH=/home/valdemaster/orca/workspaces/condor/brooks-demo-smoke \
  /home/valdemaster/brooks-condor/condor/.venv/bin/python scripts/brooks_demo_smoke.py
```

Venue: `http://localhost:8000` server `local`, account `master_account`, connector
`binance_perpetual_demo` (demo-fapi.binance.com — DEMO, no real funds).
Credentials read from the Condor config; never printed or committed.
Controller id `brooks-demo-smoke`; state root under `/tmp` (no repo pollution).
Executor/position ids below are truncated to 8 chars; no private data.

## Results (all PASS, real demo writes)

| Step | Result | Observed |
|------|--------|----------|
| S0 baseline | recorded | BTC-USDT residuals untouched (SHORT −0.0007 @83843.7, LONG +0.0007 @84379.9, SHORT −0.0007 @83843.7 — none with venue ids); ETH-USDT flat; active orders 0; active executors 0; marks BTC 83465.8 / ETH 2654.99; mode HEDGE |
| S1 trader observation | **PASS** | Real Trader via `run_role` (`opencode-go:deepseek-v4.1-flash`) on live closed bars (bounded 30-bar H4/H1/M15 seeds + full 120-bar depth via read tools, decision-time-frozen source): **ENTER_SHORT / breakout / medium** — H4 range-break below ~83700, H1 bear channel, M15 uninterrupted bearish structure. Recorded only; fixtures (not signals) drive writes |
| S2 controlled MAIN | **PASS** | Fixture ENTER_LONG (smoke-marked, trigger = live ETH mark, 2% stop, risk 0.03% → 0.059 ETH ≈ $157) → MAIN executor opened → binding reconciled with executor-derived id (`executor:<id>`) → watcher poll emitted **POSITION_OPENED** (+ORDER_CHANGED) |
| S3 PM path | **PASS, zero writes** | `pm_load_context` → real snapshot (SAFE); scripted HOLD via production `PositionManager.handle_event` → decision saved + published; GM `HOLD` → `no_write`; active executor count unchanged |
| S4 hedge lifecycle | **PASS, exact deltas, all confirmed first attempt** | HEDGE 0.30 → SELL OPEN 0.017, hedge 0→0.017, ratio 0.2881; INCREASE 0.50 → SELL OPEN 0.012, 0.017→0.029, ratio 0.4915; REDUCE 0.20 → BUY CLOSE 0.017, 0.029→0.012, ratio 0.2034; REMOVE → BUY CLOSE 0.012, 0.012→0. Ratios within quantum of targets; every step `assessment=confirmed`, `filled == requested == venue delta` |
| S5 cleanup | **PASS** | Management CLOSE of the smoke MAIN (0.059, submitted); final: BTC rows byte-identical to baseline, ETH flat, zero active smoke executors (verified independently after the run) |

## Production gaps found and fixed (were blocking S2/S4)

1. **Trading-rules shape** (`adapters._rules`): live endpoint serves a bare
   pair-keyed map with the floor spelled `min_notional_size` and no
   `max_leverage`. Reader now accepts the bare map + both spellings; a missing
   max is `None` (policy leverage sizes margin-checked, venue validates the
   write) instead of a rejection. `gm.py` guards are `None`-aware.
2. **Candle depth** (`HummingbotCandleSource`): now requests an explicit
   `[start, end]` range per timeframe so the 120-bar profile is satisfiable
   (`get_candles` alone caps H1 at ~5 bars). Nothing fabricated; short history
   still fails closed in `ClosedBarGate`.
3. **No venue position ids / empty lineage** (reader, reconciler, watcher):
   bindings resolve through the confirmed executor as `executor:<id>`
   (exact id + scope match, single side-consistent row; side stays a
   consistency check). Watcher stamps the tag so POSITION_OPENED fires.
4. **Hedge fingerprint included marks** (`hedge.py`): every mark tick tripped
   "stale". Fingerprint is now structure-only (ids/sides/quantities/roles).
5. **Hedge post-write ambiguity wedged with no recovery** (`gm.py`): bounded
   read-only corroboration (6×10s) confirms when later reads corroborate
   requested==landed; persistent ambiguity still wedges and blocks later
   writes. Fresh pre-write reads retry on unresolved structure only.
6. **INCREASE rotated `hedge_executor_id`** (`gm.py`): the tag pointed at the
   creator leg while the id pointed at the latest writer, wedging every later
   step. The binding now keeps the creator; writers live in records.
7. **Direct executor reads** (`adapters._confirmed_executor`): confirmation
   prefers `get_executor(id)` + scope check over the minutes-lagging search
   index (fail-closed on scope mismatch).

Venue quirks documented (no code changes): position/fill reads race writes
for ~a minute (splits, phantom barrier fills, lagging fills index); settled
reads are exact. The smoke paces steps on raw-row convergence; the GM bounds
all re-reads.

S1 note: the model emits valid contracts unreliably on huge prompts (empty /
invalid JSON most attempts over several runs; 30-bar bounded seeds + 120-bar
tools succeed reliably). Two valid real observations are on record (NO_TRADE
and ENTER_SHORT); failures are recorded fail-closed, never forced.

## Redacted terminal output (passing run, warnings stripped)

```text
Brooks DEMO smoke (production classes, demo-only writes, fixtures marked smoke)
correlation=demo-smoke-1790570941 state_root=/tmp/brooks-demo-smoke-1790570941 controller=brooks-demo-smoke
venue=http://localhost:8000 server=local account=master_account connector=binance_perpetual_demo (credentials redacted)

===== S0: BASELINE (own read; unrelated state is never touched) =====
BTC positions=[SHORT -0.0007 @83843.7, LONG +0.0007 @84379.9, SHORT -0.0007 @83843.7] ETH positions=[] active_orders=0 active_execs=[] marks={BTC 83465.8, ETH 2654.99} mode=HEDGE
balances=[BTC 0.01, USDT ~4676.9, USDC 5000.0]

===== S1: REAL TRADER OBSERVATION (live demo market data, production classes) =====
H1 tail closed bars (decision_time from live source); H4/H1/M15 30-bar seeds; TRADER decision: ENTER_SHORT mechanism=breakout confidence=medium
S1 result: PASS
GM policy: risk=0.03% lev=5 smoke symbol=ETH-USDT (BTC residuals untouched)

===== S2: CONTROLLED DEMO MAIN (fixture marked smoke, demo only) =====
S2 entry: executor=8agtd1Yw… planned_qty=0.059 side=LONG status=reconciled
S2 reconciled: main_position_id=executor:8agtd1Yw… status=reconciled
S2 watcher events: ['POSITION_OPENED', 'ORDER_CHANGED']
S2 result: PASS

===== S3: PM PATH (production context + HOLD fixture -> ZERO writes) =====
pm_load_context -> snapshot symbol=ETH-USDT positions=1 margin=SAFE
PM decision: action=HOLD saved=1 published=1 GM HOLD={'action': 'HOLD', 'status': 'no_write'} active_execs=1
S3 result: PASS

===== S4: HEDGE LIFECYCLE (full decision payloads incl. hedge_plan) =====
S4 HEDGE->0.30: qty=0.017 filled=0.017 side=SELL assessment=confirmed hedge_size 0->0.017 ratio=0.2881 venue=[LONG 0.059, SHORT -0.017]
S4 INCREASE_HEDGE->0.50: qty=0.012 filled=0.012 side=SELL assessment=confirmed hedge_size 0.017->0.029 ratio=0.4915 venue=[LONG 0.059, SHORT -0.029]
S4 REDUCE_HEDGE->0.20: qty=0.017 filled=0.017 side=BUY assessment=confirmed hedge_size 0.029->0.012 ratio=0.2034 venue=[LONG 0.059, SHORT -0.012]
S4 REMOVE_HEDGE->0: qty=0.012 filled=0.012 side=BUY assessment=confirmed hedge_size 0.012->0 ratio=0 venue=[LONG 0.059]
S4 result: PASS

===== S5: CLEANUP (management CLOSE of the smoke MAIN) =====
S5 CLOSE record: submitted, quantity 0.059
final BTC=[SHORT -0.0007 @83843.7, LONG +0.0007 @84379.9, SHORT -0.0007 @83843.7] (identical) ETH=[] active_orders=0 active_execs=[]
BTC baseline match: True ETH flat + no smoke execs: True

===== SUMMARY =====
S1: PASS  S2: PASS  S3: PASS  S4: PASS  S5: PASS
SMOKE RESULT: done; full lifecycle on demo with real writes, venue returned to baseline
```

(Earlier partial runs and their fail-closed aborts are superseded by this run;
intermediate findings — rules/candle gaps, fingerprint, wedge recovery,
creator binding, venue races — are fixed above with regression tests.)
