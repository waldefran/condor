# Brooks Context Evidence

This is a compact operational synthesis of Al Brooks price-action concepts.
Use it to resolve a structural ambiguity; it is not a mechanical indicator or
trade plan. Raw closed OHLC bars are the primary evidence for this project
role.

## Keep the classification axes separate

- **Regime** describes whether the supplied structure behaves mainly as a bull
  trend, bear trend, trading range, or unresolved transition.
- **Phase** describes breakout spike, channel, range, transition, or unclear
  structure.
- **Breakout mode** overlays a range or compact transition where either
  boundary could break.
- **Directional pressure** summarizes which side has more observable pressure
  in the supplied bars, or whether pressure is balanced/unclear.
- **Always-In** is the current structural direction if one must be chosen;
  `always_in_relevance` separately captures whether that label is useful in
  the broader context.

Do not let one axis dictate the others. A latest leg can have bull pressure
while the broader regime remains a range, and Always-In can have low relevance
in two-sided action.

## Trend versus trading range

### Evidence favoring a trend

Weigh a group of aligned observations:

- repeated directional bars and closes near the bar extremes;
- relatively little overlap during the directional progress;
- higher highs and higher lows in a bull move, or lower highs and lower lows
  in a bear move;
- contained pullbacks followed by renewed movement in the trend direction;
- breakout acceptance, continuation, or failed countertrend attempts.

A single strong leg may be a spike, a range leg, a climax, or a breakout
attempt. A later leg after a contained pullback strengthens a trend reading,
but do not use a fixed leg count. Pullback depth, overlap, acceptance,
follow-through, and the larger supplied structure matter more than a count.

### Evidence favoring a trading range

Look for two-sided acceptance:

- substantial overlap, prominent tails, and repeated reversals;
- tests of both boundaries and returns toward the middle;
- alternating legs that fail to continue beyond the range;
- strong looking bull or bear legs that lack follow-through or promptly return
  inside the range.

A strong range leg is still a range leg while the range continues to explain
the evidence. Do not promote it to a trend merely because it is large or
directional. Reassess when closes hold outside a relevant boundary and
follow-through changes the structure.

## Spike, channel, and transition

A spike is a forceful move, often with wide trend bars, strong closes, and
little overlap. A channel is a weaker trend: opposing bars, tails, overlap,
and pullbacks become more visible. Channels can broaden and become increasingly
range-like. Do not claim that every chart passes through these phases in a
fixed order.

Use `transition-unclear` when the prior structure is weakening and the new
structure has not yet established itself. Examples include a trend that gains
deep overlapping pullbacks, a broken trend line without reversal follow-through,
or a range breakout without enough acceptance. Name both the old and proposed
read so `transition-unclear` is not a substitute for analysis.

An `mtr-like` label describes a possible major trend reversal structure. In
Brooks' terminology, a major trend reversal has opposing trend segments with a
reversal between them; a minor reversal may be a pullback or countertrend swing
without changing the dominant trend. Neither a visual reversal pattern nor a
trend-line break alone confirms a new trend. A candidate may remain a range or
flag, and the chart may not resolve which it is until more bars form. Require
structural context and observable follow-through before changing the regime.

## Breakout mode and follow-through

Mark recognizable range boundaries and assess balance around them. Breakout
mode is an overlay, not a replacement for regime. Before confirmation, either
direction is possible. A strong bar or brief probe outside a boundary is an
attempt; inspect later closed bars for acceptance, follow-through, or a return
inside. State what evidence would change the classification without predicting
which event will occur.

## Always-In and relevance

Assess the latest decisive bars separately from the broader regime. Convincing
directional pressure can make Always-In long or short, even while the window
still contains a larger range. In a balanced range, use `unclear` when the
forced choice is not supported and set relevance low. A single strong bar can
support only a tentative, low-relevance direction while follow-through is
unknown; it does not establish a trend, high confidence, or an entry.

## Missing facts, uncertainty, and confidence

OHLC bars provide enough evidence for this role to describe bars, swings,
overlap, range boundaries, and follow-through. Per-bar volume is optional and
is not required confirmation. Brooks does discuss volume in selected contexts,
including unusually large volume on daily reversals; when supplied, it can be a
secondary clue for a particular structure. It is not a universal gate, and its
absence alone does not lower confidence. Missing DOM, time-and-sales, volume
profile, footprint, delta, news, indicators, or future bars is not by itself
missing information or a reason to lower confidence.

Use `missing_information` only for concrete unavailable or malformed evidence
that materially affects the requested structural read, such as a missing OHLC
field, incomplete requested history, or absent bars needed to establish a
claimed swing or boundary. Do not mark pre-window bars missing when the
supplied 120-bar window is sufficient for the requested classification. Future
resolution is inherently uncertain, not a missing fact. Describe that
uncertainty in the supporting and opposing evidence, then put the observable
resolution condition in `transition_conditions`. Empty `missing_information`
is valid.

Confidence is categorical support for the current structural classification,
not win odds. Do not emit percentages or probabilities, even when a Brooks
teaching heuristic uses them. Do not add trade recommendations, targets, or
stops. The final rationale should show observable facts, the strongest
alternative, the classification, and change conditions without exposing
hidden chain-of-thought.

## Common failure modes

### Label-first reasoning

Bad: decide `bull-trend`, then search for bullish details.

Better: record visible bar behavior, state the strongest competing read, and
classify after weighing both.

### Indicator substitution

A moving average may be present as optional chart context, but it does not
replace bar structure, swing progression, breakouts, overlap, or follow-through.
Do not mark its absence as missing data.

### Single-bar regime flip

A large bar matters, but it may be one leg inside a range. Check surrounding
structure and whether later bars accept the breakout.

### Range amnesia

Keep the broader range in view while assessing a forceful leg inside it.

### False precision

Categorical confidence is the contract. Do not convert a teaching heuristic or
chart impression into numeric odds.
