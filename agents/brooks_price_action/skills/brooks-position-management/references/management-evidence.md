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
account policy, while grounding directional management reasons in the recorded
premise and current price structure. Give a concise reason and the strongest
contradictory reason; do not provide a private reasoning transcript.

The PM's candle read is optional and limited to the available OHLC interface.
Missing volume profile, order-flow/footprint/delta, DOM/Level II, news, or EMA
values does not make the snapshot incomplete and is not a reason by itself to
block or request a Trader report. Al Brooks has said he does not use volume as
a price-action prerequisite and that he does not need Time & Sales or DOM to
read the chart; this does not mean that every Brooks chart concept excludes
indicators such as an EMA. Use supplied optional indicators only when relevant,
and never infer values that were not supplied.

## Condor hedge policy

The hedge actions, ratio constraints, and required fields below are Condor
management and execution policy. They are not Al Brooks teachings. Preserve
the typed management policy, authoritative hedge ownership, and reconciliation
rules when applying any price-action judgment.

## Action evidence

- **HOLD:** state is coherent, protection is adequate, and no supplied
  management condition requires intervention. Mention the risk that remains.
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
| costs | What fees, funding, spread, margin, and liquidation interactions matter? Supply a nonempty string array. |
| unlock_condition | What observable event permits reducing/removing the hedge or closing the base leg? |
| failure_condition | What makes the hedge invalid, too costly, or unsafe? |

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
If a new market reading is needed to choose the hedge direction or duration,
request an independent Trader report first. The report informs management; it
does not authorize an automatic hedge.

## Opposing case

The PM should record the strongest reason to choose a different action. Examples
include a stop that already covers the risk, an intervention that increases
fees/funding without changing net exposure, an order state that may be stale,
or a discretionary decision that lacks adequate current market evidence from
the snapshot or closed candles. This field is for uncertainty and auditability,
not for a keyword checklist.

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
