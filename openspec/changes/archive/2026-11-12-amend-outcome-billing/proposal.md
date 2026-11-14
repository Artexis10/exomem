## Why

The outcome suites paid decision runs from the Claude subscription only and authorized no API or credit spend. That rule limited spend; it was not a rule of method, because the subscription login and Claude API credits serve the same frozen models. On 2026-11-11 the owner ruled that both billing sources give valid evidence, with credits preferred when an account has room.

## What Changes

- A held-out outcome run may be billed to the Claude subscription or to Claude API credits and still decide. The run declares its source, and every session records the source it reports.
- A session that reports a source other than the declared one is a harness fault. A credit balance that runs out stops the run as a rejected limit does.
- Claude judgement files from before billing was recorded read as subscription, marked inferred.
- Amendment sequence 11 records the rule. Until the owner acknowledges it, a run billed to credits stays instrument evidence.

## Capabilities

### Modified Capabilities

- `epistemic-utility-regression`: the billing rule for outcome actors and judges, and its effect on the evidence class.

## Impact

The change touches `benchmarks/membench/outcome/` and `benchmarks/epistemic/` only. No product code under `src/` changes.
