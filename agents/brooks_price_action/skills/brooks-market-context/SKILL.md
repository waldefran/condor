---
name: brooks-market-context
description: Classify Al Brooks price-action market context from a chart, OHLC bars, or a bar-by-bar description. Use when an agent needs to establish trend vs trading-range context, market-cycle phase, breakout-mode status, directional pressure, or Always-In direction/relevance before evaluating Brooks setups, entries, reversals, breakouts, or trade management. Also use when a user asks whether current price action is trending, ranging, transitioning, or in breakout mode. Do not use this Skill alone to decide whether to enter now; entry-quality questions belong to a separate entry operation.
metadata:
  domain: al-brooks-price-action
  maturity: experimental
---

# Brooks Market Context

Establish context before naming or grading setups. Treat context as multiple axes rather than forcing one overloaded label.

## Inputs

Use any reliable combination of:

- chart/image;
- ordered OHLC bars;
- bar-by-bar description;
- timeframe/session context;
- higher-timeframe context, when supplied.

Do not invent missing bars, levels, indicators, or session facts. If evidence is insufficient, lower confidence and state what is missing. Use the complete supplied closed-bar window. For the production profile, compare up to 120 closed H4, H1, and M15 bars and state conflicts between them; do not treat a short local window as equivalent without comparable DEVELOPMENT evidence.

## Procedure

### 1. Build an observation ledger

Inspect the recent price action before classifying it. Record only observable features first:

- directional trend bars and closes;
- bar overlap and tails;
- size/depth of pullbacks;
- swing progression;
- breakout attempts;
- follow-through or immediate failure;
- repeated tests of boundaries;
- acceleration, climax, or loss of momentum;
- two-sided trading;
- distinct directional legs and the depth/quality of intervening pullbacks.

Read `references/context-evidence.md` when the classification is ambiguous or when you need the Brooks-specific evidence map.

### 2. Classify the primary regime

Choose one:

- `bull-trend`
- `bear-trend`
- `trading-range`
- `transition-unclear`

Do not make `breakout-mode` a mutually exclusive regime. A trading range or compact transition can be in breakout mode. A single directional leg, even
if visually strong, is evidence of a breakout attempt or transition rather
than proof of a mature trend; require the surrounding structure and any
follow-through before upgrading the regime.

### 3. Classify market-cycle phase

Choose the best current phase:

- `breakout-spike`
- `channel`
- `range`
- `transition`
- `unclear`

Use the most recent structure that matters to the user's task. Mention broader context separately when it conflicts.

### 4. Evaluate breakout mode

Set `breakout_mode` to `true`, `false`, or `unclear`.

Use `true` when price is balanced/compressed around defined boundaries and a breakout in either direction could plausibly produce follow-through. Do not treat every strong bar inside a range as a successful breakout.

### 5. Evaluate Always-In separately

Choose `long`, `short`, or `unclear`, then assign `always_in_relevance` as `high`, `medium`, or `low`.

A strong breakout with convincing follow-through can change the Always-In assessment quickly. In two-sided trading ranges, an intrarange directional leg can coexist with low usefulness of the Always-In concept for the broader context.
Always-In is a forced-choice bar-by-bar control question, not the same as
regime classification or an entry recommendation. A single strong bar that
closes near its extreme may justify a low-relevance directional lean while
follow-through is unknown; it must not by itself make the broader regime a
trend or produce high confidence.

### 6. Weigh conflict before confidence

List the strongest evidence supporting the classification and the strongest evidence against it. Use `high`, `medium`, or `low` confidence unless a downstream interface explicitly requires another scale.
For `high`, require enough supplied structure for the axis being classified
and no meaningful conflicting evidence. Mixed, incomplete, single-bar, or
one-leg windows should use a lower band and state the missing structure.

Do not manufacture a numerical probability from Brooks heuristic percentages. Read `references/context-evidence.md` for probability discipline.

### 7. State transition conditions

Identify what new price action would materially change the classification, such as:

- strong breakout plus follow-through;
- failed breakout and return into the prior range;
- deeper two-sided pullbacks that weaken trend behavior;
- renewed directional bars after a transition.

## Output contract

Return JSON with one `market_context` object and exactly the keys shown below, adapting prose only when the user explicitly requests a different format:

```json
{
  "market_context": {
    "primary_regime": "bull-trend",
    "phase": "channel",
    "breakout_mode": false,
    "directional_pressure": "bull",
    "always_in": "long",
    "always_in_relevance": "medium",
    "confidence": "medium",
    "observations": ["Recent supplied bars show bull closes with shallow pullbacks."],
    "evidence_for": ["Bull closes and limited pullback depth support directional pressure."],
    "evidence_against": ["The supplied window includes some overlap and opposing tails."],
    "broader_context": null,
    "transition_conditions": ["A failed breakout or deeper two-sided pullback would weaken the read."],
    "missing_information": []
  }
}
```
Allowed values are listed in the procedure above; the example uses one valid combination.

## Guardrails

- Context precedes setup labels.
- A single leg is not automatically a trend; inspect pullback depth, overlap,
  and whether a second leg or acceptance followed.
- Always-In direction is separate from the primary regime and does not tell the
  agent to enter.
- Do not equate price above/below one moving average with Brooks market context.
- Do not call a trend from a single breakout bar without considering follow-through and surrounding structure.
- Do not erase a mature trading range merely because one leg looks strong.
- Do not force certainty when bull and bear evidence is balanced.
- Do not let an indicator, pattern name, or the user's preferred direction
  replace the bar evidence.
- Keep observations distinct from interpretation.

## References

Read `references/context-evidence.md` for the compact Brooks evidence model and edge cases.

Read `references/source-notes.md` only when provenance or a disputed definition matters.
