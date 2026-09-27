---
name: brooks-position-management
description: Manage positions and orders that already exist using a time-bounded operational snapshot. Use when an agent must hold, protect, reduce, close, reconcile, hedge, unhedge, replace, or cancel existing exposure, or decide that a fresh blind market analysis is required. Do not use for finding new entries, independent Brooks market reading, portfolio allocation, or forecasting.
license: MIT
author: Skill-Brooks
---

# Brooks Position Management

## Operation

Given the current account, position, order, fill, cost, and management state,
choose the safest coherent next management action. This is a state-management
operation, not a second Trader. The operation ends with one structured decision;
it does not place an order or predict a future price.

## Contract versions

New hedge-aware calls use `brooks.position-management-input.v2` and return
`brooks.management-decision.v2`. The v1 input and decision contracts remain
unchanged and are accepted for historical replay only; their legacy
`hedge_plan.size` field must not be copied into a V2 decision.

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
- an optional independent `market_analysis` response from a fresh Trader;
- an authoritative governing `management_policy` defining strategy stop
  requirements, allowed actions, and protection semantics.

Every state item must be timestamped at or before the decision time. Treat
missing, contradictory, stale, or future-dated state as a management problem;
do not silently repair it from assumptions. Do not require candles or invent a
market read. A market-analysis response is evidence supplied to the PM, not an
invitation to reproduce the Trader's analysis.

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
   exists, choose `RECONCILE_STATE` or `MANAGEMENT_BLOCKED`.

2. **Separate facts from management inference.** Record observable state in
   `evidence.observations`. Assess exposure, protection, execution risk,
   funding/fee drag, and the effect of the proposed action. A losing position
   is not by itself a reason to hedge, hold, or close; do not anchor on entry
   price or PnL.

3. **Apply policy-aware risk containment.** Respect the authoritative
   `management_policy` and position `stop_protection`:
   - When the governing policy mandates stops (`strategy_stop_required: true`),
     an unprotected position is an unmet risk condition requiring intervention.
   - When the governing policy does not mandate stops (`strategy_stop_required: false`),
     the absence of an open stop order is policy-compliant; do not force `PROTECT`
     simply because `protective_order_ids` is empty. If margin and account risk
     remain healthy, `HOLD` is legitimate.
   - Action `PROTECT` strictly denotes placing an executable stop order. If choosing
     `PROTECT`, you must provide an explicit, positive numeric `stop_price` and
     required order fields (`reduce_only: true`, opposing side) in `execution.orders`.
     Never choose `PROTECT` without a well-founded structural stop level; if a
     structural level is absent, use `HOLD`, `REQUEST_MARKET_ANALYSIS`, `REDUCE`,
     or `CLOSE` as appropriate.
   - Use `MOVE_PROTECTION` only when structural progression warrants it and does
     not widen risk. Use `REDUCE`, `TAKE_PARTIAL`, `CLOSE`, or `CLOSE_ALL` when
     exposure must be trimmed. `HOLD` requires coherent state and no unmet
     protection or management condition.

4. **Manage orders explicitly.** Use `CANCEL_ORDER` or `REPLACE_ORDER` only
   with an identified stale, duplicate, invalid, or unsafe order and state the
   precondition. Never report an order as filled merely because it was
   requested. Partial fills change the remaining quantity and must be
   reconciled before another action.

5. **Request a fresh market read when needed.** If a discretionary action
   depends on current price action and no independent report is available,
   return `REQUEST_MARKET_ANALYSIS`. Its request must contain only symbol,
   decision time, requested market timeframes/fields, and an opaque request id.
   It must not contain position side, entry, quantity, PnL, hedge, account
   state, or the PM's intended action. After the report arrives, make a new PM
   decision from the current position snapshot plus that report.

6. **Use hedges as bounded interventions.** `HEDGE`, `INCREASE_HEDGE`,
   `REDUCE_HEDGE`, and `REMOVE_HEDGE` are valid only when the product supports
   them and the output states all of: objective, target hedge ratio, named main
   and hedge position identifiers, `ratio_basis: "absolute_mark_notional"`,
   expected effect on net exposure, fees/funding/margin cost, unlock condition,
   and failure condition. In V2 the target ratio is a canonical decimal string
   in `[0, 1]`; `HEDGE` and `INCREASE_HEDGE` require a positive target,
   `REDUCE_HEDGE` permits zero, and `REMOVE_HEDGE` requires exactly `"0"`.
   A hedge must reduce a named risk for a defined period; it must not be a way
   to hide a losing trade or postpone a decision indefinitely. A V1 replay may
   still contain its historical free-text `size` field.

7. **Search the strongest opposing management case.** For every intervention,
   state why holding, not intervening, or taking the opposite management step
   could be reasonable. For every `HOLD`, state what would make holding unsafe.
   Keep uncertainty qualitative and do not manufacture probabilities.

## Output contract

Return strict JSON with exactly these top-level keys and no prose wrapper:

```json
{
  "schema": "brooks.management-decision.v1",
  "role": "POSITION_MANAGER",
  "decision_time_ms": 0,
  "action": "HOLD",
  "position_ids": ["p1"],
  "reason": "The live position and its protection are coherent.",
  "evidence": {
    "observations": ["The supplied position quantity matches its active stop quantity."],
    "evidence_for": ["Protection covers the currently open quantity."],
    "evidence_against": ["The position remains exposed if the stop is canceled or rejected."]
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
  "conditions_that_change_action": ["A fill, cancellation, or quantity mismatch requires reconciliation."]
}
```

Each `execution.orders` item is an order request object with a required
non-empty `type` and only these optional fields: `order_id`, `position_id`,
`symbol`, `side`, `quantity`, `reduce_only`, `order_type`, `price`, and
`stop_price`. Each `execution.replace_orders` item must contain an existing
`order_id` and a non-empty `replacement` object using the same order fields.
These are requests, not exchange acknowledgements; a fill must be observed in
a later snapshot.

Allowed actions are `HOLD`, `PROTECT`, `MOVE_PROTECTION`, `TAKE_PARTIAL`,
`REDUCE`, `CLOSE`, `CLOSE_ALL`, `HEDGE`, `INCREASE_HEDGE`, `REDUCE_HEDGE`,
`REMOVE_HEDGE`, `CANCEL_ORDER`, `REPLACE_ORDER`, `RECONCILE_STATE`,
`REQUEST_MARKET_ANALYSIS`, and `MANAGEMENT_BLOCKED`.

For hedge actions, `hedge_plan` is required with
`objective`, `size`, `expected_effect_on_exposure`, `costs`,
`unlock_condition`, and `failure_condition`. For
`REQUEST_MARKET_ANALYSIS`, `market_analysis_request` is required and must be
market-only. For all other actions it is null. `evidence_against` is required
even when the decision is `HOLD`; it is the strongest reason the selected
action might be wrong.

## V2 decision delta

The V2 decision keeps the exact top-level keys, evidence, risk, execution, and
market-analysis request structure shown above, but uses this schema and hedge
plan shape:

```json
{
  "schema": "brooks.management-decision.v2",
  "hedge_plan": {
    "objective": "Temporarily reduce a named exposure risk.",
    "target_hedge_ratio": "0.25",
    "main_position_id": "main-1",
    "hedge_position_id": null,
    "ratio_basis": "absolute_mark_notional",
    "expected_effect_on_exposure": "Reduce net long mark-notional exposure.",
    "costs": ["Fees, funding, spread, and margin use."],
    "unlock_condition": "The supplied risk window ends.",
    "failure_condition": "The hedge cannot execute or margin becomes unsafe."
  }
}
```

`target_hedge_ratio` is the desired hedge notional divided by absolute main
mark notional. Never emit a ratio above `1.0`, a numeric JSON value, or the V1
`size` field. The external execution boundary remains responsible for quantity
calculation, quantization, risk admission, and exchange state.

## Hard guardrails

- Never create an entry, sizing recommendation, portfolio allocation, or
  independent Brooks context classification.
- Never use future fills, candles, outcomes, or final PnL in a decision at T.
- Never widen protection or add a hedge merely to avoid realizing a loss.
- Never conflate a requested order with an executed fill.
- Never expose private position/account fields in a market-analysis request.
- Never emit action PROTECT without an explicit, positive numeric stop_price and valid opposing-side order parameters in execution.orders.
- Never treat an unprotected position as a policy violation or force PROTECT when the governing management_policy defines strategy_stop_required as false.
- If state is unsafe or incomplete, abstention as
  `RECONCILE_STATE`/`MANAGEMENT_BLOCKED` is correct.

## Reference

Read `references/management-evidence.md` when state is contradictory, a hedge
is being considered, or the trade-off between intervention and no intervention
is material.
