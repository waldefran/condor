# Brooks V2 shadow / out-forward measurement

The V2 implementation ends at the existing GM boundary. Run the Trader in
`brooks.shadow_mode: true` against closed venue bars first. Keep each H1
decision cycle, its frozen packet hash, context freshness, intent or typed
failure, and subsequent market bars. A failed role run is a failed cycle, not
`NO_TRADE`. No shadow result authorizes an order.

## Cohort and clock

- Fix the symbol list, connector (`binance_perpetual_demo`), agent keys,
  prompt revision, costs, and start/end dates before evaluating outcomes.
- Use the `decision_time_ms` of the closed H1 bar. Record the latest D1/H4
  context IDs and `current`/`stale`/`missing` state from the frozen Trader
  packet. Keep the H1 and M15 window hashes for audit.
- Count one logical cycle per `(symbol, decision_time_ms, TRADER)` from
  `brooks_state/trader/cycles/*.json`. `pending`, `running`, `retrying`, and
  `failed` are reported separately from completed decisions.
- Compare performance by the context regime *available at decision time*;
  never relabel an old decision using a later analyst result.

## First dashboard

| Measure | Definition |
| --- | --- |
| Decision count | Completed unique H1 Trader cycles. Report failed cycles separately. |
| ENTER / NO_TRADE rate | Count of each decision divided by completed cycles. |
| MFE / MAE | Maximum favorable/adverse move after a **triggered** hypothetical entry and before the fixed exit horizon, in initial risk units. Pending unfilled entries have neither. |
| R result | Net hypothetical exit PnL divided by entry-to-invalidation risk, after fees, funding and slippage. |
| Win rate | Share of filled, resolved entries with net R above zero. |
| Expectancy | Arithmetic mean of net R for filled, resolved entries. |
| Profit factor | Gross positive net R divided by absolute gross negative net R; undefined without losses. |
| Drawdown | Largest peak-to-trough decline of the chronological, net-R equity curve. |
| Costs | Fees, funding and a declared slippage assumption, each reported separately. |
| Regime/context split | The same metrics by D1/H4 regime and freshness present in the frozen packet. |

The shadow evaluator must specify the trigger fill rule, exit horizon, and
same-bar ambiguity rule **before** reading outcomes. Use conservative handling
when a future bar spans both trigger and invalidation. Do not count an unfilled
pending trigger as a loss or a win. Keep raw outcomes beside any aggregate so
the assumptions can be changed without revising past decisions.

The first live capture and initial observed counts belong in
[`brooks_v2_prompt_capture.md`](brooks_v2_prompt_capture.md). MFE, MAE and R
remain unavailable until the defined forward window has elapsed.
