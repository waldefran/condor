# Trade-entry source notes

This Skill is an original, concise operational paraphrase. Official Al Brooks
pages and the user's authorized books ground the price-action terms; none is
copied into the runtime, and no single source is treated as a complete
mechanical rulebook. Book citations use the 1-based PDF page number of the
user-supplied editions, independent of the printed page number.

## Brooks teaching

- [What is price action? 6 aspects to consider](https://www.brookstradingcourse.com/price-action/what-is-price-action-6-aspects/) (Al Brooks, 2020) frames price action across markets and chart types and discusses bars, trends/ranges, support/resistance, and buying/selling pressure. The OHLC-only production packet is a host choice, not a claim that Brooks uses only OHLC.
- [Price action and candlestick charts](https://www.brookstradingcourse.com/how-to-trade-manual/candlestick-charts/) (Al Brooks, 2020) describes a setup as signal plus context, with context in the bars to the left. It warns that an attractive candle by itself can be poor in a tight range. This supports grading signals with location and surrounding action rather than requiring one ideal candle.
- [Beginners should enter with stop orders](https://www.brookstradingcourse.com/how-to-trade-manual/stop-orders/) (Al Brooks, 2020) distinguishes a signal bar from an entry bar and describes stop entries around signal-bar extremes. This supports separating an actionable pending trigger from a fill. The host's exact OHLC references and no-tick-arithmetic rule are local interface constraints, not a claim about Brooks's order-placement convention.
- [10 best price action trading patterns](https://www.brookstradingcourse.com/price-action/10-best-price-action-trading-patterns/) (Al Brooks, 2020) describes flexible pattern variations, High 1/High 2 and Low 1/Low 2 pullback counts, breakouts, range reversals, and major trend reversals. This supports contextual pattern reading and treating a breakout as developing evidence rather than a pattern label that guarantees continuation.
- [Price Action Trading Glossary](https://www.brookstradingcourse.com/price-action-trading-terms-glossary/) defines context through prior bars, buying/selling pressure, and support/resistance; it also defines follow-through, breakout pullback, higher time frame, and High 1/2 and Low 1/2 counts. This is the terminology reference. `H1` as the one-hour frame and the instruction to spell out `High 1` are local disambiguation rules.
- [My day trading setup](https://www.brookstradingcourse.com/how-to-trade-manual/day-trading-setup/) (Al Brooks, 2020) describes his use of 20-bar EMAs as support/resistance context. The prompt must not claim that Al never uses indicators; the host simply does not supply an EMA value to invent.

## User-supplied primary books

- *Reading Price Charts Bar by Bar* (2009), Chapter 1, “Signal Bars: Reversal Bars,” PDF pp. 41–44: a bar is evaluated against earlier bars and its setup; a reversal bar alone is not a reason to enter; a doji or less distinctive signal can be acceptable in context; overlap can show two-sided/range behavior. This supports contextual signal grading instead of a perfect-candle requirement.
- *Reading Price Charts Bar by Bar* (2009), Chapter 10, “Day Trading,” section “Entering on Stops,” PDF pp. 324–325: distinguishes signal and entry bars and describes stop entries beyond a signal-bar extreme. The book uses one-tick offsets; the host's exact OHLC reference and no-tick-arithmetic rule is a local output constraint.
- *Trading Price Action Trends* (2012), Introduction, “Bar Counting Basics,” PDF pp. 65–66: defines High 1/2 and Low 1/2 as successive pullback attempts and notes that patterns can nest. Write the full count names because this host uses `H1` for the one-hour timeframe.
- *Trading Price Action Trends* (2012), Introduction, PDF p. 60, and *Trading Price Action Reversals* (2012), Chapter 10, “Huge Volume Reversals on Daily Charts,” PDF pp. 111–112: unusually high volume may add context to a breakout or a climactic daily reversal, while the reversal chapter cautions against treating intraday volume as a reliable standalone predictor. Together these do not make volume a universal requirement.
- *Trading Price Action Ranges* (2012), Chapter 25, “Mathematics of Trading,” PDF pp. 171–174: describes the Trader's Equation using probability, risk, and reward. This host's contract keeps that check qualitative: do not calculate or emit probabilities, reward/risk estimates, or target fields.
- *Trading Price Action Ranges* (2012), Chapter 27, “Entering on Stops,” PDF pp. 191–192: distinguishes the signal bar from the entry bar and discusses stop entries according to trend strength and context. As above, its tick offsets do not override the host's exact-price rule.

The recorded [Ask Al: Extended trading room discussion](https://www.brookstradingcourse.com/ask-al/trading-room-extended-discussion/) includes an example where Al focuses on the bar and context instead of checking volume. Read that as one example, alongside the books' special volume cases, not as a claim that volume is always irrelevant.

## Project-specific contract

These are host requirements, not claims about Al Brooks's general teaching:

- Use the supplied 120 closed raw H1 and M15 OHLC bars with optional D1/H4
  structural contexts. If H4 context is stale or missing and entry is a
  candidate, require a successful read of 120 closed raw H4 bars before entry.
- Every entry uses decision timeframe M15 and cites both trigger and
  invalidation from exact raw M15 OHLC fields. Preserve the supplied price
  exactly; do not add or subtract a tick.
- OHLC is core. Volume is optional. Missing volume profile, order flow, DOM,
  footprint, news, or indicators cannot veto an entry or become a missing-data
  reason. Never invent an indicator.
- Apply the Trader's Equation only as qualitative entry-quality reasoning.
  The output contract has no probability, risk/reward, or target fields; do not
  add them.
- The Trader alone returns the market-only `TradeIntentV2` decision. No account
  or position state, order write, quantity, stop management, or target is part
  of this role.

The host contract determines output shape and evidence references. Brooks
sources guide interpretation of supplied price action; they do not override
that contract. Only the cited sections of the user-authorized books were
cross-checked. No full-text material or long source extracts are reproduced in
the repository.
