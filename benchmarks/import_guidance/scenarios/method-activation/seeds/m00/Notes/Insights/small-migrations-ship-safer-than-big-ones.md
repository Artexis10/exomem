---
exomem_id: d30299c1-6b53-58bc-8d9e-25e974205631
type: insight
status: active
created: 2026-05-29
updated: 2026-05-29
sources: []
projects: [pebble-ledger, fernwatch]
tags: [software, migrations]
---

# Small migrations ship safer than big ones

## Claim

A schema change split into several small, separately released migrations fails less often and is easier to undo than one large rewrite.

## Why it holds

Each small step can be tested against a copy of real data and rolled back alone. A big rewrite hides several risks behind one release, and a failure leaves no clear place to stop.

## Where it applies

Any change to the SQLite file of Pebble Ledger or the Postgres tables behind Fernwatch.

## Counter-considerations

Several small steps need more releases and a little more bookkeeping.
