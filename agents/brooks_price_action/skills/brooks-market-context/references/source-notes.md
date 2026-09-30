# Source Notes

This skill is an original, concise operational synthesis of Al Brooks price
action teaching. These official Brooks Trading Course pages are the primary
sources used to cross-check the concepts; this file paraphrases them rather
than copying manual passages.

## Official Al Brooks sources

- [Price action and candlestick charts](https://www.brookstradingcourse.com/how-to-trade-manual/candlestick-charts/) — Al defines price action broadly as movement on a chart and stresses context around a bar pattern.
- [What is price action? 6 aspects to consider](https://www.brookstradingcourse.com/price-action/what-is-price-action-6-aspects/) — Al discusses price movement across chart types and timeframes, trend/range context, support and resistance, and the limits of indicator-heavy charts.
- [The surefire folly of trading with Technical Analysis Indicators](https://www.brookstradingcourse.com/how-to-trade-manual/technical-analysis-indicators/) — Al argues that price action is the main evidence and says even the 20-bar EMA is not necessary to trade profitably.
- [My day trading setup](https://www.brookstradingcourse.com/how-to-trade-manual/day-trading-setup/) — Al also describes using 20-bar EMAs on his intraday charts. Do not misstate the prior page as a universal claim that he never uses indicators.
- [Trading ranges](https://www.brookstradingcourse.com/how-to-trade-manual/trading-ranges/) — Al explains why strong legs inside a trading range can be breakout attempts without sustained follow-through.
- [Learn how to trade trend channels](https://www.brookstradingcourse.com/futures-market/trade-trend-channels/) — Al distinguishes spikes from weaker channels and describes channels evolving toward range-like, two-sided behavior.
- [Price Action Trading Glossary](https://www.brookstradingcourse.com/price-action-trading-terms-glossary/) — official terminology, including price action, trading ranges, and Always-In.
- [Ask Al: Extended trading room discussion](https://www.brookstradingcourse.com/ask-al/trading-room-extended-discussion/) — a Brooks Trading Course hosted transcript attributed to Al. In it he describes inferring volume from bar size and context rather than checking each bar's volume.
- [Ask Al: Trading with Time & Sales](https://www.brookstradingcourse.com/pt-br/ask-al/trading-time-sales-benefit/) — official transcript of Al's answer about Time & Sales and DOM; he describes chart reading and support/resistance as sufficient for his approach.

## User-provided primary books checked

The book citations below refer to the user-provided PDFs. PDF page numbers are
1-based physical pages from the form-feed-separated text extractions; printed
page numbers are included where visible in the book or its table of contents.
The notes are original paraphrases, not copied book text.

- *Reading Price Charts Bar by Bar: The Technical Analysis of Price Action for the Serious Trader* (2009), Chapter 1, “Price Action,” PDF pp. 28–29 (the chapter opens at printed p. 1; the page number itself is not surfaced in the extraction): Brooks defines price action as price change on any chart type or timeframe. This supports looking at chart movement directly; OHLC-only sufficiency remains the project's narrower input choice.
- The same book, Preface, PDF p. 20 (printed page not surfaced in the extraction): Brooks describes using a 20-bar EMA, says most 1-minute volume is too unreliable to guide his trades, and notes exceptional volume spikes can matter in specific settings, including overdone daily selloffs. This is why the project treats supplied volume as optional secondary context, not forbidden data or a required gate.
- *Trading Price Action Trends: Technical Analysis of Price Charts Bar by Bar for the Serious Trader*, “List of Terms Used in This Book,” PDF pp. 15–16 (printed pp. xiii–xiv): defines Always-In and breakout mode. The project keeps Always-In structural and adds a separate relevance field.
- The same book, Chapter 1, “The Spectrum of Price Action: Extreme Trends to Extreme Trading Ranges,” PDF pp. 87–88 (printed pp. 55–56), and Chapter 3, “Breakouts, Trading Ranges, Tests, and Reversals,” PDF pp. 109–110 (printed pp. 77–78): price behavior spans trends and two-sided ranges; a breakout needs to be assessed with what follows it.
- The same book, Chapter 15, “Channels,” PDF pp. 251–252 (printed pp. 219–220): distinguishes a sloped trend channel from a horizontal trading range, while noting that the boundary can be less clear when a channel is weak or broad.
- The same book, Chapter 21, “Spike and Channel Trend,” PDF pp. 357–358 (printed pp. 325–326), and Chapter 22, “Trending Trading Range Days,” PDF pp. 391–392 (printed pp. 359–360): treats spike and channel as different trend phases and describes trend days that include trading-range structure. A spike can establish immediate directional pressure, while the broader sustained-trend assessment still depends on surrounding structure.
- *Trading Price Action Ranges: Technical Analysis of Price Charts Bar by Bar for the Serious Trader*, Chapter 11, “First Pullback Sequence: Bar, Minor Trend Line, Moving Average, Moving Average Gap, Major Trend Line,” PDF pp. 82–83 (printed pp. 66–67, inferred from the introduction's printed p. 1 at PDF p. 17): pullbacks can be small trading ranges on the current chart and can grow more two-sided as a trend weakens.
- The same book, Chapter 21, “Example of How to Trade a Trading Range,” PDF pp. 147–148 (printed pp. 131–132, inferred from that same pagination offset): strong legs can remain excursions inside a range when they lack follow-through. Chapter 22, “Tight Trading Ranges,” PDF pp. 151–152 (printed pp. 135–136, inferred): emphasizes overlap and two-sided behavior while a breakout is unresolved.
- *Trading Price Action Reversals: Technical Analysis of Price Charts Bar by Bar for the Serious Trader*, Introduction, PDF pp. 31–32 (printed pp. 16–17, inferred from printed p. 1 at PDF p. 16): distinguishes a reversal in behavior from a confirmed trend reversal; many attempts become trading ranges, and the eventual outcome may not be clear for many bars.
- The same book, Chapter 2, “Signs of Strength in a Reversal,” PDF p. 46 (printed p. 31, inferred), and Chapter 3, “Major Trend Reversal,” PDF pp. 47–48 and 51–52 (printed pp. 32–33 and 36–37, inferred): a major reversal is distinct from a minor pullback or swing; a trend-line break or reversal-shaped pattern alone does not establish a new trend. A trading range is a more common early outcome than an immediate sustained trend in the opposite direction.
- The same book, Chapter 10, “Huge Volume Reversals on Daily Charts,” PDF pp. 111–112 (printed pp. 96–97, inferred): treats exceptional volume as relevant to a particular daily reversal case while warning that intraday volume is not a dependable general predictor. This does not make volume a required confirmation for ordinary structural context.

## Teaching versus project implementation

Brooks' sources inform the price-action concepts. The following are project
choices in this context role, not claims about Brooks' prescribed method:

- One D1 or H4 production context window with 120 closed OHLC bars and the
  `MarketContextV2` / `CONTEXT_ANALYST` output contract.
- OHLC is the required structural evidence. Per-bar volume is optional; its
  absence is not a necessary-confirmation failure. The project does not require
  DOM, time-and-sales, volume profile, footprint, delta, news, indicators, or
  future bars for this structural read.
- `missing_information` names only concrete unavailable facts material to the
  task; unknown future resolution is uncertainty, not a missing fact. Empty
  `missing_information` is valid. When the complete 120-bar window suffices,
  older pre-window history is not missing by default.
- Categorical confidence reports support for the structural classification.
  The output does not include numeric probabilities, trade actions, targets,
  stops, or cross-timeframe voting.

Do not attribute these schema and role rules to Al. Brooks does use probability
heuristics in his teaching and describes using EMAs in some chart setups; this
project keeps those separate from the context analyst's output contract.
