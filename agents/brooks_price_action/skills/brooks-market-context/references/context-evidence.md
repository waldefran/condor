# Brooks context evidence model

Use this reference to resolve ambiguous context. It is a compact operational synthesis, not a replacement for the source material.

## Core distinction: regime, phase, breakout mode, Always-In

These are related but not identical.

- **Regime** answers whether the current structure behaves primarily like a bull trend, bear trend, trading range, or unresolved transition.
- **Phase** locates the structure in the market cycle: breakout/spike, channel, range, or transition.
- **Breakout mode** is an overlay: price is balanced enough that a breakout in either direction can become important.
- **Always-In** asks which direction is currently easier to justify if forced to choose, and how useful that directional framing is in the present context.

Keeping these axes separate prevents common classification errors.

## Regime structure: legs are evidence, not a rule

For regime work, inspect whether directional progress is repeated: a leg, a
pullback, and a later leg in the same direction are stronger trend evidence
than one uninterrupted move. A single strong leg can be a breakout attempt,
climax, or a move inside a larger range. Deep pullbacks, growing overlap, and
legs that repeatedly flip direction reduce the trend interpretation and may
support `transition-unclear` or `trading-range`.

Use leg count as a descriptive aid, not a deterministic threshold. Close
location, overlap, pullback behavior, breakout acceptance, and the broader
window can outweigh a nominal count.

## Evidence favoring trend behavior

Weight several aligned observations more than one isolated feature:

- repeated directional trend bars or strong closes;
- successful breakouts with follow-through;
- relatively limited overlap during the directional move;
- pullbacks that remain contained and lead to renewed continuation;
- persistent swing progression in one direction;
- failed countertrend attempts followed by continuation;
- price repeatedly accepting beyond prior barriers rather than snapping back.

A trend can weaken without instantly becoming a trading range. Reduce confidence when pullbacks deepen, overlap grows, or continuation repeatedly fails.

## Evidence favoring trading-range behavior

Look for two-sided acceptance rather than direction alone:

- substantial overlap among bars;
- frequent tails and reversals;
- repeated tests of upper/lower areas;
- strong-looking legs that fail to produce sustained breakout follow-through;
- both bulls and bears obtaining reversals;
- price returning toward the middle after excursions;
- alternating directional pressure.

A strong leg inside a mature range is still only evidence for a breakout attempt until acceptance/follow-through changes the structure.

## Transition evidence

Use `transition-unclear` when evidence is genuinely mixed and the old regime is losing explanatory power but a new one has not established itself.

Typical evidence:

- a formerly clean trend develops deeper, more overlapping pullbacks;
- trend-line/channel behavior is breached but reversal follow-through is weak;
- repeated continuation attempts fail while the opposite side also cannot sustain a breakout;
- a breakout from a range occurs but confirmation is not yet sufficient.

Do not use `transition-unclear` as a lazy default. State the competing hypotheses.

## Breakout mode

Breakout mode is most useful when price is balanced around recognizable boundaries and the next successful breakout could establish directional follow-through.

Operationally:

- mark the relevant boundaries;
- treat both directions as plausible before confirmation;
- avoid promoting the first probe beyond a boundary to a new trend automatically;
- upgrade the breakout case when follow-through/acceptance confirms it;
- downgrade it when price promptly returns into the prior structure.

## Always-In

Evaluate the most recent decisive directional evidence, especially breakouts and follow-through. Then separately rate relevance.

- **High relevance:** clear directional behavior where trading against the current direction requires strong reversal evidence.
- **Medium relevance:** direction exists but channel/range effects materially weaken the edge.
- **Low relevance:** two-sided range behavior makes a single persistent directional label less useful.

Always-In direction can change before the broader chart is visually obvious. Conversely, do not use it to pretend a balanced range has become a clean trend.

Because Always-In is a forced-choice control question, one strong bar closing
near its extreme can create a low-confidence directional lean when a choice is
required. That bar alone does not justify high confidence, a mature regime
label, or an entry. If the window is genuinely balanced, use `unclear`
rather than manufacturing a direction.

## Entry boundary

Context evidence is a prerequisite for later setup or entry work, not a trade
instruction. A strong Always-In or trend read can still be a poor entry
location after an extended move, near opposing structure, or without a
defensible invalidation. A context classifier should state those limits rather
than inventing an entry.

## Market-cycle framing

A useful Brooks cycle model is:

```text
breakout/spike -> channel -> increasingly two-sided trade -> trading range -> next breakout
```

Real charts can skip, compress, or blur stages. Use the model as a state-transition guide, not a rigid sequence detector.

## Probability discipline

Brooks often teaches memorable probability heuristics (for example, the tendency of trading-range breakout attempts to fail and the inertia of existing behavior). Use these as contextual priors, not as independent numbers to multiply or combine mechanically.

When the actual chart provides strong contrary evidence—especially a breakout with strong follow-through—update the classification from current evidence rather than repeating a memorized percentage.

## Common failure modes

### Label-first reasoning

Bad: decide `bull-trend`, then search for bullish details.

Better: write observable evidence, competing evidence, then classify.

### Indicator substitution

A moving average can be supporting context. It does not replace bar structure, breakouts, follow-through, swings, and two-sided behavior.

### Single-bar regime flips

A large bar is important but is not always enough. Ask what happened before it and whether the market accepted the breakout afterward.

### Range amnesia

Do not forget the broader range because the current leg is emotionally convincing. Strong legs frequently occur within ranges.

### False precision

Do not emit `confidence: 0.83` simply because a schema allows a float. Prefer categorical confidence unless a real calibrated model supplies the number.
