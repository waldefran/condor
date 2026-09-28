# Independent Adversarial Review: Brooks E2E Integration & Real Restart Validation Plan

**Document Path:** `docs/brooks_review_findings.md`  
**Review Target:** Brooks Architecture & Integration (`feat/brooks-agents-v1` / `waldefran/brooks-review`)  
**Scope:** Waves 1–4, covering `condor/brooks/adapters.py`, `gm.py`, `execution.py`, `supervisor.py`, `pm.py`, `agent_runner.py`, `position_watcher.py`, `store.py`, `events.py`, `tests/brooks_e2e_harness.py`, `tests/test_brooks_e2e*.py`, and Wave 4 smoke evidence.  
**Review Mode:** Strictly read-only analysis of production codebase + reproducible live demo inspection.

---

## Executive Summary

An adversarial audit of the Brooks E2E implementation was conducted across all core modules, test suites, and live demo artifacts. While the architecture enforces strong structural boundaries (clear role separation, deterministic GM, immutable bus, and append-only stores), critical gaps exist at the venue interface, state reconciliation, and error recovery boundaries:

1. **Deadlock on Ambiguous/Partial Execution (P0):** When a hedge execution yields an ambiguous or partial result, the binding is transitioned to `status = "reconciliation_required"`. Because `BrooksGM` provides no hedge reconciliation method and all subsequent entry, management (`CLOSE`/`REDUCE`), and `reconcile_main` operations reject any status other than `submitted` or `reconciled`, the trade enters an irrevocable deadlock. Live venue positions cannot be closed or managed, creating severe capital risk.
2. **Missing Position ID on Perpetuals (P1):** The reconciliation and watcher layers assume venue positions carry an explicit `position_id` / `positionId`. On Binance Perpetual (and other perpetual venues via Hummingbot API), positions carry no ID. As a result, live reconciliation fails, watcher legs evaluate to zero quantity, and the system fails closed or misidentifies positions.
3. **Circular Assessment vs True Venue Delta (P1):** Fill assessment does not read actual execution fill receipts or order execution summaries; instead, it derives filled quantity from differences in position snapshots. When post-write reads experience latency or caching, the fill is assessed as zero, prematurely flagging the write as ambiguous and triggering the deadlock in (1).
4. **Smoke Evidence Discrepancy (P1):** The Wave 4 smoke evidence document (`docs/brooks_demo_smoke_evidence.md`) reports exit code 0 and asserts that all invariants held and zero positions were left open. However, raw execution logs (`/tmp/brooks_demo_smoke_run*.log`) reveal that runs 3, 4, and 5 failed with open positions and running executors left behind on `ETH-USDT` due to the hedge deadlock in (1).
5. **Prompt Privacy Leaks in Role Runners (P1):** The `agent_runner.py` privacy guard checks direct lowercase string equality against single-word denylist tokens, allowing compound keys (`position_id`, `unrealized_pnl`, `available_margin`) to leak into LLM prompts.

---

## Section 1: Detailed Findings by Review Scope

### Scope 1: `condor/brooks/adapters.py`

#### Finding 1.1: Trading Rules Schema Rejection on Live Venues
- **Location:** [`condor/brooks/adapters.py:492-542`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L492-L542)
- **Severity:** P1 (Critical - Blocks All Venue Writes)
- **Description:** In `HummingbotAccountReader._rules()`, the parser expects connector trading rules to be wrapped in keys `trading_rules`, `rules`, or `data`:
  ```python
  if isinstance(result, dict):
      for key in ("trading_rules", "rules", "data"):
          section = result.get(key)
  ```
  On the live Hummingbot API (`/connectors/binance_perpetual_demo/trading-rules`), the endpoint returns a bare pair-keyed mapping directly: `{"BTC-USDT": {...}}`. Because the candidate list does not inspect top-level pair keys, `amount_step` is unresolved. Furthermore:
  - The reader requires `max_leverage` (`max_leverage_raw < 1` raises `GMRejected`), but Binance Perpetual rules do not supply `max_leverage`.
  - The reader looks for `min_notional` under `("min_notional", "min_order_value", "min_cost", "min_quote_amount")`, but the venue provides `min_notional_size` (`min_order_value` is `0.0`).
- **Evidence & Reproduction:**
  Live query to `client.connectors.get_trading_rules("binance_perpetual_demo", ["SOL-USDT"])`:
  ```json
  {"SOL-USDT": {"min_order_size": 0.01, "min_base_amount_increment": 0.01, "min_notional_size": 5.0, "min_order_value": 0.0}}
  ```
  Calling `reader.read(account_name=..., connector_name="binance_perpetual_demo", symbol="BTC-USDT")` unconditionally raises `GMRejected: venue trading rules are incomplete`.

#### Finding 1.2: Venue Positions Without Explicit IDs Break Reconciler and Watcher
- **Location:** [`condor/brooks/adapters.py:341-352, 741-766, 834-865, 1157-1185`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L341-L352)
- **Severity:** P1 (Critical - Reconciliation and Watcher Failure)
- **Description:**
  `HummingbotPositionReconciler._venue_identity()` and `_hedge_venue_leg()` filter candidates strictly by:
  ```python
  position_id = str(row.get("position_id") or row.get("positionId") or row.get("id") or "")
  if not position_id:
      continue
  ```
  When the venue is Binance Perpetual (or any standard perpetual connector), rows in `trading.get_positions()` contain fields `trading_pair`, `position_side`, `amount`, `entry_price`, `unrealized_pnl`, `leverage`—but **no `position_id`**.
- **Impact:**
  1. `reconcile()` always returns `None`.
  2. `binding.main_position_id` remains `None`.
  3. In `HummingbotAccountReader.read()`, `by_id.get(main_id)` fails, marking `structure_status = "main_unresolved"` or `"main_binding_without_venue_position"`.
  4. In `build_watcher_provider()`, `_find_position()` returns `None`, and `_snapshot_leg()` evaluates to `{"id": "", "side": "", "qty": "0"}`. The watcher believes positions are closed even when open.
  5. PM context builder (`_pm_snapshot()`) aborts with `None` because `len(main_legs) != 1`.

#### Finding 1.3: Available Margin Defaults to Total Equity on Unsplit Balance Payloads
- **Location:** [`condor/brooks/adapters.py:483-487, 576-607`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L576-L607)
- **Severity:** P2 (High)
- **Description:** `_available_collateral_value()` inspects `available_units` or `available_balance`. If neither is reported by the venue row in `portfolio.get_state()`, it defaults to `value` (total equity for that asset).
- **Consequence:** If an exchange reports unified balances or does not populate `available_units` in `/portfolio/state`, available margin is reported as 100% of collateral, ignoring initial margin locked in active positions and unrealized losses. `_pm_margin_health()` fails to alert on margin compression.

#### Finding 1.4: Fail-Closed Breach in Open Orders Error Responses
- **Location:** [`condor/brooks/adapters.py:564-574`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L564-L574)
- **Severity:** P1 (High)
- **Description:**
  `_has_open_orders()` states: *"An unreadable order book is UNKNOWN, never empty: callers gate venue writes on this, so a failure must propagate as GMRejected rather than read as 'no open orders'"*.
  However, it only catches `Exception`. If `client.trading.get_active_orders()` returns an API error dict (e.g., `{"error": "Connector unavailable"}`, HTTP 200 with error payload, or `{"data": None}`), `_venue_rows(result)` returns `[]`.
  `bool(_venue_rows(result))` evaluates to `False`. The function returns `False` ("no open orders") instead of raising `GMRejected`.
- **Consequence:** Unreadable or erroring order books are treated as empty, allowing entry and hedge writes when active resting orders exist on the venue.

#### Finding 1.5: Candle History Fetch Depth Caps at 5 Bars on Live Venue
- **Location:** [`condor/brooks/adapters.py:122-129`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L122-L129)
- **Severity:** P1 (High)
- **Description:** `HummingbotCandleSource.fetch_candles()` invokes:
  ```python
  rows = await fetch_historical_candles(
      self._client, self._connector, symbol, canonical, start_time=None, limit=limit + 1
  )
  ```
  In `condor/fetchers/market_data.py:160-198`, when `start_time is None`, the historical query is skipped and falls back to `client.market_data.get_candles()`. On Binance Perpetual Demo, `get_candles` for H1 returns only 5 bars.
- **Consequence:** `ClosedBarGate.validate(raw, required_count=120)` raises `ClosedBarError: 4 closed bars; require 120`. Real Trader role execution cannot evaluate setups on live data and fails closed.

---

### Scope 2: `condor/brooks/gm.py` + `execution.py`

#### Finding 2.1: Irrevocable Deadlock on `reconciliation_required` (P0)
- **Location:** [`condor/brooks/gm.py:768-771, 795-801, 1222-1236, 486-489, 598-602`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/gm.py#L768-L771)
- **Severity:** P0 (Blocker - Unrecoverable Position Lock)
- **Description:**
  When a hedge execution returns an ambiguous assessment (e.g. execution timeout, network blip, or post-write venue read lag), `_execute_hedge()` executes:
  ```python
  record.update(status="reconciliation_required", assessment_status=assessment.status, ...)
  self._replace(record_path, record)
  binding["status"] = "reconciliation_required"
  self._replace(binding_path, binding)
  ```
  Once `binding["status"] == "reconciliation_required"`:
  1. `execute_hedge()` rejects immediately:
     ```python
     if binding.get("status") == "reconciliation_required":
         raise GMRejected("trade is in reconciliation_required state; reconcile before retry")
     ```
  2. `execute_management()` (which handles emergency `CLOSE` and `REDUCE`) rejects:
     ```python
     if binding.get("status") not in ("submitted", "reconciled") or not binding.get("main_executor_id"):
         raise GMRejected("MAIN execution is not confirmed")
     ```
  3. `reconcile_main()` rejects:
     ```python
     if binding.get("status") not in ("submitting", "submitted"):
         raise GMRejected("MAIN execution is not confirmed")
     ```
  4. `BrooksGM` contains **no `reconcile_hedge` method**.
  5. No consumer, supervisor loop, or event handler in the entire repository ever clears or resets `reconciliation_required` on a binding.
- **Evidence:** This was directly observed in smoke run 5 (`/tmp/brooks_demo_smoke_run5.log`): S4 failed with `hedge execution requires reconciliation (ambiguous)`. When S5 attempted emergency `CLOSE` of the smoke MAIN, it failed closed with `GMRejected: MAIN execution is not confirmed`. Active executors and real positions were left stranded on the exchange.

#### Finding 2.2: Circular Fill Assessment Derived from Venue Delta Without Execution Receipt Verification
- **Location:** [`condor/brooks/gm.py:1145-1158`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/gm.py#L1145-L1158)
- **Severity:** P1 (High)
- **Description:**
  `AccountSnapshot` never receives `filled_quantity` from `HummingbotAccountReader.read()`. Thus, `_execute_hedge` computes:
  ```python
  diff = abs(Decimal(reconciled_state.hedge_size) - Decimal(fresh_state.hedge_size))
  filled_qty_str = format(diff, "f")
  ```
  This logic is inherently circular: `filled_quantity` is computed from the position delta, which is then compared against the expected position delta in `assess_hedge_result()`.
  If the post-write `self.reader.read()` runs before the venue updates its position cache, `diff` is `0`, causing `assess_hedge_result()` to return `ambiguous ("success reported without a fill")`.
  Furthermore, if `outcome == "unknown"` (e.g. `client` timeout on `execute_hedge`), `filled_qty_str` is hardcoded to `"0"`, guaranteeing an ambiguous result and triggering the P0 deadlock in Finding 2.1 even if the order completely filled on the exchange.

#### Finding 2.3: `HummingbotExecutionPort` Missing In-Flight Executor Verification
- **Location:** [`condor/brooks/execution.py:140-175`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/execution.py#L140-L175)
- **Severity:** P2 (Medium)
- **Description:** In `execute_hedge()`, the port creates an order executor via `executor_create.create_order_executor`. If the executor is accepted by Hummingbot (`_accepted` extracts `executor_id`), but the order is subsequently rejected by the exchange or cancelled due to price collar, the port provides no status polling or terminal state verification.

---

### Scope 3: `condor/brooks/supervisor.py`

#### Finding 3.1: Shadow-Mode Hole in Management Intent Routing
- **Location:** [`condor/brooks/supervisor.py:297-298`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/supervisor.py#L297-L298), [`condor/brooks/pm.py:257-279`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/pm.py#L257-L279)
- **Severity:** P1 (High - Unintended Live Execution)
- **Description:**
  `GMConsumer` checks:
  ```python
  if event_type == EventType.MANAGEMENT_INTENT_CREATED.value:
      if bool(payload.get("shadow_mode", False)):
          return None
  ```
  However:
  1. `ManagementDecisionV2` in `condor/brooks/contracts.py` defines no `shadow_mode` field.
  2. `PositionManager._small_context()` explicitly filters fields and does not include `shadow_mode`.
  3. `PositionManager.handle_event()` publishes `decision.model_dump(mode="json")` as `payload`.
  Consequently, `payload.get("shadow_mode")` is always `None` / `False`. If a position was initiated or tracked under shadow mode, any downstream management decision (e.g. `CLOSE` or `HEDGE`) is routed directly to `gm.execute_management()` without the shadow guard engaging.

#### Finding 3.2: Market Analysis Tools Missing in GMConsumer Initialization
- **Location:** [`condor/brooks/supervisor.py:650-660`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/supervisor.py#L650-L660)
- **Severity:** P2 (Medium)
- **Description:** In `BrooksSupervisor._build_children()`, `GMConsumer` is instantiated without passing `tools`:
  ```python
  self._gm = GMConsumer(
      gm_factory=self._gm_factory,
      publish=self.events,
      market_analysis_runner=self._market_analysis_runner,
      agent_key=self._agent_key,
      candle_source=self._candle_source,
      store=self.store,
      user_id=self._user_id,
      now_fn=self._now_fn,
  )
  ```
  `self.tools` defaults to `None`. When `REQUEST_MARKET_ANALYSIS` triggers `async_execute_fresh_market_analysis_handshake(..., tools=self.tools)`, the tool boundary validator cannot verify operational tool isolation.

#### Finding 3.3: Split Directory Root Between GM and BrooksStore
- **Location:** [`condor/brooks/adapters.py:1697`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L1697), [`condor/brooks/store.py:46-49`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/store.py#L46-L49), [`condor/brooks/supervisor.py:750`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/supervisor.py#L750)
- **Severity:** P2 (Medium)
- **Description:**
  `wire_supervisor` passes `state_root=strategy_home` to `build_gm_factory`. The GM writes:
  - `<strategy_home>/trades/<correlation_id>/binding.json`
  - `<strategy_home>/trades/<correlation_id>/original_trade_intent.json`
  - `<strategy_home>/trades/<correlation_id>/hedge_state.json`
  - `<strategy_home>/trades/<correlation_id>/management/<decision_id>.json`
  Meanwhile, `BrooksStore` initializes `self.root = strategy_home / "brooks_state"`, writing:
  - `<strategy_home>/brooks_state/trades/<correlation_id>/latest_management_intent.json`
  - `<strategy_home>/brooks_state/trades/<correlation_id>/management_history.jsonl`
  While `_default_pm_list_active` was patched to scan `strategy_home / "trades"`, `store.read_trade_document(correlation_id, "hedge_state.json")` still reads from `brooks_state/trades` and returns `None`.

---

### Scope 4: `condor/brooks/pm.py` + `agent_runner.py`

#### Finding 4.1: Prompt Privacy Guard Bypassed by Compound/Snake-Case Keys
- **Location:** [`condor/brooks/agent_runner.py:143-173`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/agent_runner.py#L143-L173)
- **Severity:** P1 (High - Privacy Boundary Violation)
- **Description:**
  `run_role()` enforces privacy for `TRADER` and `HTF_ANALYST` by checking:
  ```python
  forbidden = {
      "account", "balance", "equity", "margin", "position", "positions",
      "pnl", "orders", "fills", "executor", "executors", "leverage", "hedge",
      "trade_history", "management_history"
  }
  if forbidden.intersection(str(key).lower() for key in value):
      raise ValueError(f"{role} prompt contains private account or position fields")
  ```
  The check uses direct set intersection against `str(key).lower()`. It does not tokenize snake_case, camelCase, or compound identifiers.
  Keys such as `position_id`, `position_side`, `unrealized_pnl`, `realized_pnl`, `available_margin`, `total_equity`, `account_name`, `open_orders`, or `hedge_ratio` **do not match** the denylist and pass completely unblocked.
- **Contrast:** `condor/brooks/market_analysis.py` implements `normalize_key()`, which decomposes identifiers into tokens and blocks all occurrences. `agent_runner.py` failed to incorporate this protection.

#### Finding 4.2: Unhandled Tool Exception Crashes LLM Role Invocation
- **Location:** [`condor/brooks/agent_runner.py:233-236`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/agent_runner.py#L233-L236)
- **Severity:** P2 (Medium)
- **Description:**
  In `run_role()`:
  ```python
  name, arguments = response["tool"], response["arguments"]
  result = market_tools[name](**arguments)
  if inspect.isawaitable(result):
      result = await result
  ```
  If the model supplies unexpected argument keys or types (e.g. `timeframe="1H"` instead of `"1h"`, or extraneous kwargs), python raises `TypeError` or `ValueError`.
  The tool execution call is not wrapped in a try/catch block. The exception aborts `run_role()` and crashes the role consumer, rather than feeding the error text back to the LLM turn to allow re-trying.

---

### Scope 5: `condor/brooks/position_watcher.py` + `store.py` + `events.py`

#### Finding 5.1: False `POSITION_OPENED` and `HEDGE_OPENED` Published on Restart
- **Location:** [`condor/brooks/position_watcher.py:128-142, 185-192`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/position_watcher.py#L128-L142), [`condor/brooks/supervisor.py:645-648`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/supervisor.py#L645-L648)
- **Severity:** P1 (High - Spurious State Transitions on Restart)
- **Description:**
  In `_transition()`, previous state defaults to zero:
  ```python
  old = before or {"main": {"qty": "0", "side": "", "id": ""}, "hedge": {"qty": "0", "side": "", "id": ""}, ...}
  was_open = Decimal(previous["qty"]) > 0
  is_open = Decimal(current["qty"]) > 0
  if not was_open and is_open:
      event_type = f"{prefix}_OPENED"
  ```
  When `BrooksSupervisor` initializes `PositionWatcher`, it passes no `initial_snapshots`.
  On process restart, `self._previous` is empty. The very first poll of the new process sees `was_open = False` and `is_open = True`, and publishes `POSITION_OPENED` and `HEDGE_OPENED` for existing, long-standing positions.
- **Evidence:** This bug is codified as an assertion in `tests/test_brooks_e2e_hedge.py:358`:
  ```python
  assert EventType.POSITION_OPENED in kinds and EventType.HEDGE_OPENED in kinds
  ```
  The test treats the premature transition publication as expected behavior instead of verifying that existing positions retain their open state across restarts.

#### Finding 5.2: Volatile Fills Cursor Lost on Process Restart
- **Location:** [`condor/brooks/position_watcher.py:152-162`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/position_watcher.py#L152-L162), [`condor/brooks/adapters.py:1059-1061`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/condor/brooks/adapters.py#L1059-L1061)
- **Severity:** P2 (Medium)
- **Description:** `fills_cursor` is stored exclusively in volatile memory within `PositionWatcher._previous`. It is never written to durable storage (such as `trades/<correlation_id>/cursor.json` or `brooks_state`). Upon process restart, `old["fills_cursor"]` is initialized to empty string. While the first poll suppresses the event because `before is None`, subsequent pagination may process duplicate fill events if the exchange search window overlaps.

---

### Scope 6: `tests/brooks_e2e_harness.py` + `tests/test_brooks_e2e*.py`

#### Finding 6.1: `SimPort` Replaces `HummingbotExecutionPort`, Masking API Serializer Bugs
- **Location:** [`tests/brooks_e2e_harness.py:191-325`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/tests/brooks_e2e_harness.py#L191-L325)
- **Severity:** P1 (High - Over-Mocking)
- **Description:**
  The E2E test harness does not instantiate `HummingbotExecutionPort`. Instead, it uses `SimPort`, a synthetic in-memory simulator that writes directly into `FakeVenue.positions`.
  The production `HummingbotExecutionPort` methods (`open_main`, `reduce_main`, `close_main`, `execute_hedge`) and their integrations with `mcp_servers/hummingbot_api/tools/executor_create.py` are **never executed** by the E2E test suite.
  Bugs in executor configuration assembly, parameter coercion, or client HTTP serialization cannot be caught by these tests.

#### Finding 6.2: Synthetic Position IDs in `FakeVenue` Masked Live Exchange Rejection
- **Location:** [`tests/brooks_e2e_harness.py:237-245, 317-325`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/tests/brooks_e2e_harness.py#L237-L245)
- **Severity:** P1 (High - Over-Mocking)
- **Description:**
  `FakeVenue.positions` explicitly supplies `"position_id": "pos-1"` and `"position_id": "hpos-1"`.
  Because the fake injected position IDs, all unit and E2E tests passed. In the live demo environment, Binance Perpetual provides no position IDs, causing immediate reconciliation and execution failures.

#### Finding 6.3: Discontinuous Test Scenarios Masked the Live Multi-Step Hedge Failure
- **Location:** [`tests/test_brooks_e2e_hedge.py:177-279`](file:///home/valdemaster/orca/workspaces/condor/brooks-review/tests/test_brooks_e2e_hedge.py#L177-L279)
- **Severity:** P1 (High - Test Scenario Gap)
- **Description:**
  The goal specification mandates verifying the full hedge lifecycle: `0 -> 0.30 -> 0.50 -> 0.20 -> 0`.
  In the E2E test suite, each step is tested in isolation by initializing a new `E2EWorld` and calling `seed_reconciled_hedge()` to fabricate the prior state.
  The test suite never ran `HEDGE` followed immediately by `INCREASE_HEDGE` within the same process context. This masked the fact that real multi-step executions hit `reconciliation_required` on the second step.

---

### Scope 7: Wave 4 Smoke Evidence Verification

#### Finding 7.1: Evidence Document Inconsistencies vs Raw Execution Logs
- **Location:** [`/home/valdemaster/orca/workspaces/condor/brooks-demo-smoke/docs/brooks_demo_smoke_evidence.md`](file:///home/valdemaster/orca/workspaces/condor/brooks-demo-smoke/docs/brooks_demo_smoke_evidence.md) vs [`/tmp/brooks_demo_smoke_run*.log`](file:///tmp/brooks_demo_smoke_run5.log)
- **Severity:** P1 (Critical Audit Discrepancy)
- **Comparison & Discrepancies:**
  1. **Claimed Exit & Final Venue State:**
     - *Evidence Document:* Claims run exit code **0**, asserting: *"Nothing opened -> nothing to close; final venue state identical to baseline (`baseline_match: True`); residual rows untouched"*.
     - *Actual Run Logs (Runs 3, 4, 5):* The script attempted live writes on `ETH-USDT`. In run 5, S2 opened `0.055` ETH LONG (`executor=HxyfAZCJx7SAy5B4UATqAH7soRg7r3NmiGYhKTm3RvPX`). S4 executed HEDGE 0.30 (`-0.016` SHORT) and then INCREASE_HEDGE (`-0.027` SHORT), but crashed into `reconciliation_required`. S5 failed to close the position. The final output in `run5.log` concludes:
       ```text
       BTC baseline match: True ETH flat + no smoke execs: False
       SMOKE RESULT: INCOMPLETE OR INVARIANT VIOLATION
       ```
       Active smoke executors and unclosed ETH positions remained live on the venue.
  2. **Trader Observation Result:**
     - *Evidence Document:* Reports S1 as `PASS`.
     - *Actual Run Logs (Runs 4 & 5):* S1 failed closed with `ClosedBarError: 119 closed bars; require 120`.
  3. **Omission of P0 Deadlock:**
     - The evidence document listed 3 gaps (rules shape, missing position ID, candle depth). It omitted the critical finding: when an ambiguous hedge execution occurs, the binding enters `reconciliation_required`, deadlocking the cleanup step (S5) and preventing position closure.

---

## Section 2: Real Restart Validation Plan (FINDING 13)

### 1. Objective & Invariants
Validate **FINDING 13** of the architecture specification against the live demo environment (`http://localhost:8000`, `master_account`, `binance_perpetual_demo`):
- **Process A:** Opens small MAIN + HEDGE positions (fixture-marked), verifies durable persistence, and halts.
- **Process B:** A completely fresh OS process starts, reads the durable store from disk, queries the live venue, reconciles both MAIN and HEDGE legs, verifies PositionWatcher recognition, wakes the PM on timer/event, executes `REDUCE_HEDGE`, and cleanly exits.
- **Mandatory Invariants:**
  1. **Zero Accidental New Trades:** Process B must never create an unintended new MAIN position or order.
  2. **Isolated Symbol:** To prevent cross-talk with previous test residuals (`BTC-USDT`) and concurrent smoke runners (`ETH-USDT`), use **`SOL-USDT`**.
  3. **Strict Fail-Closed:** Any read ambiguity, rule mismatch, or reconciliation failure immediately halts execution with zero retries.
  4. **Guaranteed Cleanup:** All positions opened during the run must be fully closed before completion.

---

### 2. Operational Specification

```
+--------------------------------------------------------------------------------+
|                                 PROCESS A                                      |
|                                                                                |
|  [Step 1] S0: Read Baseline (SOL-USDT must be flat)                            |
|       |                                                                        |
|  [Step 2] S1: Controlled MAIN Entry (0.05 SOL LONG, tagged "demo-restart-<ts>")|
|       |   - Verify binding.json & original_trade_intent.json persisted         |
|       |                                                                        |
|  [Step 3] S2: Open First HEDGE (Target 0.30 -> 0.015 SOL SHORT)                |
|       |   - Verify hedge_state.json persisted                                  |
|       |   - Flush store, release locks, terminate Process A                    |
+--------------------------------------------------------------------------------+
                                       |
                     [Step 4] Process Boundary (kill / exit)
                                       |
+--------------------------------------------------------------------------------+
|                                 PROCESS B                                      |
|                                                                                |
|  [Step 5] S3: Fresh Initialization                                             |
|       |   - Fresh Python process, fresh BrooksSupervisor, same strategy_home   |
|       |   - Read durable bindings from disk                                    |
|       |                                                                        |
|  [Step 6] S4: Venue Re-Query & State Reconciliation                            |
|       |   - Query live venue (/trading/positions, /executors)                  |
|       |   - AccountReader reconciles MAIN (pos_id) and HEDGE (pos_id)          |
|       |   - Verify structure_status == "ok"                                    |
|       |                                                                        |
|  [Step 7] S5: PositionWatcher State Verification                               |
|       |   - First poll recognizes resumed state                                |
|       |   - Invariant: No duplicate trade intent triggered                     |
|       |                                                                        |
|  [Step 8] S6: PM Wake & REDUCE_HEDGE Execution                                 |
|       |   - PM wakes on PM_TIMER / event                                       |
|       |   - PM reads production context (HedgeState restored)                  |
|       |   - PM emits REDUCE_HEDGE (target 0.15)                                |
|       |   - GM compiles delta (0.0075 SOL CLOSE), executes via port            |
|       |   - Venue confirms hedge size reduced                                  |
|       |                                                                        |
|  [Step 9] S7: Accidental Trade Invariant Audit                                 |
|       |   - Verify zero new MAIN executors, zero new trade bindings            |
|       |                                                                        |
| [Step 10] S8: Guaranteed Cleanup                                               |
|           - CLOSE remaining HEDGE leg (REMOVE_HEDGE -> 0)                      |
|           - CLOSE MAIN position (GM management CLOSE)                          |
|           - Confirm SOL-USDT is completely flat                                |
+--------------------------------------------------------------------------------+
```

---

### 3. Step-by-Step Execution & Evidence Capture

#### Step 0: Baseline Verification (Pre-Flight)
- **Actions:** Query `GET /trading/positions` and `GET /trading/orders/active` filtered for `SOL-USDT`.
- **Invariants:**
  - `positions` for `SOL-USDT` must be empty (`[]`).
  - Active orders for `SOL-USDT` must be empty (`[]`).
  - Account margin mode confirmed as `HEDGE`.
- **Evidence Captured:** Snapshot JSON containing balances, mark price, and empty position list.

#### Step 1: Process A - Controlled MAIN Entry
- **Actions:**
  - Initialize `BrooksGM` under `strategy_home = /tmp/brooks-restart-val-<ts>`.
  - Issue fixture `TradeIntentV2`:
    ```json
    {
      "schema": "brooks.trade-intent.v2",
      "role": "TRADER",
      "symbol": "SOL-USDT",
      "decision": "ENTER_LONG",
      "trigger": {"price": "120.0"},
      "invalidation": {"price": "116.0"},
      "fixture": {"smoke": true, "restart_test": true}
    }
    ```
  - Call `gm.execute_entry()` sized for minimum venue notional (e.g. 0.05 SOL ≈ $6.00).
- **Evidence Captured:**
  - `trades/<cid>/original_trade_intent.json`
  - `trades/<cid>/binding.json` (`status = "submitted"` -> `"reconciled"`)
  - Executor ID from `create_position_executor`
  - Venue position confirming `+0.05` LONG.

#### Step 2: Process A - First HEDGE Open
- **Actions:**
  - Construct `ManagementDecisionV2` with action `HEDGE`, `target_hedge_ratio = "0.30"`.
  - Call `gm.execute_management()`.
  - Confirm execution port submits `SELL OPEN` for `0.015` SOL.
  - Corroborate post-write venue read until fill is confirmed.
- **Evidence Captured:**
  - `trades/<cid>/hedge_state.json` showing `main_size = "0.05"`, `hedge_size = "0.015"`, `hedge_ratio = "0.30"`.
  - `trades/<cid>/management/<decision_id>.json` (`status = "submitted"`, `assessment = "confirmed"`).
  - Venue position confirming `-0.015` SHORT.

#### Step 3: Halt Process A
- **Actions:**
  - Flush all store writes and release OS locks (`fcntl.flock`).
  - Explicitly terminate Process A.
- **Evidence Captured:**
  - Directory listing of `/tmp/brooks-restart-val-<ts>/trades/<cid>/`.
  - SHA256 checksums of `binding.json`, `original_trade_intent.json`, `hedge_state.json`.

#### Step 4: Launch Process B (Fresh Process)
- **Actions:**
  - Spawn an independent Python process pointing to the same `strategy_home`.
  - Instantiate fresh `HummingbotAccountReader`, `BrooksGM`, `PositionWatcher`, and `PositionManager`.
- **Verification:**
  - Call `read_bindings()`: Confirms binding exists on disk for `SOL-USDT` with `status = "reconciled"`.
  - Re-read `hedge_state.json`: Confirms prior ratio `0.30`.

#### Step 5: Process B - Venue Re-Query & State Reconstruction
- **Actions:**
  - Process B calls `reader.read(account_name=..., connector_name=..., symbol="SOL-USDT")`.
  - Verify reconciler resolves both the MAIN leg (`+0.05` LONG) and HEDGE leg (`-0.015` SHORT) through executor lineage.
- **Fail-Closed Gate:** If `structure_status != "ok"`, Process B aborts immediately without attempting writes.
- **Evidence Captured:**
  - Reconciled `AccountSnapshot` showing both legs bound with correct roles.

#### Step 6: Process B - PositionWatcher Recognition
- **Actions:**
  - Seed `PositionWatcher` with reconstructed snapshot.
  - Trigger `watcher.poll()`.
- **Invariants:**
  - Must **not** emit spurious `POSITION_CLOSED` or `RECONCILIATION_REQUIRED`.
  - Any wake events emitted must route to PM, not Trader.
- **Evidence Captured:**
  - Emitted event log from Process B watcher poll.

#### Step 7: Process B - PM Wake & REDUCE_HEDGE Execution
- **Actions:**
  - Execute `pm.handle_event(PM_TIMER)`.
  - Verify `pm_load_context()` restores complete thesis and state:
    - `original_trade_intent` matches Step 1.
    - `hedge_state` matches Step 2 (`0.30` ratio).
    - `margin_health` evaluates to `SAFE`.
  - PM issues `REDUCE_HEDGE` with `target_hedge_ratio = "0.15"`.
  - `gm.execute_management()` compiles delta: `0.015 - (0.05 * 0.15) = 0.0075` SOL `BUY CLOSE`.
  - Verify execution port sends `BUY CLOSE` for `0.0075` SOL.
- **Evidence Captured:**
  - Process B management record showing `action = "REDUCE_HEDGE"`, `quantity = "0.0075"`, `assessment = "confirmed"`.
  - Updated `hedge_state.json` reflecting `hedge_size = "0.0075"`, `hedge_ratio = "0.15"`.
  - Venue position confirming SHORT reduced to `-0.0075`.

#### Step 8: Accidental Trade Audit
- **Actions:**
  - Scan `/tmp/brooks-restart-val-<ts>/trades/`.
  - Query venue active executors and orders for `SOL-USDT`.
- **Invariants:**
  - Exactly one correlation directory exists (no new trade created).
  - Number of MAIN entry executors created across entire run is exactly 1.
- **Evidence Captured:**
  - File tree audit and venue executor query showing zero extraneous writes.

#### Step 9: Guaranteed Cleanup
- **Actions:**
  - Issue `REMOVE_HEDGE` (`target = "0"`): Closes remaining `0.0075` SOL SHORT.
  - Issue management `CLOSE`: Closes `0.05` SOL MAIN LONG.
  - Query venue to confirm `positions` and `orders` for `SOL-USDT` are completely flat (`[]`).
- **Evidence Captured:**
  - Final venue snapshot JSON matching Step 0 baseline.

---

## Section 3: Summary Matrix of Review Findings

| ID | Module / File | Severity | Flaw Category | Summary |
|---|---|---|---|---|
| **F1.1** | `condor/brooks/adapters.py:492` | **P1** | Venue Incompatibility | Missing support for bare dict trading rules, `min_notional_size`, and missing `max_leverage` on Binance Perp. |
| **F1.2** | `condor/brooks/adapters.py:834` | **P1** | Identity Architecture | Assumes venue positions carry explicit IDs; Binance Perp rows have no IDs, breaking reconciliation. |
| **F1.3** | `condor/brooks/adapters.py:576` | **P2** | Accounting / Margin | Available margin defaults to 100% of stable equity if venue does not provide `available_units` split. |
| **F1.4** | `condor/brooks/adapters.py:564` | **P1** | Fail-Closed Breach | Open orders error payload evaluates to empty list, reporting zero open orders instead of raising `GMRejected`. |
| **F1.5** | `condor/brooks/adapters.py:122` | **P1** | Market Data | Candle source skips range fetch when `start_time=None`, capping H1 at 5 bars and causing `INSUFFICIENT_HISTORY`. |
| **F2.1** | `condor/brooks/gm.py:1230` | **P0** | Wedge Deadlock | Ambiguous/partial hedge marks binding as `reconciliation_required`, which blocks all management, entries, and cleanup forever. |
| **F2.2** | `condor/brooks/gm.py:1150` | **P1** | Assessment | Fill quantity is calculated circularly from position delta instead of execution receipts, failing on read lag. |
| **F2.3** | `condor/brooks/execution.py:140`| **P2** | Execution Port | Lacks in-flight order monitoring and post-submit status polling. |
| **F3.1** | `condor/brooks/supervisor.py:297`| **P1** | Single-Writer / Guard | Shadow mode flag is omitted from `ManagementDecisionV2`, causing shadow positions to emit live venue writes. |
| **F3.2** | `condor/brooks/supervisor.py:650`| **P2** | Supervisor Wiring | Market analysis tools not passed to `GMConsumer`, degrading handshake boundary validation. |
| **F3.3** | `condor/brooks/supervisor.py:750`| **P2** | File System Root | Discrepancy between GM state root (`strategy_home/trades`) and BrooksStore root (`brooks_state/trades`). |
| **F4.1** | `condor/brooks/agent_runner.py:143`| **P1** | Security / Leak | Privacy guard only matches exact lowercase single words, leaking compound keys (`position_id`, `unrealized_pnl`). |
| **F4.2** | `condor/brooks/agent_runner.py:233`| **P2** | Robustness | Tool call argument unpacking lacks try/except, crashing role runner on extraneous parameters. |
| **F5.1** | `condor/brooks/position_watcher.py:128`| **P1** | State Transition | Position watcher emits spurious `POSITION_OPENED` / `HEDGE_OPENED` on restart due to empty initial state. |
| **F5.2** | `condor/brooks/position_watcher.py:152`| **P2** | Cursor Durability | Fills cursor is held in volatile memory and lost across process restarts. |
| **F6.1** | `tests/brooks_e2e_harness.py:191` | **P1** | Over-Mocking | `SimPort` replaces `HummingbotExecutionPort`, leaving real execution port and serializer paths untested. |
| **F6.2** | `tests/brooks_e2e_harness.py:237` | **P1** | Over-Mocking | `FakeVenue` injects synthetic position IDs, concealing the exchange position ID incompatibility. |
| **F6.3** | `tests/brooks_e2e_harness.py:166` | **P2** | Test Harness | Injects idealized trading rules shape rather than live venue dictionary format. |
| **F6.4** | `tests/test_brooks_e2e_hedge.py:358`| **P1** | Test Weakness | Asserts restart transition bug (`POSITION_OPENED` on existing position) as expected behavior. |
| **F7.1** | `docs/brooks_demo_smoke_evidence.md`| **P1** | Evidence Integrity | Evidence doc claims exit code 0 and all invariants passed, but raw logs reveal runs 3-5 failed with open positions and running executors. |

---
*Report completed independently under task `task_6b3e933152ba` / dispatch `ctx_42b084252876`.*
