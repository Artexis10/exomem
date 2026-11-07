## Why

Exomem's benchmarks measure recall, lifecycle and seeded utility episodes. None of them asks the question an owner asks first: does an agent do real tasks better over sessions with Exomem than with a plain folder or no memory? None of them asks whether governance stops an agent from trusting a stale or wrong note. Public suites now exist for the first question. CL-Bench measures task outcome over a sequence of sessions with a schema change halfway through. Harvey LAB measures multi-document synthesis against expert pass/fail criteria. A planted wrong-note suite answers the second question by holding page text fixed and varying only governance.

The owner asked that these suites become first-class and repeatable on the subscription, with a simple interface over the machinery.

## What Changes

- Add `benchmarks/membench/outcome/` with three suites: `clbench`, `lab` and `planted`. Each suite drives headless Claude Code actors on the subscription through the shared argv builder and the existing isolation helpers.
- Add one command group: `membench outcome fetch`, `run <suite>`, `report <suite>` and `panel`. Pin the upstream commits in one lock file.
- Register families f35 to f37 through amendment receipt sequence 9. The receipt freezes the held-out inputs by SHA-256. A held-out run calls the release gate and checks those digests before its first session.
- Label every result an internal diagnostic. Development and panel results are instrument evidence; only held-out results under an acknowledged amendment are decision evidence.
- Record a release panel per installed release under `docs/benchmarks/outcome/`, paired against the previous release. The panel is evidence, never a gate.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `epistemic-utility-regression`: add outcome suites, their single command, the held-out gate, the internal-diagnostic label, actor and judge isolation, and the release panel. The existing paired-outcome, adverse-outcome and identity rules apply unchanged.

## Impact

The change adds code under `benchmarks/membench/outcome/`, a CLI group in `benchmarks/membench/cli.py`, the sequence-9 entry in `benchmarks/epistemic/PREREGISTRATION.md`, its receipt, the registry mapping and tests. It changes no product code under `src/`. Case files stay outside the repository. The decision runs wait for the owner's acknowledgment of the receipt and for subscription room; they are measurement, not tasks of this change.
