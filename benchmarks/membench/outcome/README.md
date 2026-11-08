# Outcome evaluations

These three suites ask one question: does Exomem change what an agent gets done? Each plays a
Claude Code actor on the operator's subscription against a task whose score does not depend on
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

The plugin arms (`PP`, `P`, `EH`, `WP`, `WG`) run the plugin's hooks with no service credential. The
hooks keep their state inside the arm directory. The report records the characters the hooks
injected, and any tool the CLI refused for want of permission. Each CL-Bench question and each
LAB session starts with a fresh hook home, so a hook's cooldown from an earlier question cannot
silence its nudge. A plugin arm may read the plugin directory, so a skill can read its own
reference files, and nothing else outside its workspace.

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
`none`, or a subscription limit that the CLI reports as rejected. Every session's `init` event is
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

A run spends subscription quota. State the cost before starting one.
