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

## Action evidence

- **HOLD:** state is coherent, protection is adequate, and no supplied
  management condition requires intervention. Mention the risk that remains.
- **PROTECT / MOVE_PROTECTION:** identify the uncovered quantity or the
  observable reason protection can move. Moving protection should reduce
  unmanaged risk; widening it needs explicit authority and evidence.
- **TAKE_PARTIAL / REDUCE:** state the affected position and quantity or
  fraction, expected exposure change, and execution precondition. Do not
  count a partial fill twice.
- **CLOSE / CLOSE_ALL:** identify the position scope and why continued
  exposure is no longer acceptable. Distinguish a single-position close from
  a global close.
- **CANCEL_ORDER / REPLACE_ORDER:** name the stale, duplicate, rejected, or
  unsafe order and state how the replacement preserves required protection.
- **RECONCILE_STATE:** use when fills, quantities, orders, or hedge legs do not
  agree. Reconciliation is an action, not permission to invent a fill.

## Hedge discipline

A hedge is a temporary exposure-management tool, not a loss accounting trick.
Its plan must answer:

| Required field | Question |
|---|---|
| objective | What risk is being reduced, and why now? |
| size | How much is added, reduced, or removed? |
| expected effect on exposure | What is the net exposure before and after? |
| costs | What fees, funding, spread, margin, and liquidation interactions matter? |
| unlock condition | What observable event permits reducing/removing the hedge or closing the base leg? |
| failure condition | What makes the hedge invalid, too costly, or unsafe? |

If no unlock condition can be stated, do not use a hedge as a default answer.
If a new market reading is needed to choose the hedge direction or duration,
request an independent Trader report first. The report informs management; it
does not authorize an automatic hedge.

## Opposing case

The PM should record the strongest reason to choose a different action. Examples
include a stop that already covers the risk, an intervention that increases
fees/funding without changing net exposure, an order state that may be stale,
or a discretionary decision that lacks a current market report. This field is
for uncertainty and auditability, not for a keyword checklist.
