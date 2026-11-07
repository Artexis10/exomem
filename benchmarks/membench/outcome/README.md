# Outcome evaluations

These three suites ask one question: does Exomem change what an agent gets done? Each plays a
Claude Code actor on the operator's subscription against a task whose score does not depend on
Exomem. Every figure they produce is an **internal diagnostic**. The actor is not the suites' own
protocol, so no number here may be quoted as a CL-Bench or LAB result.

## The suites

- **clbench** is CL-Bench `database_exploration` (upstream scoring, family f35). The actor answers
  a sequence of questions about an unknown database. Arm `N` keeps nothing between questions. Arm
  `F` keeps a plain memory folder. Arm `E` keeps an Exomem vault. Arm `EH` is `E` plus the shipped
  Exomem Claude plugin directory, with its hooks at their default activation. The suite answers whether
  memory lowers the queries and raises the score across a sequence, and whether Exomem beats a folder.
- **lab** is Harvey LAB multi-document synthesis (family f36). The actor writes one deliverable per
  matter. Arm `S` reads the documents. Arm `W` gets an Exomem vault compiled from those documents
  beforehand. Arm `WG` is `W` with the graph index off, an ablation. A blind judge scores each
  deliverable against the task's own criteria. The suite answers whether a compiled vault helps on
  long, multi-document work.
- **planted** is the planted wrong-note suite (family f37). A vault holds a stale or wrong note and
  its correction. Arm `E` records the correction as Exomem governance. Arm `P` holds the same pages
  without it. A blind judge reads each answer against the case key. The suite answers whether
  governance keeps the agent from trusting the stale value.

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
  family's amendment to be acknowledged, and every held-out input file to match the digest the
  amendment receipt froze. Either failure is a one-line refusal. Its report says
  `"evidence": "decision"`. CL-Bench runs upstream's `default` schedule at seed 42, run indexes
  1, 2 and 3.
- **panel** is the release panel. It runs the Exomem arms on a fixed slice: CL-Bench `E` and `EH` at
  run index 0, the LAB dev matters with `W`, and the planted dev cases with `E`. It is evidence and
  never a gate. If the actor's CLI version or reported model differs from the previous panel, the
  control arms (`N`, `F`, `S`, `P`) run too, and the `.md` names the change.

Every report carries `"label": "internal diagnostic"`.

## Commands

Run them from the repository root with `PYTHONPATH=benchmarks:src`, as `python -m membench.cli`.

1. Fetch the upstreams once: `membench outcome fetch`. It clones each pinned commit, syncs its
   environment and downloads the CL-Bench databases. A second run changes nothing. A checkout at
   another commit is refused, and the message gives the fix.
2. Run a suite: `membench outcome run clbench --run-id r1`. Add `--arms E,F`, `--seeds 1,2`,
   `--jobs 2`, `--model`, `--effort` or `--limit 3` as needed. `--limit` plays the first N
   questions, matters or cases and is for dev runs only.
3. Report it: `membench outcome report clbench --run-id r1`. LAB and planted also judge, with
   `--judge claude` (default) or `--judge codex`. The judge model id is recorded in the report.
4. Test an installed release instead of this checkout by adding `--product-python PATH` to `run`
   or `panel`. Each arm manifest records `exomem --version`, the CLI version, the requested and
   reported model, the tools served and the MCP config digest.
5. Run the release panel: `membench outcome panel --run-id p1 --version X.Y.Z`. Add `--controls`
   to run the control arms without an actor change.
6. Run a decision: `membench outcome run lab --split heldout --run-id h1`.

A run spends subscription quota. State the cost before starting one.
