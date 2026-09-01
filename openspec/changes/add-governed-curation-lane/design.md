## Context

The no-nudge architecture now has deterministic sensors, review queues, and a
served authority envelope. What it lacks is a safe executor for an agent's
multi-step curation decision. The current `exomem-curate` workflow asks the
agent to read each page and call individual write tools. That preserves
judgment in the agent, but a sequence has no immutable identity, durable
progress, crash recovery, or single reviewed preview.

Two existing systems provide the pattern rather than a new architecture:

- Adoption Studio separates deterministic context and validation from an
  agent-authored proposal, review, and governed apply.
- The writer boundary, mutation terminal, semantic validate/commit drafts,
  governed trash, and supersession chain already provide the single-operation
  safety properties curation needs to compose.

The current Hosted v4 agent profile exposes the legacy `maintain_memory`
contract, but its historical command schema is immutable and the common
invocation boundary refuses request-bound write maintenance except
`structured-files`. Curation therefore ships remotely only through the new v5
profile: v1-v4 neither advertise nor admit the new arguments. V5 gains one
narrow remote exception for its exact reviewed-plan protocol; ordinary `fix`,
`reconcile`, and ID backfill remain operator-only.

That refusal is the subject of the still-active `bound-remote-maintenance`
OpenSpec change. S8 is a deliberate successor constraint, not a contradiction:
the older change blocks unbounded synchronous maintenance whose worker can
outlive the request, while this design admits at most one evidenced content
step per request. Implementation must preserve and extend its refusal tests,
not delete or route around the common admission boundary.

The design deliberately does not touch Planning/Records automation or the
separate Exomem/OpenSpec workflow-contract work. Curation targets governed
knowledge and entity material, not intended-future-state or observed-event
state machines.

## Goals / Non-Goals

**Goals:**

- Let the active agent author one strictly typed, immutable curation plan and
  show its exact effects before any content mutation.
- Apply a confirmed plan as bounded, individually receipted governed steps,
  with deterministic recovery at every crash cut.
- Expose honest partial, failed, interrupted, blocked, completed, and
  compensated states; never imply multi-file atomicity.
- Make exact replay and continuation safe across local CLI/MCP/REST and Hosted.
- Preserve history when reversing work through a separately reviewed
  compensation plan.
- Keep plan execution content-language-neutral and usable for multilingual
  Markdown.

**Non-Goals:**

- Server-side semantic judgment, proposal writing, same-modal text generation,
  automatic authority inference, or automatic execution of surfaced work.
- A new general workflow engine, arbitrary command runner, or arbitrary
  Markdown patch format.
- Multi-step atomicity or rollback that erases history.
- Planning/Records lifecycle changes, OpenSpec orchestration, or a replacement
  for the workflow-contract effort.
- Changes to Adoption Studio run semantics or legacy maintenance modes.

## Decisions

### 1. Extend `maintain_memory` with one closed curation action set

`maintain_memory(mode="curation")` gains the finite selector
`curation_action`:

| Action | Classification | Effect |
|---|---|---|
| `work-item` | read | Assemble bounded current pages, review refs, hashes, and allowed step schemas. |
| `propose` | mutation | Validate and create one immutable forward plan; no knowledge page changes. |
| `preview` | read | Return exact ordered actions, current binding state, blockers, and compensation classes. |
| `status` | read | Inspect evidence and report durable progress, recovery needs, and the next action without writing. |
| `apply` | mutation | Record approval for the exact forward plan and execute at most its next step. |
| `resume` | mutation | Execute at most the next step of the already approved forward or compensation plan. |
| `propose-compensation` | mutation | Derive a new immutable compensation plan from committed receipts. |
| `apply-compensation` | mutation | Record approval for the exact compensation plan and execute at most its next step. |

The signature reuses `plan_id` and `why` and adds only curation-specific
arguments (`curation_action`, `run_id`, `plan`, `refs`, `paths`, and
`expected_plan_fingerprint`). Argument validation refuses fields belonging to
another maintenance mode. Unknown actions take the conservative mutation path
and then fail with `INVALID_CURATION_ACTION` before any leaf dispatch.

This is preferable to a new product command because maintenance is already the
advanced product surface for audit and repair, and its registry entry already
generates MCP, REST, CLI, OpenAPI, Hosted, and capability documentation. It is
preferable to extending Adoption Studio because adoption's originals-never-
modified and first-run run model are different invariants.

Each apply-shaped request executes at most one content step. This bounds the
vault mutation hold and Cloudflare request duration, gives every step one
request/terminal identity, and makes partial progress visible. The shipped
skill continues an approved run by calling `resume` until a terminal state or
blocker; it does not ask the user to approve every already-reviewed step.

### 2. Canonical run layout separates immutable intent from progress

Runs live beneath:

```
Knowledge Base/_Governance/curation/runs/<run_id>/
  plan.json
  approval.json
  state.json
  receipts/<ordinal>-<step_id>/<attempt>.json
  evidence/<operation_id>.json
  compensation/<compensation_plan_id>/plan.json
  compensation/<compensation_plan_id>/approval.json
```

`run_id` is `cur-<YYYYMMDD>-<plan-id-prefix>`. `plan.json` and compensation
plans are create-only canonical JSON; `plan_id` is SHA-256 over their canonical
bytes. Reusing a plan id with different bytes is a hard collision. `state.json`
contains phase, active step, and receipt index; it never changes plan meaning.
Approval and per-attempt step receipts are create-only terminal records, and at
most one committed or recovered-committed receipt may exist for a step. State
can be rebuilt from immutable plans, approvals, witnesses, and receipts, so a
stale state projection is repairable rather than authoritative.

Plans are governed content because they may contain agent-authored note bodies
and exact pre-images needed for compensation. `_Governance` disclosure rules
therefore apply. The local idempotency database remains a retry accelerator,
not the only record of plan outcome.

### 3. Plans use a closed step vocabulary, never command-name dispatch

The v1 step kinds are:

- `create-note` → the existing `remember` validate/commit path;
- `create-entity` → `connect_memory(operation="create-entity")`;
- `accept-relation` → the existing fingerprint- and hash-bound relation accept;
- `edit` → the discriminated `edit_memory` validate/commit path;
- `supersede` → `replace_memory` validate/commit;
- `move`, `delete`, and `recover` → the matching `manage_memory_file` leaves.

Every kind has a strict per-kind argument schema with unknown fields refused.
An agent cannot submit a command name, raw filesystem operation, Python
callable, or free-form patch program. Existing leaf safety stays authoritative:
delete still requires its canonical confirmation semantics, source/evidence
and relation validation still run, semantic draft tokens still bind content,
and protected-tree/path confinement is never relaxed.

`work-item` returns exact current hashes and registry identities. `propose`
validates every step in order, runs existing validate-only preparation where
available, records all read/write targets, expected present hashes and expected
absences, relevant registry fingerprints, and the deterministic postcondition.
It refuses an input binding that does not match live state; it never silently
rebinds an agent's stale review. A plan fingerprint covers canonical plan bytes,
the ordered binding manifest, and relevant schema/relation/entity registry
identities.

The executor refuses Planning, Records, workflow-contract, `_Schema`,
`_Governance`, `_Adoption`, and other protected administrative targets except
its own internal run artifacts. Those domains retain their typed owners.

### 4. The agent decides; Exomem validates and executes

Candidate sensors and existing review queues may point the agent at work. The
`work-item` action only assembles bounded recorded context and deterministic
measurements. `propose` accepts the agent's explicit interpretation and desired
steps; Exomem performs schema, binding, registry, and policy checks without
ranking or semantic inference.

No language classifier participates in plan admission. Text fields remain
Unicode, display titles can be multilingual, and executability depends on
typed operation ids, paths, hashes, draft tokens, and registries. An agent may
therefore author a Finnish, English, or mixed-language correction under the
same contract. ASCII filename slug constraints remain the existing leaf rule,
not an English-content requirement.

### 5. Explicit plan approval is durable and bounded to exact bytes

`apply` and `apply-compensation` require `plan_id`,
`expected_plan_fingerprint`, and a bounded single-line `why`. The server
recomputes the fingerprint and every live binding under the mutation boundary
before recording a create-only approval artifact. Approval is stored once
against those exact plan bytes. `resume` may act only on that stored approved
identity and cannot switch to a newly proposed or changed plan.

The served skill maps `propose`/`preview` to the existing
`structural_suggestions` envelope class and maps `apply`, `resume`, and
compensation execution to `restructure_execution`. It requires explicit
in-conversation confirmation before the first apply call. No prominence or
family disposition can lift that ceiling. This is an agent-contract gate plus
server-side exact-plan binding; it does not pretend the server can infer human
intent from prose.

### 6. Each leaf commits a witness with its effect

For a step, the executor first persists a `prepared` state containing the
deterministic `operation_id = sha256(plan_id + ordinal + step_id)`. It then
dispatches the existing governed leaf through a narrow internal adapter. The
adapter supplies one content-free curation witness as an additional write to
the leaf's canonical atomic batch. The witness binds:

- run, plan, ordinal, step, operation, and parent-compensation identity;
- exact before/after target manifest hashes;
- the governed leaf's operation/draft/transition identity;
- committed timestamp and result digest.

The adapter may expose a private `additional_writes`/commit-witness seam in the
underlying prepare/commit implementation, but public leaf schemas and ordinary
behavior stay unchanged. It must not perform the page mutation itself or create
a second implementation of leaf validation.

After the leaf returns, the executor creates the terminal attempt receipt and
advances `state.json`. Read-only `status` may diagnose every cut but never
repairs it; the next mutation-shaped `apply` or `resume` performs any required
receipt recovery. This produces four recoverable cuts:

1. no prepared state: no step started;
2. prepared state, no witness: no canonical effect committed, so exact retry is
   allowed after live bindings still match;
3. witness present, terminal receipt absent: the effect committed; status
   reports recovery required, then exact resume verifies the witness and live
   postcondition, creates a `recovered-committed` receipt, and never invokes the
   leaf again;
4. terminal receipt present: replay returns that terminal.

An invalid witness, competing witness, or witness/postcondition disagreement is
`CURATION_OUTCOME_UNCERTAIN` and blocks execution. Recovery never guesses from
mtime, activity-log prose, or current content alone.

### 7. Partial failure is a first-class terminal, not rollback

After each step, phase derives from immutable receipts:

- `proposed` before approval;
- `approved` after approval but before the first step;
- `executing` while a prepared step exists;
- `partial` when at least one step committed and the next step failed or is
  blocked;
- `failed` when no content step committed and the plan cannot continue;
- `completed` when every step has a committed receipt;
- `compensating`, `compensated`, or `compensation-partial` for the reverse plan;
- `blocked` when exact outcome cannot be proven.

A clean leaf refusal records a failed attempt without a commit witness. A
retryable operational failure may be retried by `resume` with the same
operation id after guards are rechecked. Stale content, changed registries, or
invalidated semantics require a new forward plan; immutable plan bytes are
never edited in place.

### 8. Compensation is a new reviewed plan that preserves history

`propose-compensation` reads only committed step receipts, witnesses, and the
sealed compensation material captured during proposal validation. It reverses
committed steps in descending order:

- created notes/entities are deleted to governed trash;
- deleted items are recovered from their exact trash receipt;
- moves are moved back when the original path is still safely available;
- edits and accepted relations are corrected by superseding the current page
  with the sealed pre-step document;
- a supersession is corrected by superseding its committed successor with the
  sealed predecessor document;
- a prior recover is compensated by a new governed delete.

The derived plan includes fresh live bindings and refuses collisions or drift.
It is immutable, has its own fingerprint and approval rationale, uses the same
one-step witness/receipt protocol, and links every result to the forward plan.
The original plan, successor chain, trash entries, witnesses, and receipts are
never removed. This is compensation, not time travel.

### 9. Hosted and standalone use the identical executor

The registry-derived surfaces expose the curation schema everywhere.
`invocation_is_read_only` classifies only `work-item`, `preview`, and `status`
as reads. Every other action takes the common writer authority, process-safe
vault boundary, terminal projection, and tenant-scoped retry path.

For profiles whose pinned schema declares curation, the request-bound
maintenance refusal changes from a single `structured-files` exception to the
explicit set `{structured-files, curation}`. No other maintenance write is
admitted remotely. The v5 runtime needs no intercept or separate executor: its
profile includes the new `maintain_memory` schema, and the generic gateway
forwards the canonical arguments to the same leaf. V1-v4 keep their frozen
schema and refuse the new arguments before manager dispatch. Runtime capability
and actual-wire tests prove both boundaries rather than relying on
documentation.

Hosted v1-v4 source and generated artifacts, locks, archives, definitions,
fixtures, promotion records, resolved command schemas, and actual-wire
identities remain byte-identical. The curation implementation supplies a
generic synthetic contribution input outside the candidate tree at
`tests/fixtures/hosted_v5_contributions/governed_curation.json`. The single v5
owner canonicalises and freezes that input into the candidate-owned combined
fixture before the first render or lock. The curation lane does not edit,
render, lock, archive, promote, or roll back any v5 file.

Standalone mode stores all canonical run material inside the vault and uses
only the existing local mutation/runtime state; it requires no Hosted service,
control plane, or network coordinator.

### 10. Fault injection is part of acceptance

The curation executor exposes test-only barriers after prepared-state commit,
after leaf+witness commit, after terminal-receipt commit, and after each
equivalent compensation cut. Tests terminate/recreate the executor at every
barrier, then call `status` and exact replay. Acceptance requires one effect,
one terminal receipt, correct next-step selection, and byte-identical plan
identity at every cut. A changed plan id/fingerprint or altered target must
refuse, not recover optimistically.

## Risks / Trade-offs

- **[Risk] Plan files duplicate pre-change content for compensation.** → Keep
  them under governed `_Governance`, cap plan/step/body sizes, include them in
  normal backup/disclosure policy, and never expose them through broad review
  listings.
- **[Risk] Adding an atomic witness seam touches several mature writers.** →
  Keep the seam private and additive, test every allowed adapter against its
  ordinary leaf, and reject a step kind whose leaf cannot prove witness+effect
  in one batch.
- **[Risk] One-step requests require several tool calls for a long plan.** →
  The skill loops automatically after one approval; bounded calls are safer for
  Hosted edge timeouts and make progress observable.
- **[Risk] A content-free witness may not be enough to reconstruct a rich leaf
  result.** → The curation receipt promises the closed curation terminal shape,
  not the full legacy leaf payload; it retains a result digest and exact target
  manifest while `response_detail=full` may include the live leaf result only
  on the first delivery.
- **[Risk] Independent edits can make compensation impossible.** → Refuse on
  drift and require a freshly authored correction plan; never overwrite later
  work to satisfy a reverse operation.
- **[Risk] Remote curation weakens the broad maintenance refusal.** → Admit only
  `mode="curation"`, require an immutable reviewed fingerprint, retain the
  restructure ceiling, and test that every legacy write mode is still refused
  before manager dispatch.

## Migration Plan

1. Add the curation plan/store and read-only work-item/preview/status paths
   behind the new mode; no existing invocation changes.
2. Add strict step schemas and validate-only plan preparation.
3. Add the private leaf commit-witness seam and one adapter at a time with
   crash tests before enabling its step kind.
4. Add forward apply/resume, then compensation derivation/execution.
5. Update standalone read/write classification, schema fixtures, capability
   docs, and scaffold workflow skill; provide the generic curation contribution
   input outside the v5 tree. The v5 owner adds profile-scoped remote admission,
   composes the candidate-owned fixture, and proves the complete historical
   v1-v4 manifest unchanged.
6. Run scoped suites during implementation, then the full lean corpus,
   OpenSpec strict validation, artifact freshness, and privacy/leak gates.

Rollback is code-only: older versions ignore the new run subtree and continue
serving ordinary vault content. Already committed knowledge effects remain
governed history; operators use the generated compensation plan rather than
deleting run artifacts or attempting an out-of-band rollback.

## Open Questions

None. The v1 action set, step vocabulary, one-step execution bound, witness
protocol, compensation rules, Hosted admission, and Planning/Records exclusion
are fixed by this change.
