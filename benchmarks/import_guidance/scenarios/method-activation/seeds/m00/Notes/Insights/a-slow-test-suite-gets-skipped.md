---
exomem_id: 248a87b4-8e1a-5462-8356-683e6da61bee
type: insight
status: active
created: 2026-07-12
updated: 2026-07-12
sources: []
projects: [pebble-ledger]
tags: [software, testing]
---

# A slow test suite gets skipped

## Claim

Once the tests take longer than a coffee break, they stop being run before each commit, and bugs return.

## Why it holds

Waiting costs more than the perceived risk of one small change. People batch their changes, then skip the run.

## Where it applies

Side projects most of all, since nobody enforces the habit.

## Counter-considerations

A fast suite that tests too little is no better. Speed matters only while the tests still cover the parts that break.
