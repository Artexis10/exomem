<!-- authority:non-specification -->

# Context-activation benchmark runbook

Operational companion to OpenSpec change `add-context-activation-benchmark`.
Not a specification authority: the normative source is
`openspec/specs/context-activation-benchmark/spec.md` and
`openspec/specs/epistemic-utility-regression/spec.md` once the change is
archived (today, the change's own
`specs/context-activation-benchmark/spec.md` and
`specs/epistemic-utility-regression/spec.md`). This page is how to actually
run the instrument in the order the pre-registration requires.

## What this instrument is, and is not

- The **deterministic activation audit** (Layer A) is model-free, runs in
  CI over the seeded synthetic corpus, publishes no comparative claim, and
  carries no epistemic-bench registry row. It is implemented in
  `benchmarks/membench/utility/context_activation.py`, over fixtures in
  `epistemic.corpora.context_activation`.
- The **agent-in-the-loop arms** (Layer B, A1-A5) ride the released `f32
  utility_action_episode` family as new variants (a code-tuple extension,
  no new amendment). They are implemented in
  `benchmarks/membench/utility/context_activation_arms.py`, reusing
  `epistemic.journeys.f27_replay`'s isolation primitives directly. **No
  code in this repository executes a real `claude -p` session for these
  arms.** Building and printing a dry-run argv is the supported entry
  point; an actual replay is a separate, explicitly authorized, opt-in paid
  probe under the existing paid-probe rule
  (`epistemic-utility-regression`, "Paid probes are bounded and opt-in").
- The **private real-vault instrument** runs the same Layer A scorer
  locally over a digest-pinned snapshot of a real vault. Its results are
  Evidence in the owner's own knowledge base, never committed here.

## Order of measurement (design.md D8)

Run these in order. Each step either produces the input the next step needs,
or exists specifically to strike cases that would make every later step
meaningless.

1. **Naive-path latency baseline.** Measure the deterministic baseline's own
   `ask_memory` latency on a quiesced cell before pinning any latency
   threshold. This is what turns the placeholder p50 800 ms / p95 2500 ms
   in the spec into a measured constant.
2. **A5 ceilings.** Oracle-packet dry runs (or, once authorized, replays)
   per case. "A case for which A5 does not beat A1 SHALL be struck from
   the report and never scored against the compiler" — striking unwinnable
   cases here means every later step's denominator is honest.
3. **A1 floor.** The no-Exomem control, per case.
4. **A2/A4.** Raw recall, then nudged recall. If A4 already clears the
   pre-registered bar, that is the cheapest possible falsification of the
   compiler's claim (no A3 session needed to reach a verdict).
5. **Deterministic baseline.** `ask_memory` output, per fixture turn,
   labelled against gold/poison — this is the one step in this list this
   change actually runs (see below); everything upstream of it in this
   ordering that touches a real agent session is dry-run-only until
   separately authorized.

## Quiesced-cell and nonce rules

Two Exomem services may be live on this host (a personal cell and a
secondary POLLY cell). Before any measurement against either:

- **Check the cell is quiet first.** Read instantaneous CPU from
  `/proc/<pid>/stat` deltas (two samples a second or more apart), never
  `ps %CPU` (a load-average-style figure that answers a different
  question). Do not measure while the cell is busy.
- **Use the normal client path only.** MCP (`ask_memory`) or the personal
  REST facade. Never an out-of-process `exomem index` or `find` run
  against a live service's state directory -- that measures a different
  code path and can corrupt an index a live service is using.
- **Never restart the cell, and never read its state directory directly.**
- **Put a nonce in every query.** A short, unique, per-query token appended
  to the turn text, so repeated measurement cannot be served from a cache
  and so a query is identifiable in the cell's own logs without needing to
  read vault content back out.
- **Titles and paths only, never quoted body content**, in any report this
  produces, whether committed or handed to the orchestrator.

## Repeats: n = 1 baselines, n = 5 comparison

- **Pre-implementation baselines run at n = 1 per arm per case** for A1,
  A2, A4 and A5. This is what "the first paid smoke" language in
  `PREREGISTRATION.md` §4 describes for the sibling `f32` smoke, applied
  here to the four arms that do not depend on the compiler.
- **The A3 comparison runs at n = 5 per arm per case**, reporting
  individual and modal outcomes -- never a mean across cases -- once the
  compiler (`add-context-activation`) exists.
- **Arm order rotates by seed and case index**
  (`context_activation_arms.rotate_arm_order`).
- Harness faults (non-zero exit, an error-subtype or `is_error` result, a
  malformed transcript line) are reported **blocked**, never scored as a
  loss (`context_activation_arms.harness_fault_status`).

## Stopping criteria (verbatim from the spec)

From `openspec/changes/add-context-activation-benchmark/specs/
context-activation-benchmark/spec.md`, "Run protocol, stopping criteria and
manifests":

> The mechanism SHALL be reported falsified if A3 fails to beat A4 on the
> reminder test in at least five of nine cases, if any twin yields a
> `resolved` false activation, if A3 uses a poison fact where A2 and A4 used
> none, if the no-memory case injects any context, if the supersession case
> presents superseded knowledge as current, or if A3 harms more than one
> case the control arm got right; accepted for v0 only if A3 beats A4 in at
> least seven of nine cases with every deterministic threshold met, zero
> poison use and a p95 packet at most 1,500 tokens; and indeterminate
> otherwise. Every run manifest SHALL carry the fixture-set digest, the
> corpus digest and the threshold digest; a manifest missing any of them
> SHALL void the run. All eighteen fixtures SHALL run or no verdict SHALL be
> published.

And, from "Agent arms and controls":

> A case for which A5 does not beat A1 SHALL be struck from the report and
> never scored against the compiler.

## Running the deterministic audit (CI, always available)

```
uv run python -m pytest tests/test_context_activation_fixtures.py tests/test_context_activation_audit.py -q
```

The audit itself is a library, not (yet) a standalone CLI:
`membench.utility.context_activation.run_audit` takes a `case_id -> packet`
mapping (from `load_packet` on an oracle-packet file, or later from
`activate_context` output) and a `RunManifest` carrying the three required
digests, and returns a report with no aggregate field -- every metric is a
per-case or per-case-per-anchor-kind numerator/denominator pair. A `case_id`
with no supplied packet scores against the documented kill-switch shape
(`DISABLED_PACKET`): this is the mechanism-removal check --
`EXOMEM_DISABLE_WORKING_SET=1` should make every positive case fail, because
running the audit against an empty packet map is exactly what "the compiler
is disabled" looks like from this scorer's point of view.

## Producing a dry-run argv for one arm (never executes anything)

```python
from pathlib import Path
from epistemic.journeys.f27_replay import discover_agent_envelope
from membench.utility.context_activation_arms import (
    context_activation_turn_argv, dry_run_lines, generate_context_activation_episode, variant_for_case,
)

envelope = discover_agent_envelope()
episode = generate_context_activation_episode(seed=1, variant=variant_for_case("C1"))
plan = context_activation_turn_argv(episode=episode, arm_id="A4_nudged_recall", envelope=envelope, out_dir=Path("/tmp/context-activation-dry-run"))
print("\n".join(dry_run_lines(plan)))
```

This prints the exact argv and environment delta and writes nothing --
`plan.workdir` and `plan.system_prompt_file` are computed paths, not created
files. Running the real `claude -p` session this argv describes is a
separate, explicitly authorized step.

## Building the private-vault snapshot (local only, never committed)

```
uv run python scripts/private_vault_snapshot.py --source /path/to/vault --dest /path/outside/every/repository
```

Refuses outright if `--dest` resolves inside any git checkout. Excludes any
page whose body contains a fixture turn verbatim and lists the exclusion
(by path only) in `SNAPSHOT_MANIFEST.json`. The corpus's own logical
gold/poison keys (`epistemic.corpora.context_activation.KEY_KINDS`) still
need a locally-authored, never-committed `key -> real path` mapping before
`membench.utility.context_activation.score_case` can resolve a real packet
against them -- `score_case`'s `key_to_ref` parameter exists for exactly
this, and defaults to the identity mapping for a hand-written oracle packet
that already speaks in the fixture's own keys.

## Naive-path latency and deterministic baseline reports

Both are model-free measurements this change actually runs (not dry-run
placeholders): see the run reports and the measured latency constant
recorded in `epistemic.corpora.context_activation.MEASURED_LATENCY_MS`.
Evidence from a run against a real vault is preserved in the owner's
knowledge base, never in this repository.
