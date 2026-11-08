## Context

`benchmarks/membench/outcome/` already holds the parts f38 needs:

- **A session runner:** the CL-Bench runner keeps a Claude Code actor per arm across a sequence of questions. It resets the workspace and hook home per question and carries the arm's memory forward.
- **A blind judge:** the f36/f37 judge sees an opaque id, the task, the key and the answer. It answers through a JSON schema.
- **A held-out gate:** it binds released families, frozen input digests and a clean checkout.
- **A paired statistic:** a per-unit bootstrap and Newcombe's interval for paired proportions.

f38 combines these parts. Its new part is the case set: scripted projects whose knowledge changes.

## Decisions

### A project is a sequence of work sessions with planted changes

Each project is one fictional setting with 12 sessions:

- a small service fleet with its configuration and an on-call rota;
- a research and data project;
- a small business's operations.

A session gives the actor real work: change a config, debug a failing check, summarise three sources, draft a customer email, compute a figure from a CSV. The work introduces facts as a side effect, the way real work does. Nothing tells the actor what to remember.

At set points a later session changes what is true. Each change belongs to one dimension:

- **Currency:** a decision is reversed.
- **Correction:** a source refutes an earlier claim.
- **Provenance:** two sources disagree and one is primary.
- **Attribution:** a vendor states a figure.
- **Kind:** an assumption is never verified.
- **Argument structure:** a claim has evidence on both sides.
- **Dependency:** a service another depends on moves.
- **Entities:** a team changes owner.
- **Identity:** a system is renamed.
- **Scope:** a rule applies to one client only.
- **Uncertainty:** a question stays open.

A probe is a task in a later session whose correct outcome depends on that change. Each project carries 2 probes per dimension, 22 probes in all.

### The folder gets a fair chance

Arm F gets the same learning instruction as P, pointed at its `memory/` folder. That isolates the store from the instruction. A folder that fails here fails because it has no way to say "this replaced that", "this came from there" or "this is about her". It does not fail because nobody told the agent to use it.

### Lockstep and two windows

Provider-side behaviour moved within hours on 2026-11-08, so arms that run at different times cannot be compared. The runner advances all four arms one session at a time. A decision run plays the whole schedule twice, in two windows at least six hours apart. The paired statistic pools both windows. The report gives each arm's per-window score and the difference between windows.

### Key and judge

A probe's key holds the expected value where one exists, and a rubric of behaviours otherwise.

- **Key match:** a deterministic match scores a single value.
- **Judge:** the blind judge scores semantic correctness for answers that are not a single value, and scores the behaviours:
  - `uses_current`;
  - `flags_conflict`;
  - `cites_source`;
  - `keeps_attribution`;
  - `abstains_when_unknown`;
  - `trusts_stale`.
- **What decides:** for a probe with a key value, the key decides correctness. The judge's correctness verdict is reported beside it, and disagreements are listed for review. That catches a key too narrow for a correct but creative answer, and that list feeds key repairs in the next dev round.
- **Re-judging:** the judge re-judges 20% of items.

### Decisions and description

The paired unit is the probe, clustered by project.

- **Exomem beats a folder** when the lower bound of P − F on probe correctness is above 0.
- **Exomem helps** when the lower bound of P − N is above 0.
- **E against P** shows what the plugin and the instruction add.
- **Description only:** per-dimension rows, the behaviours, stability between windows, and turns, tokens and cost per arm.

### Cases are authored blind

An independent lane writes the projects, sessions, probes and keys from this design and the dimension list. It never runs, answers or judges. The case files live in evaluator custody outside the repository. The split is one project for dev and two held out, and it is fixed before any dev round.

## Risks

- **Authoring bias:** a probe written to favour typed memory would flatter Exomem. The rule is that every probe must be solvable by an agent that reads every earlier session in full. The bench measures what memory makes cheap and reliable, not what it alone makes possible. Arm N with the full transcript is a check on that rule during dev.
- **Judge family:** the judge (Opus) shares the actor's family, as in sequence 10. A Codex sample re-judges when Codex has room.
- **Cost:** a two-window decision run costs about $160 notional. That cost is stated before the run and held for the owner's agreement.
