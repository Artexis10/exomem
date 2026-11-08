## Why

The outcome suites from sequence 9 cannot show what Exomem is for.

- **f35 (CL-Bench):** the development split needs about a dozen schema facts, and a plain memory folder holds those perfectly. Exomem can tie a folder there, never beat it.
- **f36 (LAB):** a plain session already meets 96% of the rubric, so it can only show that Exomem does no harm.
- **f37 (planted wrong notes):** this one does show the difference. Governance kept the agent from trusting a stale note in 32 of 32 held-out cases, against 24 of 32 without it. But f37 builds the vault for the agent, so it tests only the reading half of the loop.

Exomem's claim is epistemic. It keeps what is current, what was corrected, where a claim came from, whose claim it is, what kind of statement it is, what it rests on and what it is about. A folder of notes keeps none of that. Only a bench where the agent builds its own memory over many sessions, while the knowledge changes, can measure the difference.

Two findings from 2026-11-08 shape the design:

- **Provider drift:** the same commit, model id and CLI version recalled memory in 10 of 10 CL-Bench questions in the morning and in 6 of 10 a few hours later. Arms compared across time windows measure drift, not Exomem. Arms must run side by side.
- **Judging creative answers:** a key match marks a correct but differently worded answer as wrong. Agent answers need a blind judge as well as a key.

## What Changes

- Add family **f38 `epistemic_task_outcome`**. The actor works through scripted sessions on synthetic projects: code and configuration changes, an operations incident, data analysis, a research synthesis and writing. It builds its own memory as it goes. Partway through, the project's knowledge changes. Later probe tasks succeed only if the agent uses the knowledge as it stands now.
- Tag each probe with one **epistemic dimension**:
  - currency over time;
  - correction;
  - provenance;
  - attribution;
  - kind of knowledge;
  - argument structure;
  - causality and dependency;
  - entities and relationships;
  - identity;
  - scope and context;
  - uncertainty and open questions.
  The report gives a score per dimension, so a weak spot shows by name. The dimension of each probe is case data. Once the epistemic-language change makes dimensions registry data, the case files name dimension keys from that registry.
- Add four arms that share every condition except the memory store:
  - **N:** no memory.
  - **F:** a plain memory folder.
  - **P:** Exomem as shipped (plugin, skills, hooks and the learning instruction in the workspace `CLAUDE.md`).
  - **E:** Exomem over MCP alone, an ablation.
  F receives the same user-side learning instruction as P, pointed at its folder, so the bench compares stores, not instructions.
- Run the arms in lockstep. Every arm finishes session k before any arm starts session k+1. A decision run repeats the schedule in a second time window, and the report states each arm's change between windows as its stability.
- Score each probe two ways:
  - A key decides the answer where it is one value, such as a port, a name or a rule.
  - A blind judge, using the f36/f37 view and a JSON schema, decides meaning and behaviour: used the current fact, flagged the conflict, cited the source, kept a claim as the claimant's, declined to invent an answer.
  - The judge re-judges 20% of its items.
- Register f38 through a preregistration amendment, with a dev split of one project and a held-out split of two, before any decision run.

## Impact

- **Benchmarks:** this adds a suite to `benchmarks/membench/outcome/`, reusing the CL-Bench session runner, the f37 judge and the gate. It is one more `membench outcome` suite, not a new command.
- **Case authoring:** an independent lane writes the projects, sessions, probes and keys. That lane never runs, answers or judges. The files stay in evaluator custody outside the repository.
- **Cost:** the estimate is unverified and is restated before each run. A dev round on one project with four arms is about 50 sessions, roughly $20 notional on the subscription. A two-window decision run on two projects is about $160 notional.
- **Product:** no product change. The dimension tags become registry keys when the epistemic-language change lands. The bench does not wait for that change.
