## Why

Development rounds on 2026-11-08 changed two premises of the outcome suite. Every CL-Bench memory system receives the benchmark's per-task memory sentence, but the shipped Exomem arm did not, and without it Exomem saved nothing in 50 sessions. The owner decided to run both framings, to make the told arm the primary comparison against the folder, and to stop waiting for a cross-family judge.

## What Changes

- The primary Exomem arm stays the product as shipped, with no instruction written for the benchmark, except that where a benchmark gives every memory system a per-task memory sentence, the primary arm also receives it. The arm without the sentence is reported as the product gap.
- The f36 and f37 decision judge may come from the actor's model family. Every report then names the judge family, and a cross-family judge re-judges a sample when it has room.
- Amendment sequence 10 records the arms, the shrunk f35 deciding run and the judge.

## Capabilities

### Modified Capabilities

- `epistemic-utility-regression`: the primary-arm and decision-judge rules of the outcome suites.

## Impact

The change touches `benchmarks/membench/outcome/` and `benchmarks/epistemic/` only. No product code under `src/` changes.
