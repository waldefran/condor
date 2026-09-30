# Trade-entry evidence map

Use this compact map when the supplied bars leave a material entry distinction
unclear. It is an operational synthesis of Al Brooks price action, not a
deterministic rule set. Start from the bars; pattern names summarize evidence
and do not replace it.

## Evidence sequence

1. **Observe.** State what the closed bars show: direction and close location,
   overlap and tails, swing sequence, pullback depth, attempts beyond a prior
   boundary, and any follow-through. Cite the actual bar references.
2. **Locate.** Decide whether surrounding action is behaving like a trend,
   trading range, or transition. Place the candidate against nearby swing highs
   and lows, range edges, breakout points, and current supplied D1/H4 context.
3. **Grade the setup.** Evaluate the signal bar with its left-side context and
   the available follow-through. A textbook-looking candle cannot repair a
   poor premise or poor location; a less distinctive bar may be acceptable in
   strong context. Do not invent follow-through beyond the decision time.
4. **Challenge it.** Give the strongest opposite case its own evidence. Check
   failed breakout, trapped traders, deep or persistent pullback, opposing
   closes, late extension, overlap, and nearby resistance/support.
5. **Resolve actionability.** Name an exact trigger and structural invalidation
   from raw M15 OHLC for an entry. An actionable pending stop may qualify before
   a fill. Keep pending distinct from triggered, and forming distinct from
   failed. A pattern is failed only after the bars show its failure.

## Trend, range, and location

- In a trend, a pullback can be a flag and the trend may resume. Assess whether
  the countertrend leg is weak or persistent, whether the resumed direction has
  a usable signal, and whether price is already extended or facing nearby
  opposing structure.
- In a trading range, consider reversals and failed breakouts near the edges.
  A breakout bar is an attempt; subsequent acceptance or follow-through gives
  it more weight. A return into the range with opposing pressure can support a
  failure read, but do not label an untested or still-forming attempt failed.
- The middle of a range is usually less favorable than an edge. A strong bar
  can still be a poor entry if price is near resistance/support, late in an
  extended move, or directly into opposing structure.
- Make a qualitative Trader's Equation check: does the candidate have an
  acceptable location and defensible invalidation without being crowded by
  nearby opposing structure? Do not estimate probabilities, reward/risk
  multiples, or targets; the host contract has no fields for them.
- Higher-timeframe disagreement is evidence about location and room for the
  setup to work. It is not an automatic veto. Explain how it changes the
  strongest opposing case and choose from the complete evidence.

## Brooks signal counts and timeframes

Use full labels `High 1`, `High 2`, `Low 1`, and `Low 2`. In a bull-flag
pullback, the first bar whose high exceeds the prior bar is a High 1 attempt;
if price continues sideways or down instead of turning into a bull swing, the
next occurrence of a bar with a higher high is a High 2 attempt. In a bear-flag
pullback, the first bar with a lower low is a Low 1 attempt; if price continues
sideways or up, the next occurrence of a lower low is a Low 2 attempt. These
are contextual counts, may be nested, and do not by themselves prove the setup
is good or failed. In this host, `H1` means the one-hour timeframe, so never
abbreviate a High 1 setup as `H1`.

## Trigger and failure status

- `pending` means a concrete, actionable stop entry is defined but not yet
  triggered. It does not mean a fill occurred.
- `triggered` or `present` means the entry condition is active in the supplied
  closed-bar facts. A candidate does not claim an order was actually filled.
- `absent` means no actionable trigger is supported. `unknown` means the
  supplied packet cannot establish its status.
- `failed` requires price action that shows the attempted setup or breakout
  failed. `stale` requires evidence that a once-actionable setup is no longer
  actionable. A trigger that has not fired is not, by itself, failed or stale.
- Do not force a follow-through bar to exist. A pending stop can be actionable
  from the signal and its context before later follow-through is available;
  assess later follow-through only if it is inside the frozen packet.
- The host accepts trigger and invalidation references only at exact supplied
  M15 OHLC fields. Do not add/subtract a tick or derive a price. This is a host
  contract and does not change how Al Brooks describes example stop orders.

## Source basis

The author and source distinctions are documented in
[`source-notes.md`](source-notes.md). Use these ideas as evidence questions,
not as hard-coded scoring rules.
