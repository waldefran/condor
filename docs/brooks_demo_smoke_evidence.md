# Brooks DEMO smoke — evidence (Wave 4)

Runner: `scripts/brooks_demo_smoke.py` (production Brooks classes only, demo-only
writes, fixtures marked `demo-smoke-<ts>` + `{"smoke": true}`).
Run: 2026-09-28 ~01:40 UTC, exit code **0**. Command (from repo root):

```sh
PYTHONPATH=/home/valdemaster/orca/workspaces/condor/brooks-demo-smoke \
  /home/valdemaster/brooks-condor/condor/.venv/bin/python scripts/brooks_demo_smoke.py
```

Venue: `http://localhost:8000` server `local`, account `master_account`, connector
`binance_perpetual_demo` (demo-fapi.binance.com — DEMO, no real funds).
Credentials read from the Condor config; never printed or committed.
Controller id `brooks-demo-smoke`, state root `/tmp/brooks-demo-smoke-1790558417`
(no repo pollution; no bindings persisted).

## Results

| Step | Result | Observed |
|------|--------|----------|
| S0 baseline | recorded | 3 residual BTC-USDT rows (SHORT −0.0007 @83843.7, LONG +0.0007 @84379.9, SHORT −0.0007 @83843.7), **none carries a venue position id**; active orders 0; executors 0; mark ≈83901; mode HEDGE; BTC 0.01 / USDT 4677.81 / USDC 5000 |
| S1 trader observation | **PASS** | 4 real closed H1 bars via `HummingbotCandleSource`; real Trader via `run_role` (`opencode-go:deepseek-v4.1-flash`) returned **NO_TRADE, confidence high** — H1 depth (4 bars) and M15 depth (119) below the production 120-bar profile (`INSUFFICIENT_HISTORY`), so no reproducible entry case. First model turn returned empty text (26 JSONL events, transient); one read-only retry succeeded |
| S2 controlled MAIN | **FAIL-CLOSED, zero writes** | `GMRejected: venue trading rules are incomplete`; executors 0→0, no smoke bindings |
| S3 PM path | **PASS, zero writes** | `pm_load_context` → `None`; `PositionManager.handle_event(PM_TIMER)` → `None` (idle, no model call); GM `HOLD` → `{"status": "no_write"}`; saved=0 published=0 executors=0 |
| S4 hedge lifecycle | **FAIL-CLOSED ×4, zero writes** | HEDGE 0.30 / INCREASE 0.50 / REDUCE 0.20 / REMOVE 0.0 each `GMRejected: MAIN binding is missing` (full `ManagementDecisionV2`+`hedge_plan` payloads validated first); executors 0 |
| S5 cleanup | **PASS** | Nothing opened → nothing to close; final venue state identical to baseline (`baseline_match: True`); residual rows untouched |

## Production gaps found (reported, not patched — code frozen)

1. **Trading-rules shape** (`condor/brooks/adapters.py::_rules`): live
   `GET /connectors/{c}/trading-rules` returns a bare pair-keyed map
   (`{"BTC-USDT": {...}}`), while the reader only looks under
   `trading_rules`/`rules`/`data` keys — so `amount_step` is never found.
   Additionally the per-pair rules carry **no `max_leverage` key** and spell
   the notional floor **`min_notional_size`** (reader expects `min_notional`
   et al). Any one of the three is fatal → `execute_entry`/management always
   fail closed on this venue. Reproduced: `HummingbotAccountReader.read`
   → `GMRejected: venue trading rules are incomplete`.
2. **Venue positions carry no id** (`trading.get_positions` rows have
   `account_name/connector_name/trading_pair/side/amount/entry_price/
   unrealized_pnl/leverage` — no `position_id`/`positionId`/`id`), so
   `HummingbotPositionReconciler` can never bind `main_position_id`
   (exactly-one explicit id required). Residual BTC rows are also
   **unbound**, which would independently gate any BTC-USDT entry
   (`unbound_venue_positions`).
3. **H1 history depth**: `market_data.get_candles(..., "1h", N)` caps at
   **5 bars** regardless of limit, so the production Trader profile
   (120 H1 bars) cannot run on this venue — the model correctly NO_TRADEd
   on `INSUFFICIENT_HISTORY`. (H4/M15 serve 120+.)

## Redacted terminal output (pydantic `schema` UserWarnings stripped)

```text
Brooks DEMO smoke (production classes, demo-only writes, fixtures marked smoke)
correlation=demo-smoke-1790558417 state_root=/tmp/brooks-demo-smoke-1790558417 controller=brooks-demo-smoke
venue=http://localhost:8000 server=local account=master_account connector=binance_perpetual_demo (credentials redacted)

===== S0: BASELINE (own read; unrelated state is never touched) =====
positions=[{"trading_pair": "BTC-USDT", "side": "SHORT", "amount": "-0.0007", "entry_price": "83843.7", "has_venue_position_id": false}, {"trading_pair": "BTC-USDT", "side": "LONG", "amount": "0.0007", "entry_price": "84379.90000000001", "has_venue_position_id": false}, {"trading_pair": "BTC-USDT", "side": "SHORT", "amount": "-0.0007", "entry_price": "83843.7", "has_venue_position_id": false}] active_orders=0 executors=0 mark=83901.1 mode=HEDGE
balances=[{"token": "BTC", "units": "0.01", "value": "838.85"}, {"token": "USDT", "units": "4677.81465336", "value": "4677.81465336"}, {"token": "USDC", "units": "5000.0", "value": "5000.0"}]

===== S1: REAL TRADER OBSERVATION (live demo market data, production classes) =====
H1 closed bars: n=4 last_close_ms=1790557199999
last closed bar: {"close": "84238.9", "close_time_ms": 1790557199999, "closed": true, "high": "84799.1", "low": "84238.8", "open": "84454.4", "open_time_ms": 1790553600000, "volume": "85087.7046"}
S1 attempt 1 no-text/transient (RuntimeError); retrying once
TRADER decision: NO_TRADE mechanism=none confidence=high
evidence_for=['The production profile requires 120 closed bars on H4, H1 and M15. At decision_time_ms 1790557199999 the H1 window returned only 4 closed bars (open_time_ms 1790542800000 through close_time_ms 1790557199999), and the closed-bar gate rejected the required depth with INSUFFICIENT_HISTORY (4 closed bars; require 120).', 'The M15 window also failed completeness at the decision point (119 closed bars; require 120), so the setup and trigger timeframe cannot be evaluated to the required depth either.', 'With the H1 active-leg window and the M15 setup window incomplete, cross-timeframe alignment, signal-bar quality, actionable trigger, and structural invalidation cannot be established, so no reproducible entry case exists.'] evidence_against=['H4 did supply a complete 120-bar window (last 20 bars ranged 83495.4-85299, last close 84433), so broad structure is observable; a H4-only read places price near the middle of that range rather than at a clean extreme.', 'The partial M15 sample shows a fast two-way swing up to 84799.1 and back down to 84238.9, which could tempt a reversal hypothesis, but it lacks the required 120-bar depth and confirmed follow-through to qualify as a signal.']
S1 result: PASS
GM policy: risk=1% lev=5 (venue max unknown; reader must confirm)

===== S2: CONTROLLED DEMO MAIN (fixture marked smoke, demo only) =====
S2 FAIL-CLOSED (zero writes): GMRejected: venue trading rules are incomplete
S2 safety: executors before=0 after=0 smoke_bindings=[]
S2 result: FAIL-CLOSED

===== S3: PM PATH (production context + HOLD fixture -> ZERO writes) =====
pm_load_context('demo-smoke-1790558417') -> None (fail-closed, no binding)
PositionManager.handle_event -> None (None = stayed idle, no model call)
GM HOLD record: {'action': 'HOLD', 'status': 'no_write'} saved=0 published=0 executors=0

===== S4: HEDGE LIFECYCLE (full decision payloads incl. hedge_plan) =====
S4 HEDGE->0.30: FAIL-CLOSED (zero writes): GMRejected: MAIN binding is missing
S4 INCREASE_HEDGE->0.50: FAIL-CLOSED (zero writes): GMRejected: MAIN binding is missing
S4 REDUCE_HEDGE->0.20: FAIL-CLOSED (zero writes): GMRejected: MAIN binding is missing
S4 REMOVE_HEDGE->0: FAIL-CLOSED (zero writes): GMRejected: MAIN binding is missing
S4 result: FAIL-CLOSED executors=0

===== S5: CLEANUP (management CLOSE of the smoke MAIN) =====
S5: no smoke MAIN was ever opened (S2 fail-closed) -> nothing to close; verifying baseline untouched instead
final positions=[{"trading_pair": "BTC-USDT", "side": "SHORT", "amount": "-0.0007", "entry_price": "83843.7", "has_venue_position_id": false}, {"trading_pair": "BTC-USDT", "side": "LONG", "amount": "0.0007", "entry_price": "84379.90000000001", "has_venue_position_id": false}, {"trading_pair": "BTC-USDT", "side": "SHORT", "amount": "-0.0007", "entry_price": "83843.7", "has_venue_position_id": false}] active_orders=0 executors=0
baseline match (positions+orders): True smoke_bindings=[]

===== SUMMARY: per-step results (PASS = observed, FAIL-CLOSED = safe abort) =====
S1: PASS
S2: FAIL-CLOSED
S3: PASS
S4: FAIL-CLOSED
S5: PASS
SMOKE RESULT: done; all safety invariants hold (zero smoke executors, zero smoke bindings, HOLD wrote nothing)
```