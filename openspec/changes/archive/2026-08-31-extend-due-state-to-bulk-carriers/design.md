# Design — due-state carriage on the operation leaves

## D1. Carriage rides the committed terminal, so the no-write case is out

The block is admitted inside the terminal projection, which exists only when
the invocation committed a governed write (`mark_active_mutation_committed`
fires on a real replacement; the projection short-circuits otherwise). All
five leaves reach that path when they write. When they do not write — a
clean-vault `fix` pass, already-valid media, `process_media` `retry` (which
re-enqueues in the machine-local job store and commits nothing) — there is no
committed terminal to carry a block, and the leaf's raw payload passes
through untouched. The requirement therefore scopes carriage to invocations
that commit at least one governed write, honestly: a first-qualifying
no-write maintenance pass stays silent even when the projection has open
items. Closing that would be a response-contract change to non-committing
responses — named future work, not implied.

## D2. The invocation list is enumerated, not gestured at

- `adoption_studio`: `apply` and `apply-proposal` only. The other six
  registry mutations (`start`, `select`, `plan`, `cancel`, `finish`,
  `propose`) are run bookkeeping or previews; letting a `plan` preview carry
  would burn the session's single change-only emission before the real apply
  — the exact failure the terminal's admission logic records having fixed
  once.
- `maintain_memory`: `fix` with `dry_run=false` (the mode DEFAULTS to a
  dry-run preview, which is read-only), `reconcile` (mutating by default),
  `backfill-ids` with `dry_run=false` (already batch-scoped), and
  `structured-files` with `apply=true` — the one maintenance mode that
  already produces a canonical terminal and the only one exempt from the
  remote-surface refusal.
- `adopt_vault`: mutating modes (scan-only stays clean).
- `preserve_artifacts` and `process_media` mutating operations, subject to
  D1's committed-write scoping.

## D3. Per-leaf projection reality

Two different value propositions, stated so nobody expects the wrong one:

| Leaf | Its writes move projected categories? | Delta path |
|---|---|---|
| `adopt_vault`, `adoption_studio` applies | Yes — compiled pages | already wired (`_apply_batch_deltas`) |
| `maintain_memory` `fix`, `reconcile`, `backfill-ids` | Sometimes — repairs touch compiled pages | settle in tasks 1.5: wire the batch-delta path or prove the reconcile rebuild covers it before carriage lands |
| `maintain_memory(mode="structured-files", apply=true)`, `preserve_artifacts`, `process_media` | No — representation renames, binary Evidence blobs and transcript sidecars author no predictions, questions, experiments, or supersession pointers | none needed; carriage value is the session channel (first qualifying response and change-only deltas from elsewhere) |

Either way the served block SHALL reflect the post-batch projection — a block
computed from a stale projection is the bug tasks 1.5 exists to prevent.

The emission ledger's `writes` denominator counts governed page or structured
projection deltas applied to that projection, not every filesystem mutation or
terminal delivery. These three no-delta carrier families can therefore produce
`emissions +1` and `writes +0` on a first qualifying delivery; terminal delivery
MUST NOT synthesize a write tick.

## D4. Surface reality

Mutating `maintain_memory` (except `structured-files`) is refused on every
request-bound remote surface, so its carriage serves the local operator.
Hosted and MCP invocations of the other leaves flow through the same terminal
projection — no hosted-specific machinery, no per-surface emission rules.

## D5. Bench adjudication (the D10 precedent)

f23 was built to measure the missing carrier: its journey docstring records
zero carrier reach for four of these five leaves, the canonical f23 scenario
pins "on this runtime it reports `unsupported`", and two tests in
`tests/test_due_state_emission_capture.py` are documented tripwires ("the day
this leaf commits through `semantic_writes` … this test says so"). This
change flips them on purpose:

- The MODIFIED requirement rewrites the f23 scenario's runtime-outcome lines:
  the emission assertion is now decided (one block, twelve writes) and the
  measured-zero clause is retired. The evaluation rule itself — decided only
  for a batch that delivered a block, `unsupported` otherwise — is unchanged.
- The tripwire tests are **inverted, not deleted**: their red run against the
  old expectation is the red-first evidence for tasks 2.1.
- No §7 amendment is needed: a preregistered assertion moving from
  `unsupported` to evaluated is the anticipated consequence the family was
  built to detect — the runtime gained the capability; no family, assertion,
  predicate, gate, or OpKind changes, and the deterministic evaluation rule
  stands. This is the same adjudication the structured-collection carrier
  change recorded in its D10.
- f26 (`hookless_episode_carrier`) is withheld with amendment sequence 2; its
  measured world changes too, and its before/after is recorded when sequence
  2 activates — nothing publishes meanwhile.

## D6. Rollout, partial failure, and the payload check

Per-leaf wiring is independently shippable behind its own red-first test —
no all-five-or-none coupling; a leaf that stalls ships later without
un-shipping the others. A partially failed invocation that committed at
least one governed write still carries under the change-only rule; a wholly
failed or wholly-refused one has no committed terminal and carries nothing.
Before wiring each leaf, tasks 1.6 verifies the terminal's compact rebuild
preserves that leaf's response payload (the adoption-run document, maintain
summaries, the media job payload — including the media `state` key that
would collide with the envelope's) — a payload the terminal would gut is a
pre-existing defect escalated as its own change, never silently worked
around.
