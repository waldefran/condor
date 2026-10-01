# Position-management evidence map

This reference is an operational checklist for existing exposure. It does not
replace exchange-specific order rules and does not turn management heuristics
into deterministic financial promises.

## State before action

Check, in order:

1. the snapshot and every event timestamp are at or before the decision time;
2. live quantities reconcile with fills and order statuses;
3. each position has an unambiguous symbol, side, and identifier;
4. protective quantity covers the live quantity or the uncovered amount is
   explicit;
5. hedge legs, if any, are identified separately from the exposure they offset;
6. fees, funding, margin mode, and product constraints are visible when they
   affect the action.

If those checks cannot be completed, the PM should reconcile or block rather
than infer a convenient state.

Use `management_history` to audit prior actions and fills. It is not a command
to repeat a prior `HOLD`; reassess current structure, exposure, and the active
objective at each wake.

## Price-action evidence for existing exposure

Price action informs whether the recorded trade premise still fits the market;
it does not replace position ownership, execution, margin, or management-policy
facts. Use the original trade intent and supplied Trader analysis when they are
available; retrieve the intent with `get_original_trade_intent` when needed. If
no premise or invalidation was recorded, say so and do not invent one.

When the current structure matters, the PM can read up to 30 closed OHLC bars
on demand. Keep the summary tied to the timeframe and latest bar close, and
name the visible evidence: closes relative to prior bars or recorded structure,
swing highs/lows, support/resistance tests, bodies/tails, overlap, breakout
quality, and follow-through. Compare subsequent bars with the existing premise.
A pullback or isolated adverse bar can occur without a trend reversal; sustained
opposite-direction follow-through and a break of the premise's stated structure
are stronger evidence that it is challenged or has failed. A transition into a
tight range may also contradict a premise that expected directional follow-
through. These are contextual observations, not deterministic signals.

Entry price and unrealized PnL are account facts, not structural proof that the
thesis failed. Consider PnL, fees, funding, and margin under the governing
account policy, while grounding discretionary management in the recorded
premise and current price structure. At every wake compare holding, capturing
open profit or reducing giveback, and evidence-supported hedge/unwind choices.
For each discretionary action, including `HOLD`, use fresh closed M15 bars;
fetch them if the input's raw bars are inadequate. Inspect the complete
D1/H4 `macro_contexts` and `latest_trader_intent_freshness` when supplied, but
treat those summaries as fallible context, not a vote or a substitute for
current closed bars. The recorded entry Trader read is historical. A recent
`NO_TRADE` does not command closing the open position, and a contrary
`ENTER_*` does not command a new entry. Give a concise reason and the strongest
contradictory reason; do not provide a private reasoning transcript.

In the opted-in long experiment, account for costs in combined operation net
and report them, but do not use fees, spread, funding, or other costs alone to
veto or discourage a structure-supported hedge. Do not promise profit or
recovery. Margin health, product mode, hedge ownership, and coherent live state
remain hard constraints.

For every discretionary decision, use adequate recent closed M15 bars from the
input or the available OHLC read interface; the PM need not reproduce a full
Trader analysis or paste all bars into its rationale. Missing volume profile,
order-flow/footprint/delta, DOM/Level II, news, or EMA values does not make the
snapshot incomplete and is not a reason by itself to block or request a Trader
report. Al Brooks has said he does not use volume as a price-action prerequisite
and that he does not need Time & Sales or DOM to read the chart; this does not
mean that every Brooks chart concept excludes indicators such as an EMA. Use
supplied optional indicators only when relevant, and never infer values that
were not supplied.

## Condor hedge policy

The hedge actions, ratio constraints, and required fields below are Condor
management and execution policy. They are not Al Brooks teachings. Preserve
the typed management policy, authoritative hedge ownership, and reconciliation
rules when applying any price-action judgment.

For an operation-scoped `lock_and_wait_nonnegative_net` experiment, apply the
procedure in the parent skill only when the host-supplied
`operation_correlation_id` exactly matches the current MAIN LONG operation.
With `max_unhedged_loss_r: "5"`, the host locks at 100% when combined projected
exit net reaches the five frozen initial-R loss on a closed M1 bar. This is the
final mandatory host backstop, not a PM target or prerequisite: at every wake,
compare holding, profit protection/giveback, hedge/increase, and unwind choices
against fresh closed M15 structure. A supported hedge through ratio 1 may be
chosen before 5R with combined PnL green, near zero, or negative. Account for
fees and modeled exit costs in net, but they are not standalone reasons to
refuse that hedge. Reduce or remove a hedge only with fresh M15 evidence of
resumption or other admissible current structure and a concrete plan; the
original structural limit is informative context, not a mandatory reclaim
price. If the max-loss field is absent, use only the explicitly defined legacy
1R fallback. When supplied alongside this scope in the host's typed
management-policy context, honor
`management_objective: "maximize_operation_net_profit"`,
`discretionary_hedge_timing: "any_management_wake"`,
`hedge_cost_policy: "account_in_net_never_standalone_veto"`,
`hedge_objectives: ["protect_open_profit", "limit_structural_deterioration"]`,
and `unlock_policy: "fresh_closed_m15_recovery_structure"`. The host TP remains
active and fixed; treating the original stop/structural limit as informational
does not make TP informational, and PM cannot move TP. Costs remain part of
combined exit net and the nonnegative MAIN-exit floor. Judge exit net across
both legs and costs; disclose that funding is not modeled and do not promise an
exact cap. These are Condor rules, not Al Brooks teachings.

## Action evidence

- **HOLD:** state is coherent and fresh structure supports continued exposure;
  explain the profit objective for holding, what current structure supports
  leaving exposure open, and what would make that judgment unsafe. Do not use
  a price prediction. `SAFE` margin or being short of 5R is not sufficient.
- **REDUCE:** identify the affected position, the `reduce_fraction`, and the
  expected exposure change. Do not supply a quantity or count a partial fill
  twice; reconcile observed fills before another action.
- **CLOSE:** identify the position and why continued exposure is no longer
  acceptable. There is no `CLOSE_ALL` action; name the positions being closed.
- **HEDGE / INCREASE_HEDGE / REDUCE_HEDGE / REMOVE_HEDGE:** use the V2 hedge
  plan fields below and explain the bounded risk being changed.
- **REQUEST_MARKET_ANALYSIS:** request only a fresh, independent public-market
  read when it is necessary to resolve a material management question. Keep
  the request free of position, account, and intended-action details.
- **RECONCILE_STATE:** use when fills, quantities, orders, or hedge legs do not
  agree. Reconciliation is an action, not permission to invent a fill.
- **MANAGEMENT_BLOCKED:** use when a missing or conflicting operational fact,
  policy limit, or unavailable safe action prevents a supported decision.

Keep the decision reason concise: cite the latest relevant closed-M15 bar and
structure, the action objective, and only the net/cost facts needed for the
decision. Do not repeat every account quantity or paste all bars returned by a
30-bar read.

V2 requires `evidence.observations`, `evidence.evidence_for`, and
`evidence.evidence_against` to each be a nonempty array of nonempty strings.
Write one reason as `["Reason."]`, never as a scalar. The same nonempty-array
rule applies to `risk.exposure_before`, `risk.exposure_after`,
`risk.costs_considered`, hedge `costs`, and `conditions_that_change_action`.
Use lowercase `high`, `medium`, or `low` for uncertainty and the exact
protection-status values from the V2 contract.

## Hedge discipline

A hedge is a temporary exposure-management tool, not a loss accounting trick.
Its plan must answer:

| Required field | Question |
|---|---|
| objective | What risk is being reduced, and why now? |
| target_hedge_ratio | What target ratio, as a canonical decimal string from `"0"` to `"1"`, is intended? |
| main_position_id | Which identified main position is being managed? |
| hedge_position_id | Which existing hedge is affected, or `null` when none exists yet? |
| ratio_basis | Use the contract value `absolute_mark_notional`. |
| expected_effect_on_exposure | How does the named action change net exposure? |
| costs | What fees, funding, spread, margin, and liquidation interactions matter? Supply a nonempty string array; costs are reported and included in net, but alone do not veto a supported hedge. |
| unlock_condition | What fresh closed-M15 structure or other observable evidence permits reducing/removing the hedge or closing the base leg? A reclaim of the original stop reference is not mandatory. |
| failure_condition | What observable structure, ownership, margin, or execution failure makes the hedge invalid or unsafe? Costs alone are not a veto. |

Use the exact V2 names `target_hedge_ratio`, `main_position_id`,
`hedge_position_id`, and `ratio_basis`. Do not emit a `size` field: GM owns
quantity derivation and execution checks. `REMOVE_HEDGE` requires a target
ratio of `"0"`. Although the typed ratio field accepts values from 0 to 1,
transition checks require `REDUCE_HEDGE` to target a ratio above zero and below
the current hedge ratio; choose `REMOVE_HEDGE` to target zero. Fresh hedge state
and policy checks still govern admissibility. Hedge actions require a plan and
position identifiers appropriate to the existing state; do not infer
ownership from side, size, or PnL.

If no unlock condition can be stated, do not use a hedge as a default answer.
Use current closed M15 bars to choose a hedge direction, ratio, and review
condition when they adequately show the structure. If those bars are inadequate
and a new market reading is material, request an independent Trader report; it
informs management but does not authorize an automatic hedge. Do not churn a
hedge without new evidence, and do not let cost alone prevent a justified risk
or profit-protection hedge.

## Opposing case

The PM should record the strongest reason to choose a different action. Examples
include fresh structure supporting a continued move, a hedge that does not
meaningfully change exposure, an order state that may be stale, or inadequate
current bar evidence. Disclose fees/carry and their effect on net, but do not
turn costs alone into an argument against a supported hedge. This field is for
uncertainty and auditability, not for a keyword checklist.

## Live V2 boundary

The live action vocabulary is `HOLD`, `REDUCE`, `CLOSE`, `HEDGE`,
`INCREASE_HEDGE`, `REDUCE_HEDGE`, `REMOVE_HEDGE`, `REQUEST_MARKET_ANALYSIS`,
`RECONCILE_STATE`, and `MANAGEMENT_BLOCKED`. The three arrays under
`execution` (`orders`, `cancel_order_ids`, and `replace_orders`) must remain
empty. PM reports evidence and selects a supported action; GM compiles that
action under Condor's policy and fresh-state checks. Do not issue executable
order, stop-placement, cancellation, or replacement commands or promise that
GM will infer them.

V1 decisions and their former order examples are historical replay material
only. Use `brooks.management-decision.v2` for every live response. A
`REQUEST_MARKET_ANALYSIS` payload must use the market-only
`brooks.market-analysis-request.v1` contract; it cannot contain account,
position, order, hedge, or intended-management-action data.

## Brooks source notes

These official Brooks Trading Course materials support the price-action lens;
they do not replace Condor's operational or hedge policy:

- [How to Trade Price Action Manual](https://www.brookstradingcourse.com/how-to-trade-price-action-manual/): Al Brooks frames decisions by market cycle (trend or trading range, then channel or breakout) and discusses trade management.
- [10 Best Price Action Trading Patterns](https://www.brookstradingcourse.com/price-action/10-best-price-action-trading-patterns/): Al describes trends through higher/lower highs and lows, context-dependent reversals, pullbacks, breakouts, and support/resistance.
- [Ask Al: Time Stops — Al's Approach](https://www.brookstradingcourse.com/ask-al/time-stops-al-brooks-approach/): Al describes evaluating whether follow-through and market behavior match the trade premise; if the premise remains valid, he may hold.
- [Ask Al: Extended Trading Room Discussion](https://www.brookstradingcourse.com/ask-al/trading-room-extended-discussion/): Al says he does not look at volume and explains reading setups and trapped traders from bars and context.
- [Ask Al: Trading with Time & Sales](https://www.brookstradingcourse.com/ask-al/trading-time-sales-benefit/): Al describes relying on chart reading rather than Time & Sales or DOM for his price-action read. The PM's lack of those optional feeds therefore must not be treated as missing state.

## Book cross-checks

These short paraphrases use the local Wiley editions; PDF page numbers below
are one-based and identify PDF pages, not the books' printed page numbers.

- In *Trading Price Action Trading Ranges*, Chapter 24, one bull-spike example
  treats the premise as intact while price holds above the spike's base (PDF
  p. 170). The example illustrates using a named structural premise; its exact
  level is not a universal invalidation rule. Chapter 25 says to reassess as
  the market changes and exit when the premise no longer works, rather than
  rely on hope (PDF p. 177).
- In *Trading Price Action Trading Ranges*, Chapter 25, Brooks describes
  probability, reward, and risk changing as price evolves (PDF pp. 171, 178).
  He also uses initial stop distance when evaluating a prospective trade's
  reward (PDF p. 182). For this PM, that supports reassessing the existing
  position from current evidence; it does not authorize calculating a fresh
  entry, target, or position size.
- In *Trading Price Action Trading Ranges*, Chapter 29, Brooks distinguishes
  money-management stops from price-action stops (PDF p. 202) and discusses
  allowing room for a pullback when the premise remains valid (PDF p. 203).
  These examples do not change Condor's `management_policy` or authorize the
  PM to invent or widen a stop.
- In *Trading Price Action Reversals*, the glossary defines stop-distance risk
  as a minimum that slippage and other factors can increase (PDF p. 13). Use
  supplied fills, quantities, fees, and margin for operational exposure; do not
  treat structural thesis evidence as a calculation of actual account risk.
- In *Trading Price Action Reversals*, Chapter 9 explains that failed setups
  can lead to an opposite move of only scalp size or, in the right context, a
  larger reversal (PDF p. 99). Its bull-trend example shows a strong bear bar
  with weak follow-through and overlapping bars failing to establish a bear
  reversal (PDF p. 108). Assess context and subsequent follow-through rather
  than deciding from one adverse bar.
