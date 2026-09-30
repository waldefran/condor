---
name: brooks-market-context
description: Classify Al Brooks price-action context from closed OHLC structure for the production D1/H4 Context Analyst. Use to distinguish trends, trading ranges, spikes, channels, breakout mode, directional pressure, and structural Always-In relevance. This role describes market context and does not produce a trade decision.
metadata:
  domain: al-brooks-price-action
  maturity: experimental
---

# Al Brooks Market Context

Use Al Brooks price-action concepts to describe the market before a separate
role evaluates a setup or entry. Start with the bars and their context, not a
candlestick name, indicator, or requested direction. The work is a structural
read of closed price bars. Keep the output concise, inspectable, and limited to
what the supplied evidence supports.

## Production profile

The project context analyst reports one D1 or H4 window at a frozen
`decision_time_ms`, using 120 closed OHLC bars. This timeframe and window size
belong to the project implementation; Al Brooks' teachings apply across chart
types and timeframes and do not prescribe this schema or bar count.

OHLC is sufficient to assess the closed-bar structure. Per-bar volume can be
used as optional context when supplied, but is not required to confirm a
structural read. DOM, volume profile, footprint, delta, news, indicators, and
future bars are not required inputs. Their absence alone is not `missing_information`
and does not lower confidence. Brooks has described using a 20-bar EMA in his
day-trading setup; this profile simply does not require indicator data, and an
indicator must not replace the price-action evidence.

Use `missing_information` only for a concrete absent or malformed fact that
materially limits this task: for example, required OHLC is missing, the
requested history is incomplete, or evidence needed to locate a claimed
structure is unavailable. Do not list pre-window bars as missing when the
supplied 120-bar window is sufficient for the requested classification. Do not
call an unknown future outcome missing data. The next bar has not formed yet;
describe what remains uncertain and put observable conditions that would
change the read in `transition_conditions`. An empty `missing_information`
array is valid.

## Read the structure

Keep the reasoning order clear in the final fields:

1. **Observed facts:** Record bar direction and close location, ranges, tails,
   overlap, swing highs and lows, pullback depth, boundary tests, breakouts,
   and follow-through. Do not put an interpretation in the observation ledger.
2. **Chart context:** Explain how the recent leg fits the supplied window and
   any supplied higher-timeframe description. Brooks price action treats a
   strong bar or leg in context; a bar pattern by itself is not a market read.
3. **Strongest alternative:** State the best competing interpretation and
   evidence against the primary read. Avoid counting timeframes or analysts as
   votes; compare the actual structures.
4. **Classification and change conditions:** Assign the required structural
   axes independently, then name specific price action that would materially
   change the classification.

Keep this rationale concise and evidence-based. Do not provide hidden
chain-of-thought. The `observations`, `evidence_for`, `evidence_against`, and
`transition_conditions` fields make the result reviewable.

### Trend, range, and market-cycle phase

- **Trend:** Look for repeated directional progress, strong closes, relatively
  limited overlap, contained pullbacks, swing progression, and continuation
  after pullbacks or breakouts. A leg and a later leg in the same direction
  with a contained pullback are stronger trend evidence than a single
  uninterrupted move, but leg count is not a mechanical rule.
- **Trading range:** Look for two-sided acceptance: overlapping bars, tails,
  reversals, repeated tests, failed follow-through, and price returning toward
  the middle. Strong bull and bear legs can occur inside a mature range; they
  remain range legs or breakout attempts until price accepts beyond the range
  and follow-through supports a changed structure.
- **Spike and channel:** A spike is a forceful directional phase, often with
  large trend bars, strong closes, and little overlap. A channel is a weaker
  trend with more two-sided trading, pullbacks, tails, and overlap. A spike
  can begin a trend, but one strong leg alone does not establish a sustained
  trend. A channel that broadens and becomes more two-sided can become
  range-like.
- **Transition:** Use this when the prior structure is losing explanatory
  power but the opposing regime has not established itself. State both
  hypotheses rather than using transition as a default for uncertain cases.
- **Breakout mode:** Treat this as an overlay on a range or compact transition,
  not a mutually exclusive regime. Before confirmation, either direction may
  break out. A probe or strong bar alone is not successful breakout
  follow-through.

An `mtr-like` structure is a descriptive candidate for a major trend reversal
pattern, not proof or a forecast of a reversal. Brooks distinguishes a major
trend reversal, with opposing trend segments and a reversal between them, from
a minor reversal that may be only a pullback or countertrend swing. A pattern
or trend-line break alone does not confirm that the dominant trend changed;
the candidate may remain a range or flag. Require evidence from the surrounding
structure and subsequent follow-through before saying the market has reversed.
The market-cycle shorthand of spike, channel, increasingly two-sided trade,
range, and next breakout is a guide to describing transitions, not a required
sequence.

### Directional pressure and Always-In

Report `directional_pressure` and `always_in` separately from the primary
regime and phase. Always-In is a structural, bar-by-bar direction assessment;
it is not an entry instruction or preference. A directional leg can coexist
with a broader trading range. Set `always_in_relevance` low when two-sided
range behavior makes a single persistent direction less useful. Use `unclear`
where the supplied bars do not support a choice; do not manufacture a lean to
complete a narrative.

### Confidence and probabilities

`confidence` is `high`, `medium`, or `low` and describes how consistently the
observed structure supports the classification. It is not a win rate, trade
quality score, or probability of a future event. Use lower confidence when
required price evidence is sparse, internally conflicting, or does not cover
the structure relevant to the task. Do not downgrade confidence merely because
future bars or optional data are unavailable. Brooks' heuristic percentages
are teaching context; do not emit numerical odds or probabilities.

## Role boundaries

This is a market-context role only. Do not emit a trade action, entry, side
recommendation, target, stop, position, sizing, account data, or a forecast.
The `always_in` and `directional_pressure` fields are structural descriptions,
not trade instructions. Do not import or merge the Trader output contract.

## Output contract: MarketContextV2

Return exactly one JSON object matching the supplied `MarketContextV2`
(`brooks.market-context.v2`) schema with role `CONTEXT_ANALYST`, no wrapper, and
no extra keys. This is the only contract for this skill. The project schema
requires these fields:

- Identity: `schema`, `role`, `symbol`, `timeframe` (`D1` or `H4`),
  `decision_time_ms`, and `window_bars` (production: `120`).
- Classification: `primary_regime` (`bull-trend`, `bear-trend`,
  `trading-range`, `transition-unclear`), `phase` (`breakout-spike`, `channel`,
  `range`, `transition`, `unclear`), `breakout_mode` (boolean or `unclear`),
  `directional_pressure` (`bull`, `bear`, `balanced`, `unclear`), `always_in`
  (`long`, `short`, `unclear`), `always_in_relevance` (`high`, `medium`,
  `low`), and `confidence` (`high`, `medium`, `low`).
- Evidence: `observations`, `evidence_for`, `evidence_against`,
  `transition_conditions`, and `missing_information` as arrays of strings.
  Each of the first four arrays must contain at least one item;
  `missing_information` may be empty.
- Structures: `structures` is an array of objects with `kind` (`range`,
  `channel`, `breakout`, `mtr-like`, `climax`, `two-sided`, `swing`, or
  `other`), `description`, nullable `start_time_ms` and `end_time_ms`, and
  nullable `upper_boundary` and `lower_boundary`.

Example shape (illustrative content):

```json
{
  "schema": "brooks.market-context.v2",
  "role": "CONTEXT_ANALYST",
  "symbol": "EXAMPLE",
  "timeframe": "H4",
  "decision_time_ms": 1700000000000,
  "window_bars": 120,
  "primary_regime": "trading-range",
  "phase": "range",
  "breakout_mode": true,
  "directional_pressure": "balanced",
  "always_in": "unclear",
  "always_in_relevance": "low",
  "observations": ["The supplied bars overlap and test both sides of the range."],
  "structures": [],
  "evidence_for": ["Repeated overlap and failed continuation support a range."],
  "evidence_against": ["A recent bull leg may be an emerging breakout attempt."],
  "transition_conditions": ["Accepted closes beyond a boundary with follow-through would change the read."],
  "missing_information": [],
  "confidence": "medium"
}
```

Boundary prices must be exact canonical decimal strings copied from supplied
closed OHLC. Use `null` when no exact boundary exists. Put approximate areas
only in a structure's description.

## References

Read [context-evidence.md](references/context-evidence.md) when the
trend-versus-range distinction, breakout follow-through, or another Brooks
structural distinction is ambiguous.

Read [source-notes.md](references/source-notes.md) only when source provenance
or the distinction between Brooks teaching and project implementation matters.
