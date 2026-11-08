## Context

`benchmarks/membench/outcome/` already holds the parts f38 needs:

- **A session runner:** the CL-Bench runner keeps a Claude Code actor per arm across a sequence of tasks. It resets the workspace and hook home per task and carries the arm's memory forward.
- **A blind judge:** the f36/f37 judge sees an opaque id, the task, the key and the answer. It answers through a JSON schema.
- **A held-out gate:** it binds released families, frozen input digests and a clean checkout.
- **A paired bootstrap** over units.

f38 combines these parts. Its new parts are the case set, lockstep scheduling and the development gates.

## Decisions

### A project is a sequence of work sessions with planted changes

Each project is one fictional setting with 12 sessions:

- a small software and operations team;
- a research and data project;
- a small business's operations.

A session gives the actor real work: change a config, debug a failing check, summarise three sources, draft a customer email, compute a figure from a CSV. The work introduces facts as a side effect, the way real work does. Nothing tells the actor what to remember. No harness text mentions memory, except the arm's own treatment.

At set points a later session changes what is true. Each change belongs to one dimension:

| Dimension key | Change | What the probe needs |
|---|---|---|
| `currency` | A decision is reversed. | Uses the current version. |
| `correction` | A source refutes an earlier claim. | Does not reuse the refuted claim. |
| `provenance` | Two sources disagree and one is primary. | Uses the primary one and names it. |
| `attribution` | A vendor or colleague asserts something unverified. | Keeps it as that party's claim. |
| `kind` | An assumption is never verified. | Does not treat it as established. |
| `argument` | A claim has evidence on both sides. | Reflects both sides. |
| `dependency` | X depends on Y, and Y changes. | Updates X. |
| `entities` | An owner or supplier changes. | Resolves the right party. |
| `identity` | A system is renamed. | Treats both names as one thing. |
| `scope` | A rule holds for one client only. | Applies it only there. |
| `uncertainty` | A question stays open. | Flags it as open and invents nothing. |

A **change probe** is a task in a later session whose correct outcome depends on one change. Each project carries 2 change probes per dimension, 22 in all.

A **stable probe** asks for a fact from an early session that never changes. Each project carries 4, labelled `stable`, which is a bench label and not an epistemic dimension. Stable probes show how much an arm recalls at all, so the report can separate recall from currency.

### The workspace resets, and only memory carries over

Before each session, every arm's workspace resets to that session's scripted files. The actor's own edits from earlier sessions are gone. The reset is a condition all arms share, as the controls requirement asks.

The case check refuses a probe whose own task or session files contain its key value, an accepted phrasing or its trap, case-folded. A string under four characters, such as a port or a count, is matched as a whole token, and a hit warns instead of refusing, because short values recur innocently. A paraphrased leak passes that check, so the development gate catches it: arm N without history solves the probe.

### Probes do not feed each other

A probe runs as its own task after its session's main task, from a copy of the arm's memory root as that main task left it. The runner discards the probe's memory writes afterwards. Without that, one probe's answer could help or hurt a later probe.

### The folder gets a fair chance

Arm F gets the same learning instruction as P, pointed at its `memory/` folder. That isolates the store from the instruction. A folder that fails here fails because it has no way to say "this replaced that", "this came from there" or "this is about her". It does not fail because nobody told the agent to use it.

### Lockstep and two windows

Provider-side behaviour moved within hours on 2026-11-08, so arms that run at different times cannot be compared.

1. The runner starts all four arms' session k together.
2. It waits until every arm has finished session k and its probes.
3. Then it starts session k+1.

An infrastructure fault, such as a provider error, gets the declared retries. If a session still fails, that project's window is invalid for every arm, and the report lists it with its error. The runner replays that project once in a new window, at least six hours after the other. If the replay also fails, the decision reads incomplete. Product failures, such as an exhausted task budget or a wrong action, stay scored, as the denominator requirement asks.

A decision run plays the whole schedule twice, in two windows that start at least six hours apart.

### The judge decides

The judge sees an opaque id, the probe task, the key (value, accepted phrasings, rubric and trap), the actor's final answer and the files it changed. It never sees the arm, the memory tool calls or the other arms' answers. It answers through a JSON schema:

- `correct`: true or false.
- Each behaviour as true, false or not applicable: `uses_current`, `flags_conflict`, `cites_source`, `keeps_attribution`, `abstains_when_unknown` and `trusts_stale`.
- `reason`: one sentence.

The judge decides correctness. That keeps a correct answer in other words from scoring as wrong.

Before judging, the harness replaces the store names it introduces, the folder path and the product name, with a neutral mark in the answer and the changed files. In a smoke run, every folder and Exomem answer named its store, which would show the judge the arm. The report still counts the raw mentions per arm.

- **Cross-check:** when a probe names a structured answer field, such as `config/deploy.yaml` at `staging.approvals`, the harness reads it with a parser and compares it with the key value. The report lists every disagreement with the judge. In the dev round, the list drives key repairs. In the held-out run, it is description only. If no case names such a field, the harness builds no cross-check and the report says so.
- **Re-judging:** a fresh call of the same judge re-judges a 20% sample. If agreement on `correct` is below 90%, the run reads incomplete. In the dev round, the rubric is repaired. A held-out run has no tiebreaker, because only the frozen decision judge's verdicts are decision evidence. A judge from another family may re-judge a sample as description.
- **Model:** Opus at the sequence-10 judge setting.

### Development gates

The dev round runs the dev project with all four arms in one window. Two gates read every probe:

- **Solvable:** arm N gets the full scripted text of every earlier session's task and files in its prompt. It must solve at least 90% of probes. The author repairs or drops each probe it fails.
- **Memory needed:** arm N from the main dev run, without that text, must fail at least 80% of probes. The author repairs or drops each probe it passes.

The report lists both rates and each probe removed. A repaired probe is checked again before the split freezes.

The gates run on the dev project only. Held-out probes are never gated or dropped after a run, because that would choose cases by their results. The dev round's repair rate is the expected share of flawed held-out probes, and the report states it.

### Statistics

The unit is the probe.

1. The judge scores each probe 1 or 0 per arm and window.
2. Each probe's score per arm is the mean over the two windows: 0, 0.5 or 1. The windows are not independent units.
3. The paired difference per probe is, for example, P minus F.
4. The interval is a 95% bootstrap over probes, resampled within each project, with 10,000 draws and a recorded seed.

The decisions read held-out change probes only:

- **Exomem beats a folder** when the lower bound of P − F is above 0.
- **Exomem helps** when the lower bound of P − N is above 0.
- **Exomem is no worse than a folder** when the lower bound of P − F is above −0.05, the largest loss this protocol accepts.

Description only:

- E against P: what the plugin, skills and instruction add;
- stable-probe differences, and P's gain on change probes beyond its gain on stable probes;
- per-dimension rows, with 4 held-out probes each;
- the behaviours;
- harm: probes that N solves and P fails, with the denominator;
- each arm's per-window score and the difference between windows;
- turns, tokens, cost and wall time per arm.

With 44 held-out change probes, a difference under about 15 points will probably read as no change. That figure is inferred, not computed.

### Cases are authored blind

An independent lane writes the projects, sessions, probes and keys from a brief built on this design. It never runs, answers or judges. The case files live in evaluator custody outside the repository. A seeded draw picks the dev project before anyone reads a case, and the split and case digests are recorded before the dev round.

## Risks

- **Authoring bias:** a probe written to favour typed memory would flatter Exomem. The solvable gate requires every probe to be solvable from the full session text. The bench measures what memory makes cheap and reliable, not what only typed memory makes possible.
- **Judge family:** the judge (Opus) shares the actor's family, as in sequence 10. A Codex sample re-judges when Codex has room.
- **Small samples:** per-dimension rows rest on 4 held-out probes each, so they are description. They point at weak spots and decide nothing.
- **Cost:** the decision run costs roughly $80–100 notional plus the judge. That cost is restated from the dev round and held for the owner's agreement.
