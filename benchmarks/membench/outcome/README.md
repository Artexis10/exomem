# Outcome evaluations

These suites ask one question: does Exomem change what an agent gets done? Each plays a
Claude Code actor, on a subscription login or on API credits, against a task whose score does not depend on
Exomem. Every figure they produce is an **internal diagnostic**. The actor is not the suites' own
protocol, so no number here may be quoted as a CL-Bench or LAB result.

## The suites

- **clbench** is CL-Bench `database_exploration` (upstream scoring, family f35). The actor answers
  a sequence of questions about an unknown database. Arm `N` keeps nothing between questions. Arm
  `F` keeps a plain memory folder. Arm `PP` is the primary Exomem arm: Exomem as its users get it,
  with CL-Bench's per-task memory sentence pointed at Exomem. Arm `P` is `PP` without that
  sentence, so the pair `PP` and `P` differs only by the sentence. Both have the shipped Claude
  plugin (skills and hooks) at its default prominence, the documented balanced custom-instruction
  block, the Skill tool and the full Exomem tool surface. Arm `P` measures the product gap that a
  capture change must close, and only describes. Arm `E` is an ablation: Exomem over MCP alone,
  with the same sentence in the task prompt. Arm `EH` is a diagnostic: `E` plus the plugin. The
  suite answers whether memory lowers the queries and raises the score across a sequence, and
  whether Exomem beats a folder. The arms run in the order `N`, `F`, `PP`, `P`, `E`, `EH`.
- **lab** is Harvey LAB multi-document synthesis (family f36). The actor writes one deliverable per
  matter. Arm `S` reads the documents. Arm `WP` is Exomem as its users get it, the primary Exomem
  arm: a compile session builds a vault from the documents, then a fresh task session writes the
  deliverable. Both sessions get the plugin, the balanced block, the Skill tool and the full
  Exomem tool surface, and the task prompt says nothing about memory. Arm `W` is an ablation:
  Exomem over MCP alone, with the task prompt telling the actor its memory holds notes. Arm `WG`
  is `WP` with the graph index off, run on a copy of `WP`'s compiled vault, an ablation. A blind
  judge scores each deliverable against the task's own criteria. The suite answers whether a
  compiled vault helps on long, multi-document work.
- **planted** is the planted wrong-note suite (family f37). A vault holds a stale or wrong note and
  its correction. Arm `E` records the correction as Exomem governance. Arm `P` holds the same pages
  without it. A blind judge reads each answer against the case key. The suite answers whether
  governance keeps the agent from trusting the stale value.
- **epistemic** is the epistemic task bench (family f38, dev only until its amendment registers
  it). The actor works through a scripted project of several sessions, and probes then ask what
  the memory should now hold. Arm `N` has no memory and plays probes only. Arm `G` has no memory
  either, plays probes only and reads each probe's contest set in its prompt. Arm `F` keeps a
  `memory/` folder and gets the product's learning instruction, pointed at the folder. Arm `P` is
  Exomem as its users get it:
  the plugin, the skills, the hooks, the instruction block and the learning instruction. Arm `E`
  is an ablation: Exomem over MCP alone, with no instruction. No arm gets a per-task memory
  sentence. The suite answers whether Exomem beats a folder on facts that change, and whether it
  helps at all.

The plugin arms (`PP`, `P`, `EH`, `WP`, `WG`) run the plugin's hooks with no service credential. The
hooks keep their state inside the arm directory. The report records the characters the hooks
injected, and any tool the CLI refused for want of permission. Each CL-Bench question and each
LAB session starts with a fresh hook home, so a hook's cooldown from an earlier question cannot
silence its nudge. A plugin arm may read the plugin directory, so a skill can read its own
reference files, and nothing else outside its workspace.

## The epistemic suite

The cases are three projects, `project-a`, `project-b` and `project-c`, with `SPLIT.json` beside
them. They sit under `cases/epistemic/` in the run root and never go in the repository.
`SPLIT.json` names the `dev` and `heldout` projects and the SHA-256 of each project file. A run
refuses a project file that differs from its digest.

A project is a list of sessions (a task and its files) and a list of probes. A probe belongs to
one session and carries a task, files, and a key: the value, the accepted phrasings, a rubric, the
behaviours it scores and the trap. A probe of the dimension `stable` asks for a fact that never
changes. Every other probe is a change probe and names its `change_id`.

A key may also carry `contest`: a list of `{"session": n, "file": name}` entries, each naming a
scripted file of the probe's session or an earlier one. Only arm `G` reads it. The hand-written
projects have none, so `--arms` leaves `G` out by default and a run that asks for `G` on a project
with a probe that has no `contest` is refused before any session starts.

A generated development project comes from the synthetic world. `projection.generate(root, seed)` in
`epistemic/projection.py` writes the world with its keys under `root/world` and the project under
`root/cases/epistemic`, with the `SPLIT.json` that names it as the dev project. Its sessions are the
weeks of the world's timeline. A probe also holds its `family`. It is asked in the session of its
date, which the planner keeps free of the key's things. Its label is the
dimension of its family in `benchmarks/membench/projections/exomem.yaml`, or the family's own key when
the product ships no dimension. The report builds a row for each label, so a family key is a row of
description. The project goes under a temporary root, never into the repository.

How a window plays:

1. The command plays one project in lockstep. Every arm starts session `k` together. Session
   `k + 1` starts only after every arm finished session `k` and its probes. Arms `N` and `G` play
   no main task: they have no memory, so the task would change nothing that a later call sees.
2. Before each session the workspace holds only that session's scripted files. Arm `F` keeps its
   `memory/` folder. Arms `P` and `E` keep their vault and state.
3. After an arm's main task, each probe of that session runs as its own `claude -p` call. It
   starts from a snapshot of the memory taken after the main task. The harness restores the
   snapshot after every probe, so a probe never writes into memory that a later call reads. The
   prompt of arm `G` holds the files of the probe's `contest` in session order, under a neutral
   header, and then the probe's task. It holds no other file and no task of an earlier session.
4. A harness fault that survives its retries invalidates that project's window for every arm. The
   project stops and `state.json` records the error. A product failure (a timeout, a wrong
   answer) stays scored.

Run one window with `membench outcome run epistemic --split dev --run-id r1 --window 1`. The
window number can be any whole number from 1. `--project project-b` plays one project, for example
to replay an invalid window in a new window number. Run `--history full --arms N` to play the
solvable gate: arm `N` answers each probe with the scripted tasks and files of sessions 1 to the
probe's session in its prompt, under a neutral header. This gate runs on the dev split only.

Before any run the case check compares every authored key string (the value, each accepted
phrasing and the trap) with the probe's task, the probe's files and its session's files. A string
of four or more characters refuses the case set when it appears there, case-folded. A shorter
string matches whole tokens only and gives a warning in the run record, not a refusal. The check
reads no meaning: it finds copied strings, and the solvable and memory-needed gates find
paraphrases.

`membench outcome report epistemic --run-id r1` judges every answer blind and writes the report.
The judge sees an opaque id, the probe task, the key, the answer and a diff of the files that the
call changed. It does not see the arm or the memory tool calls. A seeded 20% of the items are
judged twice. If the two judgements agree on `correct` in less than 90% of the items, the report
reads `incomplete: judge agreement X`. No other judge breaks that tie. The harness has no parser
cross-check of the judge, and the report says so. Before it judges, the report refuses a case file
whose digest differs from the digest that the run recorded.

The judge reads each token that names a memory store (`memory/...` or the product name) as `[store]`, so it
cannot read the arm from those names. Case text may not contain one; the case check refuses it. A run judged
before this masking holds unmasked items: move its judge folder aside and run `report` again to re-judge it.

The score of a probe for an arm is the mean of `correct` over the project's counted windows. A
project counts its first two valid windows. The pairs `P-F`, `P-N` and `P-E` use change probes
only; `P-E` shows what the plugin, skills and instruction add to the MCP server alone. Each reports the mean difference and a 95% percentile bootstrap interval over probes,
resampled inside each project (10,000 draws, the seed is in the report). A decision reads a
held-out run only, and only when every project has two valid windows that start at least 6 hours
apart. Each project has windows 1 and 2 and at most one replay: a project with more than one
invalid window, or with more than three windows, makes the decision `incomplete`, and so does a
gap under 6 hours. Only a new run fixes either. The decision otherwise reads `beats folder` when the lower bound of `P-F` is above 0, `helps`
when the lower bound of `P-N` is above 0, and `no worse than folder` when the lower bound of `P-F`
is above the not-worse bound. A dev run only describes, and `decision.why` says so. A project
with fewer than two valid windows makes the decision `incomplete` and names the project. A dev run adds two gates: arm `N` with the whole history solves at least 90% of the
probes, and arm `N` without it fails at least 80%. Each gate lists the probes that fail it. The
description rows include the share of answers per arm that name a memory store (`memory/` or the
product name), because the blind judge reads the answer text.

A held-out epistemic run refuses today: f38 is not registered in the preregistration, so no gate
can pass it.

## Incident regressions

`python -m membench.outcome.incidents` replays product incidents on real models. Each file under
`incidents/cases/` holds one incident's cases: a seeded note, one user turn, and the checks that
pass it. The checks are `seed_corrected` (the seeded note's body changed, or the product reports it
superseded), `called_any` (an Exomem tool was called) and `judge` (a blind model's schema fields
match). An arm is a checkout: its `src` serves Exomem, its `plugins/claude-code` supplies the skill
and hooks, and its own docs and code supply the guidance text. Each session gets a fresh vault under
a neutral unit id, and the arms run interleaved. A unit passes, fails or is unknown: a harness fault,
a seeding error or a judge error is unknown, never a failure.

1. Add a case file when an agent misbehaves with Exomem in real use. A case is never removed or held out.
2. Rerun the cases when guidance text, a prompt, the model or the code that reads the answer changes.
3. Compare the base checkout with the change, with three repeats. The run root must be new and outside every repository:

```
PYTHONPATH=benchmarks:src python -m membench.outcome.incidents --arm base=../exomem-base --arm changed=. --repeats 3 --run-root /var/tmp/exomem-outcome-evals/runs/incidents-<id>
```

The run prints each case's passed, failed and unknown counts per arm. The run root holds
`manifest.json` (each arm's commit and dirty state, the CLI version, the models), `results.jsonl`
(one record per unit, and the only place that maps a unit to its arm) and `units/<id>/` (the stream
and the judge's verdict). The actor model defaults to the one that the other suites use; pass
`--model` (a full model id) when the incident happened on another. A session costs about $0.2 to $1 of notional
usage.

Reading the result: three repeats cannot prove a difference. When the changed arm passes fewer runs
than the base arm, treat the case as suspect and rerun it with more repeats before you merge. Any
other difference is "no change" at this size. When a count has unknown units, run again in a new run
root before you read it.

## Where data and evidence live

Everything sits under the run root: `EXOMEM_OUTCOME_ROOT`, default `/var/tmp/exomem-outcome-evals`.

- `upstream/` holds the pinned CL-Bench and LAB checkouts. `LOCKFILE.json` in this directory names
  their repository, commit and licence.
- `cases/` holds the private LAB and planted case files. They never go in the repository.
- `runs/<run-id>/` holds one run: `outcome.json` (what the run is), one directory per arm, and the
  reports. A judged report is `report-<backend>.json`; CL-Bench writes `report.json`.
- `docs/benchmarks/outcome/<version>.json` and `.md` in the repository hold the release panels.

## The three kinds of run

- **dev** is the default. It is instrument evidence for building and tuning. A dev run never calls
  the gate. Its report says `"evidence": "instrument"`.
- **heldout** is a decision run (`--split heldout`). Before its first session it requires the
  family's amendment and amendment sequence 10, which this harness implements, to be acknowledged,
  and every held-out input file to match the digest the amendment receipt froze. CL-Bench and LAB also need an upstream checkout without local changes.
  The Exomem checkout must have no uncommitted changes either. Any failure is a one-line refusal. The run refuses any `--model` or `--effort` other than the
  frozen actor (`claude-sonnet-5-5` at `high`). The report of LAB or planted refuses any judge
  other than `claude-opus-5-5` on Claude, which is the actor's own model family, so those
  reports carry `"judge_family": "same as actor"`. Only a run that passes all of this reports
  `"evidence": "decision"`. A CL-Bench decision plays `N`, `F`, `PP` and `P`; it refuses `E`
  and `EH`, which are ablations. It runs upstream's `default` schedule at seed 42 and plays
  `run_index` 1 by default. When a decision interval contains its bound (0 for "helps" and
  "beats", -0.05 for "not worse"), the decision is incomplete and the report says that the protocol
  calls for the next ordering; the figures stay in the report. It is complete once the three
  orderings have run, or no interval holds its bound. Add an ordering with the same run id: `--seeds 1,2`, then `--seeds 1,2,3`. The orderings come in that
  order only, so `--seeds 2` alone is refused, and a finished cell is not replayed. Run index 4 is
  allowed once, as the replacement for one voided run. The report states the run indexes played.
  The decision reads `PP - N` and `PP - F`. The pairs `P - N` and `P - F` are the product gap and
  only describe.
- **panel** is the release panel. It runs from a clean checkout of the release tag `v<version>` and
  refuses any other checkout. It runs the Exomem arms on a fixed slice of development cases:
  CL-Bench `PP` and `P` on the `multi_group` variant (never the `default` schedule), the LAB dev
  matters with `WP`, and the planted dev cases with `E`. It is evidence and never a gate. If the
  actor's CLI version or reported model differs from the previous panel, the control arms (`N`,
  `F`, `S`, `P`) run too, and the `.md` names the change. The stored files hold numbers, ids and
  arm codes only.

Every report carries `"label": "internal diagnostic"`.

## Faults

A product failure is scored: a wrong answer, a refusal, a timeout, an error result that the CLI
returns (a spent turn or context budget included) or a missing deliverable. An unanswered planted
case scores as `stale_trusted` and not `correct`. A LAB matter with no deliverable scores 0. A
harness fault is the harness failing to run the product. The evidence is a CLI crash or a CLI that
sends no `init` event before its timeout, an Exomem MCP server that is not `connected`, a lost
transcript, a reported model other than the requested one, a credential source other than
the one the run declared (see Billing), or a subscription limit that the CLI reports as rejected. Every session's `init` event is
checked.

1. The run retries a session with a harness fault, with the same inputs, at most twice. In
   CL-Bench only a question's first turn is retried, because upstream cannot rewind a question.
2. In CL-Bench, a fault that survives voids the whole run index for every arm. The run skips the
   voided index's remaining cells.
3. In LAB and planted, a fault that survives strikes the matter or case from every arm.
4. A rejected subscription limit belongs to the account, so it stops the whole run at once, with
   no retry and no further cell, matter or session. The command prints one line that says the run
   waits for room, and scores no loss. With `--jobs` above 1, a session already running ends when
   it meets the same rejection. The unit that hit the limit has no result, so the run is
   incomplete (rule 6).
5. A held-out report says the decision is void after a second voided CL-Bench run, or after more
   than 2 struck units.
6. A held-out report says the decision is incomplete, and decides nothing, while a judgement has
   failed, a unit is missing or a scheduled CL-Bench cell has not finished. It names each one.
   A CL-Bench decision is also incomplete while a decision interval holds its bound and an ordering
   is left to play. It names the next ordering; add it with `--seeds`.
   For the other causes, run the same command again, with the same run id. The judge retries failed items. A resume
   must match the run it continues: `outcome.json` records the product revision, whether the
   checkout had changes, and the CLI version (`claude --version`). A call that differs in any of
   them refuses before it starts a session. Use a new run id, or restore the checkout and CLI.
   - CL-Bench: a finished cell is not replayed. A cell that never reached its evaluation replays
     from its first question, and the run keeps the old one under `unfinished/`. The report's
     `runs.replayed` lists each replayed cell, how many times it replayed and its parked folders.
   - Planted: the run answers only the cases that have no answer file. Each arm's
     `manifest.json` is written once, and a resume must match it.
   - LAB: the run plays only the matters that lack a `result.json` for a requested arm. A matter
     that stopped half way replays every arm, because a copying arm starts from its source's
     snapshot, and the run keeps the old matter under `unfinished/`. The same run id must repeat
     the tasks, arms, model, effort, product revision, checkout state and CLI of the first call.
     The report's `runs.replayed` lists each replayed matter (its folder name), the run it belongs
     to, how many times it replayed and its parked folders.

Every report lists each arm's scheduled, valid, invalid and missing units.

## Commands

Run them from the repository root with `PYTHONPATH=benchmarks:src`, as `python -m membench.cli`.

1. Fetch the upstreams once: `membench outcome fetch`. It clones each pinned commit, syncs its
   environment, downloads the CL-Bench databases and prints their digests. A second run changes
   nothing. A checkout at another commit is refused, and the message gives the fix.
2. Run a suite: `membench outcome run clbench --run-id r1`. Add `--arms E,F`, `--seeds 1,2`,
   `--jobs 2`, `--model`, `--effort` or `--limit 3` as needed. `--limit` plays the first N
   questions, matters or cases and is for dev runs only.
3. Report it: `membench outcome report clbench --run-id r1`. LAB and planted also judge, with
   `--judge claude` (default) or `--judge codex`. The judge model id is recorded in the report.
   Add `--rejudge-sample codex` to LAB or planted to re-judge a seeded random 20% of the items
   with `gpt-6.1-sol`. It writes its own files (`judge-codex-sample/`) and adds a `rejudge_sample`
   block with the agreement to the report. The block is description only and never changes a
   decision. The command writes the report first and the sample after it, and refuses the report's
   own judge backend. It spends Codex quota, so run it only when Codex has room.
4. The product under test is this checkout. Each arm manifest records `exomem --version`, the
   checkout's commit, the CLI version, the requested and reported model, the tools served and the
   MCP config digest.
5. Check out the release tag `vX.Y.Z` in a clean worktree. Then run the release panel there:
   `membench outcome panel --run-id p1 --version X.Y.Z`. Add `--controls` to run the control arms
   without an actor change.
6. Run a decision: `membench outcome run lab --split heldout --run-id h1`.

A run spends subscription quota or API credits. State the cost before starting one.

## Billing

A session is billed to a subscription login or to API credits. It is the same model either way,
so the source is a run condition to record, not a defect. A run declares its source with
`EXOMEM_OUTCOME_BILLING` (`subscription`, the default, or `credits`). The declaration decides the
environment of every actor and judge session: under `subscription` the sessions get no
`ANTHROPIC_API_KEY`, even from a shell that holds one; under `credits` they get it, and the run
refuses before its first session when the shell holds none. A credit run starts from a shell whose
`ANTHROPIC_API_KEY` is a Console org's key, for example the one that `harness claude-credits run` uses.

- Faults. A session whose `init` event reports another `apiKeySource` than the declared one (`none` for
  `subscription`, `ANTHROPIC_API_KEY` for `credits`) is a harness fault. A source outside that
  closed set is a fault too. One run never mixes sources by accident.
- Evidence. Credit runs are instrument evidence. Decision runs stay on the subscription as the
  preregistration registers: a held-out run declared on credits, or judged on credits, never reads as
  decision evidence.
- Records. `outcome.json`, the incidents manifest and the f33 manifest store the declared source. Each
  arm manifest stores the reported `api_key_source` and its `billing` name beside the model. A session
  with no recorded source reads `unknown`.
- Reports. Every report opens with one `billing:` line that sets the declared source against the
  sources the run's sessions reported. It says plainly when they differ or one is unknown. The
  panel also says when it ran on other sources than the previous panel.
- The claude judge's billing is declared, not observed: its result names no credential source.
  `judge.json` records the declaration, and a judging directory keeps one source. A `judge.json`
  from before the field resumes and re-reports unchanged: its billing reads `subscription`, marked
  `billing_inferred`, because the run followed the protocol that put decision judges on the
  subscription although the judge's own environment was never checked. The report says so in a
  `judge billing:` line. A Codex judge gets no `ANTHROPIC_API_KEY`.
