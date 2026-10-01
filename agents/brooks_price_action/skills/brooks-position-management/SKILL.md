---
name: brooks-position-management
description: Manage existing exposure with Al Brooks price action and Condor account policy. Use to assess whether to hold, reduce, close, hedge, remove a hedge, reconcile, or request market analysis. Do not use for new entries, standalone market forecasting, or portfolio allocation.
license: MIT
metadata:
  author: Skill-Brooks
---

# Al Brooks Position Management

## Operation

Use Al Brooks price-action reasoning to manage existing exposure together with
Condor's operational, account, and protection rules. Aim to maximize the
existing operation's net profit: preserve favorable exposure when current
structure supports it, protect open gains, and contain deterioration through
admissible partial or full hedges. A hedge is available at any management wake
when policy permits; reaching 5R is never a prerequisite. Compare the available
actions using current closed price action, exposure, and operation-level net.
Costs must be accounted for, but alone must not veto a supported hedge.
Return one structured management decision; do not open a fresh trade, place an
order, or forecast a future price.

## Current contract

The live input is `brooks.position-management-input.v2`; every live response
must be `brooks.management-decision.v2` as specified below. Any V1 material is
historical replay context only, never a live output template or fallback.

## Al Brooks price-action lens

Use the original trade intent and supplied Trader analysis when available to
identify the existing premise and its stated structural invalidation. Retrieve
the intent with `get_original_trade_intent` when needed. Do not invent a
premise, entry, stop, target, or size. When price evidence matters,
relate closed OHLC bars to that premise: trend or trading range, breakout or
channel, swing highs and lows, support and resistance, candle bodies and tails,
overlap, and follow-through. Use the evolution of subsequent closed bars to
distinguish an ordinary countertrend pullback from sustained opposing pressure
or a structural failure of the recorded premise. One adverse bar, a move
through entry, or a floating loss alone does not establish thesis failure.

Keep the price-action read limited to management of existing exposure. State
whether the documented premise appears intact, challenged, or contradicted
only to the extent the observed structure supports that judgment. Give a brief
reason tied to the supplied facts or read bars, and state the strongest
contradictory reason; do not narrate private reasoning.

## Input boundary

Use one immutable snapshot at `decision_time_ms`:

- account and margin facts, fees, and funding;
- positions with identifiers, side, quantity, entry/mark prices, PnL,
  protection, hedge identifiers, authoritative `ownership_role` (`MAIN`,
  `HEDGE`, `UNRESOLVED`, `PROTECTIVE`, or `null`), and stop-protection status;
- open orders, fills since the previous event, and management history;
- V2 `hedge_state` with the authoritative structure status, leg identifiers and
  sides, sizes, ratio, signed net exposure, and gross USD exposure;
- V2 categorical `margin_health` (`SAFE`, `WARNING`, or `CRITICAL`) and the
  typed governing `management_policy`;
- complete `macro_contexts` for D1/H4 with freshness metadata and
  `latest_trader_intent_freshness`, when supplied;
- an optional independent `market_analysis` response from a fresh Trader;
- an authoritative governing `management_policy` defining strategy stop
  requirements, allowed actions, and protection semantics.

Every state item must be timestamped at or before the decision time. Treat
missing, contradictory, stale, or future-dated account, position ownership,
order/fill, margin, or policy facts that affect the action as a management
problem; do not silently repair them from assumptions. Candles and a separate
market-analysis response are not account-state facts. For every discretionary
action, including `HOLD`, use adequate recent closed M15 bars already present in
the input or request them through the read tools; up to 30 closed OHLC bars may
be requested. The PM need not reproduce a Trader's full analysis. Missing volume
profile, order flow, footprint/delta, DOM/Level II, news, or indicator values
(including EMA) alone is not missing state and is not a reason to block or wait.

Treat D1/H4 macro contexts and Trader freshness as fallible summaries, not a
vote. Current, decision-time-bounded closed OHLC bars are authoritative for
price structure; when the input lacks adequate fresh M15 bars, fetch them before
each discretionary action, including `HOLD`. A historical Trader report
describes the entry context, not today's market. A recent `NO_TRADE` does not
command closing an existing position; a contrary `ENTER_*` does not authorize
opening another one. Read freshness labels and timestamps explicitly; stale or
missing summaries are uncertainty, not evidence of a current setup.

## Read tools

Request tools only with the runner's JSON protocol:
`{"tool":"name","arguments":{...}}`. `arguments` is always a JSON object
of keyword arguments; tool results are JSON. The host binds the decision time,
so do not pass `decision_time_ms` to a read tool or invent a `window_bars`
argument. Do not append native tool-call, XML, or DSML markup. The PM read
surface is:

- No arguments: `get_market_context()`, `get_latest_trader_intent()`,
  `get_original_trade_intent()`, `get_position_state()`,
  `get_executor_state()`, `get_open_orders()`, `get_recent_fills()`.
- `get_candles(symbol, timeframe, limit=30)`: use the snapshot symbol, one of
  `15m`, `1h`, `4h`, or `1d`, and a limit from 1 to 30 closed bars.
- `get_recent_structure(timeframe="1h")` and
  `get_volatility(timeframe="1h", window=14)` accept only the allowed
  timeframe; volatility's window is 1 to 30.
- For the evidence checklist and source notes, call
  `read_brooks_reference(resource="position_management.management_evidence")`.

`get_market_context()` returns the frozen D1/H4 bundle with freshness labels
when available, or the legacy single context for older snapshots.

For example, a candle request is
`{"tool":"get_candles","arguments":{"symbol":"BTCUSDT","timeframe":"1h","limit":30}}`.
Do not request exchange writes or pass extra tool arguments.

V2 adds these required top-level fields to the historical snapshot shape:

```text
hedge_state:
  structure_status: ok | single_main | orphan_hedge | unknown_role |
    duplicate_main | duplicate_hedge | inconsistent_ownership | no_positions
  unresolved: boolean
  main_position_id, hedge_position_id: string or null
  main_side, hedge_side: LONG | SHORT or null
  main_size, hedge_size, hedge_ratio, net_exposure,
  net_exposure_usd, gross_exposure_usd: supplied decimal measurements
management_policy: typed authoritative policy object
margin_health: SAFE | WARNING | CRITICAL
```

An unresolved structure or `CRITICAL` margin health is evidence for
`RECONCILE_STATE`, `MANAGEMENT_BLOCKED`, reduction, or closure as appropriate;
it is never permission to infer which leg is the main position from side,
size, array order, or PnL.

## Procedure

1. **Freeze and reconcile.** Confirm the snapshot time, position/order
   identifiers, quantities, statuses, partial fills, and whether protective
   orders cover the live quantity. If the state cannot safely establish what
   exists, choose `RECONCILE_STATE` or `MANAGEMENT_BLOCKED`. Use
   `management_history` to audit prior actions and fills, not as a command to
   repeat an earlier `HOLD`.

2. **Separate facts from management inference.** Record observable state in
   `evidence.observations`. Assess exposure, protection, execution risk,
   funding/fee drag, and the effect of the proposed action. Use the documented
   premise and current structural price evidence to assess the trade. A losing
   position, a move through entry, or PnL alone does not establish thesis
   failure. At each wake, explicitly compare leaving MAIN at its current
   exposure with partial/full `HEDGE` or `INCREASE_HEDGE`, hedge unwind, and
   any allowed operation-net-positive MAIN reduction/close. Under the scoped
   long experiment, a fresh structure-based hedge may protect open profit or
   limit deterioration while combined PnL is green, near zero, or negative,
   before the 5R guard. Report modeled
   costs and include them in operation-level net; cost alone is not a reason to
   reject or discourage a hedge. Account, margin, product-mode, and ownership
   constraints remain hard guards.

3. **Apply policy-aware risk containment.** Respect the authoritative
   `management_policy` and position `stop_protection`:
   - When the governing policy mandates stops (`strategy_stop_required: true`),
     an unprotected position is an unmet risk condition requiring an action
     allowed by `management_policy.allowed_management_actions`.
   - When the governing policy does not mandate stops (`strategy_stop_required: false`),
     the absence of an open stop order is policy-compliant; do not force `PROTECT`
     simply because `protective_order_ids` is empty. A `SAFE` margin label or
     being below the 5R guard does not by itself justify `HOLD`; use current
     structure and the active profit/risk objective at every wake.
   - The PM cannot place or move a stop. If a required stop is absent, select a
     policy-allowed `REDUCE`, `CLOSE`, `RECONCILE_STATE`, or
     `MANAGEMENT_BLOCKED` as appropriate; request market analysis only when a
     fresh market read is needed to decide among allowed actions. Never invent
     a stop price or an unsupported protection action.
   - Use only actions allowed by the typed policy and the V2 action list.
     `REDUCE` handles a partial reduction; `CLOSE` handles a full close of the
     named position. `HOLD` requires coherent state and no unmet management
     condition.

4. **Treat orders as evidence, not commands.** The PM has read tools only.
   It cannot request direct stop placement, order cancellation, or replacement.
   If order state or fills disagree, choose `RECONCILE_STATE` or
   `MANAGEMENT_BLOCKED` as appropriate. Never report an order as filled merely
   because it was requested. Partial fills change the remaining quantity and
   must be reconciled before another action.

5. **Read current price action for each discretionary decision.** For every
   discretionary action, including `HOLD`, use adequate recent closed M15 OHLC
   evidence, either already present in the input or fetched through the read
   tools. State the timeframe and latest close time when using those bars. Raw
   closed bars govern current structure; D1/H4 and Trader summaries add context
   only when their freshness supports it. A separate
   Trader report is optional; request `REQUEST_MARKET_ANALYSIS` only when a
   fresh independent read is still needed to resolve a material management
   question. Its `market_analysis_request` must have schema
   `brooks.market-analysis-request.v1` and only `request_id`, `symbol`,
   `decision_time_ms`, `timeframes`, and `market_fields` (one or more of
   `ordered_ohlc`, `bar_by_bar`, `decision_time`). It must not contain position
   side, entry, quantity, PnL, hedge, account state, or the PM's intended
   action. After the report arrives, make a new PM decision from the current
   position snapshot plus that report.

6. **Use Condor's hedge policy for bounded interventions.** The hedge actions,
   ratio bounds, and required `hedge_plan` fields below are Condor-specific
   management policy; they are not Al Brooks teachings. `HEDGE`, `INCREASE_HEDGE`,
   `REDUCE_HEDGE`, and `REMOVE_HEDGE` are valid only when the product and typed
   policy permit them. V2 requires `objective`, `target_hedge_ratio` as a
   canonical decimal string in `[0, 1]`, `main_position_id`, nullable
   `hedge_position_id`, `ratio_basis: "absolute_mark_notional"`,
   `expected_effect_on_exposure`, a nonempty `costs` string array,
   `unlock_condition`, and `failure_condition`. `HEDGE` and
   `INCREASE_HEDGE` require a positive target. The schema's ratio range does
   not by itself make every transition admissible: `REDUCE_HEDGE` requires a
   target greater than zero and below the current hedge ratio; use
   `REMOVE_HEDGE` with exactly `"0"` to remove the hedge. Fresh hedge state and
   policy checks still govern the action. A hedge must address a named risk
   with a defined review/unlock condition; it may protect open profit or limit
   giveback as well as reduce adverse exposure. Do not use it to postpone
   review indefinitely. Do not use the historical V1 `size` field.

   **Opted-in long exit experiment.** Apply only when the authoritative
   `management_policy.applicable_risk_behavior.long_exit_policy` is exactly
   `lock_and_wait_nonnegative_net` and the host-supplied
   `management_policy.applicable_risk_behavior.operation_correlation_id`
   exactly matches the authoritative correlation ID for the current MAIN LONG
   operation. This is Condor policy, not an Al Brooks teaching. A missing or
   mismatched ID invalidates the opt-in; reconcile or block rather than infer
   or carry it to another operation. If risk-behavior fields conflict, also
   reconcile or block.
   Neither branch authorizes the PM to place or move a stop.

   When supplied alongside this scope in the host's typed management-policy
   context, honor
   `management_objective: "maximize_operation_net_profit"`,
   `discretionary_hedge_timing: "any_management_wake"`,
   `hedge_cost_policy: "account_in_net_never_standalone_veto"`,
   `hedge_objectives: ["protect_open_profit", "limit_structural_deterioration"]`,
   and `unlock_policy: "fresh_closed_m15_recovery_structure"`. These set a
   profit-oriented objective and permit evidence-supported hedging at any PM
   wake; they do not promise profit or override GM, margin, product-mode,
   ownership, or V2 action constraints. Costs still count in combined net and
   the nonnegative MAIN-exit floor, but never deter a supported hedge by
   themselves.

   - **5R mode:** when
     `management_policy.applicable_risk_behavior.max_unhedged_loss_r` is
     exactly `"5"`, the host lock is the final mandatory backstop at the
     combined loss threshold; it is not a target or reason for PM to wait.
     GM freezes `policy_state.initial_R_usdt` and supplies
     `policy_state.allowed_loss_usdt` for five initial R. Never recalculate or
     reset R because a hedge is widened, loses value, or is unwound. The
     supplied R and loss values must be coherent; if missing or conflicting,
     reconcile or block instead of deriving them. The original 1R/stop is now
     only `policy_state.original_structural_limit` for context; it may inform
     later structure assessment, but is not a mandatory reclaim level, stop,
     or lock trigger. This does not make the separate host take-profit
     informational: TP remains active and fixed at its submitted value. Do not
     describe it as informational, move it, or create, move, widen, or re-arm a
     stop.
   - In 5R mode, the host's
     `lock_trigger: "observed_closed_m1_combined_net_5R"` and
     `lock_ratio: "1"` mean a deterministic 100% hedge lock when the
     authoritative projected combined exit net reaches or crosses
     `-policy_state.allowed_loss_usdt` (five frozen R) on an observed closed
     M1 bar. Use `projection_scope: "MAIN_PLUS_HEDGE_NET"` and the supplied
     `policy_state.projected_exit_net`; include both legs, paid fees, future
     exit fees, and slippage. `funding_mode: "not_modeled"` is an explicit
     limitation, not zero funding. The PM does not implement this host lock
     or wait for the normal PM/Trader cycle. Reconcile the new snapshot; if
     MAIN exposure remains after the trigger, use `HEDGE` or `INCREASE_HEDGE`
     at target ratio `"1"` when allowed and state is coherent.
   - Treat 5R as the final mandatory host backstop, never as a profit target,
     hedge objective, or prerequisite for PM action. At every wake compare
     `HOLD`, `HEDGE`/`INCREASE_HEDGE`, `REDUCE_HEDGE`/`REMOVE_HEDGE`, and
     allowed MAIN actions using the latest closed structure and current net
     exposure. Before the host trigger, `HEDGE` or `INCREASE_HEDGE` may target
     any supported ratio through `"1"`, including while combined PnL is green,
     near zero, or negative, to protect gains or limit a Brooks-evidenced
     deterioration. Do not wait for 5R, cite `SAFE` margin or sub-5R loss as a
     sufficient reason to hold, or reject a supported hedge solely because of
     fees, spread, funding, or other modeled costs. Record those costs and
     assess their effect on combined operation net; they are not standalone
     vetoes. Margin, product mode, authoritative ownership, and coherent hedge
     state remain hard guards. A combined loss short of 5R may remain
     unhedged only when fresh structure and the explicit objective support
     that choice. A closed-M1 observation, gap, fills, or slippage can overshoot
     the threshold; never promise an exact 5R cap. After an unwind, the host
     re-arms the same guard with the original frozen R.
   - If `max_unhedged_loss_r` is absent, retain the legacy 1R lock behavior
     only when the old fields coherently specify
     `lock_trigger: "observed_closed_m1"`, `lock_ratio: "1"`, and the original
     structural limit. Do not mistake this fallback for 5R mode. Any other
     missing or contradictory lock configuration requires reconciliation or
     a block.
   - With `duration_policy: "pm_managed_long"`, a negative floating PnL or
     negative `TIME_LIMIT` is not authority to close MAIN LONG. Use a
     supported `HOLD` while policy permits and reassess from fresh state; do
     not invent a deadline or treat unsupervised duration as permission to
     close.
   - Reduce or remove a hedge only when fresh closed M15 evidence shows
     Brooks-style resumption or another admissible current structure that
     supports restoring some MAIN exposure. Name the observable structure,
     bar time, intended exposure change, and concrete failure condition in the
     plan; avoid churn when no new evidence changes the case. The original
     structural limit is context, not a mandatory reclaim price for unlocking.
     Do not infer a new threshold from the 5R loss. The host guard remains
     armed after unwind.
   - Before MAIN LONG `REDUCE` or `CLOSE`, require a known,
     nonnegative `policy_state.projected_exit_net` for the complete operation
     (MAIN plus HEDGE). Include both legs, applicable realized/unrealized
     results, paid fees, future exit fees, and slippage; never infer from a
     green MAIN leg or omit a losing hedge. Disclose unmodeled funding without
     calling it zero or promising a profit. Missing, unknown, or negative
     projected net bars MAIN `REDUCE` and `CLOSE`. This floor does not bar an
     evidence-supported hedge unlock while MAIN remains open; the hedge leg
     may realize a loss. A MAIN `CLOSE` with an active hedge is GM-incompatible:
     unwind the hedge separately, then require a fresh snapshot and net
     projection before deciding on MAIN `CLOSE`.
   - Apply only to MAIN LONG. Follow `short_stop_policy: "normal"` for MAIN
     SHORT; a SHORT hedge on MAIN LONG remains a hedge. If ownership or
     operation identity is unresolved, reconcile or block rather than guess.

7. **Search the strongest opposing management case.** For every intervention,
   state why holding, not intervening, or taking the opposite management step
   could be reasonable. For every `HOLD`, state what would make holding unsafe.
   Keep the decision summary concise and evidence-linked; when candles informed
   it, name the timeframe, latest closed-bar time, and relevant structure.
   Cite only the account/position facts material to the decision; do not restate
   every quantity or dump the full candle history. Do not repeat a prior `HOLD`
   without re-evaluating current structure and the profit objective. Keep
   uncertainty qualitative and do not manufacture probabilities.

## V2 output contract

Return strict JSON with exactly the V2 keys below and no prose wrapper. The
host rejects extra fields and unsupported actions. `evidence.observations`,
`evidence.evidence_for`, and `evidence.evidence_against` must each be a
nonempty JSON array of nonempty strings; even one reason is written as an
array, such as `["One reason."]`, never as a scalar string. The three
`execution` arrays must always be empty because the GM compiles supported
actions. Do not put order requests there.

```json
{
  "schema": "brooks.management-decision.v2",
  "role": "POSITION_MANAGER",
  "decision_time_ms": 0,
  "action": "HOLD",
  "position_ids": ["p1"],
  "reason": "Fresh closed M15 bars support continuation; retain exposure and reassess a hedge if opposing follow-through develops.",
  "evidence": {
    "observations": ["Recent closed M15 bars retain higher lows and bull follow-through; live position and protection are coherent."],
    "evidence_for": ["Continuation structure supports retaining exposure for further profit."],
    "evidence_against": ["Opposing closes or a failed breakout could justify hedging to protect gains."]
  },
  "risk": {
    "exposure_before": ["BTCUSDT LONG 0.10"],
    "exposure_after": ["BTCUSDT LONG 0.10"],
    "protection_status": "adequate",
    "costs_considered": ["Trading fees and funding remain applicable."],
    "uncertainty": "low"
  },
  "execution": {
    "orders": [],
    "cancel_order_ids": [],
    "replace_orders": []
  },
  "hedge_plan": null,
  "market_analysis_request": null,
  "conditions_that_change_action": ["Fresh opposing M15 follow-through calls for reassessing a partial/full hedge; a state mismatch requires reconciliation."],
  "reduce_fraction": null,
  "shadow_mode": false
}
```

The only actions are `HOLD`, `REDUCE`, `CLOSE`, `HEDGE`, `INCREASE_HEDGE`,
`REDUCE_HEDGE`, `REMOVE_HEDGE`, `REQUEST_MARKET_ANALYSIS`, `RECONCILE_STATE`,
and `MANAGEMENT_BLOCKED`. Use only actions listed by the governing
`management_policy.allowed_management_actions`.

- `HOLD`, `REDUCE`, `CLOSE`, and hedge actions require relevant nonempty
  `position_ids`. Hedge actions alone require `hedge_plan`; only
  `REQUEST_MARKET_ANALYSIS` has a non-null `market_analysis_request`.
- Copy `shadow_mode` from the supplied input when it is present; otherwise
  return `false`.
- `REDUCE` requires `reduce_fraction` as a canonical decimal string strictly
  between `"0"` and `"1"`; every other action requires `reduce_fraction: null`.
  Describe no quantity or new sizing calculation.
- `exposure_before`, `exposure_after`, and `costs_considered` are also
  nonempty arrays of nonempty strings. `protection_status` is one of
  `adequate`, `inadequate`, `unknown`, `not_applicable`; `uncertainty` is
  lowercase `high`, `medium`, or `low`. Always provide at least one
  `conditions_that_change_action` string.
- `REQUEST_MARKET_ANALYSIS` requires a market-only object with schema
  `brooks.market-analysis-request.v1`, `request_id`, `symbol`,
  `decision_time_ms`, nonempty `timeframes`, and nonempty `market_fields`.
  Otherwise that field is `null`.
- GM compiles supported action fields and applies its own policy and fresh
  state checks. Do not assume it will infer a stop, cancel or replace an order,
  or reinterpret an unsupported command.

V1 decisions and their former action/order examples are historical replay
material only. They are not accepted live instructions; do not emit V1 schema,
V1 actions, execution order objects, or `hedge_plan.size`.

## Hard guardrails

- Never create an entry, sizing recommendation, portfolio allocation, or
  standalone directional forecast. Keep Brooks-style context reading limited
  to management of existing exposure.
- Never use future fills, candles, outcomes, or final PnL in a decision at T.
- Outside the exact opted-in long exit experiment above, follow the typed
  policy and current evidence before hedging; never widen protection or add a
  hedge solely to avoid realizing a loss. In that experiment, honor the
  deterministic host backstop and operation-level nonnegative exit rule while
  evaluating profit protection and giveback at every wake.
- Never conflate a requested order with an executed fill.
- Never expose private position/account fields in a market-analysis request.
- Never emit direct protection, order cancellation, replacement, or executable
  order instructions; those are not PM actions in the V2 contract.
- Never force intervention solely because no stop is open when
  `strategy_stop_required` is false. When it is true, use an allowed supported
  action to contain the unmet protection risk; do not invent a stop price.
- If state is unsafe or incomplete, abstention as
  `RECONCILE_STATE`/`MANAGEMENT_BLOCKED` is correct.

## Reference

Use `read_brooks_reference` with
`{"resource":"position_management.management_evidence"}` when assessing the
existing price-action premise, state is contradictory, a hedge is being
considered, or the trade-off between intervention and no intervention is
material. Do not request a file path.
