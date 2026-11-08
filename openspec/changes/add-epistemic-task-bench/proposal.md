## Why

The outcome suites from sequence 9 cannot show what Exomem is for.

- **f35 (CL-Bench):** the development split needs about a dozen schema facts, and a plain memory folder holds those perfectly. Exomem can tie a folder there, never beat it.
- **f36 (LAB):** a plain session already meets 96% of the rubric, so it can only show that Exomem does no harm.
- **f37 (planted wrong notes):** this one does show the difference. Governance kept the agent from trusting a stale note in 32 of 32 held-out cases, against 24 of 32 without it. But f37 builds the vault for the agent, so it tests only the reading half of the loop.

Exomem's claim is epistemic. It keeps what is current, what was corrected, where a claim came from, whose claim it is, what kind of statement it is, what it rests on and what it is about. A folder of notes keeps none of that. Only a bench where the agent builds its own memory over many sessions, while the knowledge changes, can measure the difference.

Two findings from 2026-11-08 shape the design:

- **Provider drift:** the same commit, model id and CLI version recalled memory in 10 of 10 CL-Bench questions in the morning and in 6 of 10 a few hours later. Arms compared across time windows measure drift, not Exomem. Arms must run side by side.
- **Judging creative answers:** a key match marks a correct but differently worded answer as wrong. A blind judge that sees the key must decide correctness.

## What Changes

- Add family **f38 `epistemic_task_outcome`**. The actor works through 12 scripted sessions per synthetic project: code and configuration changes, an operations incident, data analysis, a research synthesis and writing. It builds its own memory as it goes. Partway through, the project's knowledge changes. Later probe tasks succeed only if the agent uses the knowledge as it stands now.
- The workspace resets before every session to that session's scripted files, the same in every arm. Only the arm's memory carries anything from one session to the next.
- Each project carries 22 **change probes**, two per epistemic dimension, and 4 **stable probes** that ask for facts that never change. Stable probes separate "remembers at all" from "remembers what is current". The dimension keys are the shipped keys that the epistemic-language change names: `currency`, `correction`, `provenance`, `attribution`, `kind`, `argument`, `dependency`, `entities`, `identity`, `scope` and `uncertainty`.
- Four arms differ only in the memory treatment:
  - **N:** no memory.
  - **F:** a plain memory folder.
  - **P:** Exomem as shipped (plugin, skills, hooks and the learning instruction in the workspace `CLAUDE.md`).
  - **E:** Exomem over MCP alone, an ablation.
  F receives the same user-side learning instruction as P, pointed at its folder, so the bench compares stores, not instructions.
- The runner advances the arms in lockstep: all four start session k together, and none starts session k+1 before all have finished k. A decision run repeats the schedule in a second time window.
- A blind judge decides each probe's correctness from the task, the key and the answer, and scores the epistemic behaviours. A parser-read match on a structured answer field, such as a port in a config the probe asks for, is a cross-check only.
- A development round gates every probe: arm N with the full earlier-session text must solve it, and arm N without that text must fail it.
- A preregistration amendment registers f38, with one dev project and two held-out projects, before any decision run.

## Impact

- **Benchmarks:** this adds one `membench outcome` suite to `benchmarks/membench/outcome/`. It reuses the CL-Bench session runner, the f37 judge and the gate.
- **Case authoring:** an independent lane writes the projects, sessions, probes and keys. That lane never runs, answers or judges. The files stay in evaluator custody outside the repository.
- **Cost:** the estimate is unverified and is restated from the dev round's measured cost before each run.
  - Dev round: 48 sessions and about 130 probe tasks, roughly $20–25 notional on the subscription.
  - Decision run: 2 held-out projects × 12 sessions × 4 arms × 2 windows is 192 sessions, plus 416 probe tasks. That is roughly $80–100 notional, plus the judge.
- **Product:** no product change. The bench does not wait for the epistemic-language change.
