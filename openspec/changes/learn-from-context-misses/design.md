# Design

## Context

See `proposal.md` for the outcome. Existing components already provide most of the workflow:

| Existing component | Reuse | Remaining gap |
| --- | --- | --- |
| `working_set_learning.py` | Admitted anchor picks and naming advisories | Does not establish source truth or verify a later answer |
| Incident Records and observation routing | Durable occurrences, provenance, guarded updates | Do not connect each repair to a verification result |
| `context_catalogue.py` | Summary, registry, and lede description origins | Coverage alone cannot show that a description helps |
| Governed content writers and `curation.py` | Guarded edits and multi-step receipts | The agent must choose a justified repair |
| `switch_eval` and ordinary-use traces | Case-level evidence and answer checks | Evaluation material needs an explicit learning boundary |

`ranking-autotune` already owns retrieval-weight learning. This workflow does not add another tuner.
Existing attention, delegation, and collection contracts continue to own their behavior.

## Goals / Non-Goals

**Goals:** make one correction traceable from a supported miss through governed data edits to a later useful answer.

**Non-Goals:** automatic diagnosis, a second incident store, code-defined domain categories, background generation, or a packet-mode default change.

## Decisions

### Reuse an existing collection and portable agent guidance

The active agent handles semantic diagnosis through a reference linked from the generic skill scaffold.
An optional generic collection example demonstrates evidence, correction, receipt, and check links.
The manifest defines its fields and lifecycle values; the workflow maps their meaning instead of requiring fixed keys.
Existing incident references identify repeated occurrences. No extra review family, feedback endpoint, or runtime classifier is required.

This adds documentation and a small example to maintain, rather than a new persistence or execution service.
Collection creation and guarded edits retain their existing authority rules.
`add-governed-curation-lane` supplies multi-step execution when a repair needs it; a single edit uses its existing writer directly.

### Engagement owns incident filing

The source-backed correction is always the agent's behaviour. Filing the miss in an incident collection follows the active engagement: a user request, a workflow skill, or a policy that delegates filing observed outcomes.
An unrequested incident write would exceed the engagement contract, and at a balanced policy the product already asks before filing a strong incident route.
The correction's reason, history record and receipt stay available, so an engaged session can link them later.
A write-time carrier would need the server to decide that an edit is a miss correction, which this change forbids, and the tool and instruction budgets have no room for passive prose that agents ignored in practice.

### Diagnose before selecting the correction

The agent records the observed request, actual available context, source-backed expectation, and missing evidence at the permitted disclosure level.
It separates missing source data, catalogue description, discovery, selection, delivered context, and answer use as reasoning questions, not stored product enums.
No counter, lexical heuristic, or model answer alone establishes a cause.

Supported data repairs include authored summaries, topics, aliases, typed relations, and corrections to compiled knowledge.
The agent cites source evidence and checks existing ambiguity before editing an alias or membership.
An engine defect follows the normal software repair process. An unavailable source stays an evidence gap.
The workflow does not manufacture source facts to make a failed answer pass.

### Bind writes and checks to the evidence

The incident links current target versions, the proposed edit, terminal write receipts, and the later verification artifact.
Existing guards reject stale targets. The agent rereads and reconsiders them rather than replaying an obsolete patch.
Original Sources and Evidence stay immutable. Corrections retain the existing history and supersession semantics.

The exposed miss diagnoses and checks the repair. A fresh source-keyed example tests whether the repair transfers.
An independent key writer establishes expected facts before answer generation.
The answer actor never writes the key; held-out packet-mode inputs, keys, and arm maps never enter this workflow.
Checks retain failures and compare the same cases when reporting a before/after difference.

### Keep closure proportional and bounded

A record distinguishes committed data, successful retrieval, and successful downstream use through its declared fields and linked evidence.
Missing or failed stages remain visible. Only the behavior actually checked can be reported as repaired.
The next round changes one hypothesis and uses the smallest check that answers it.
Automatic bulk repair and model backfill stay default-off; missing optional work leaves the incident open while ordinary reads continue.
The server performs no generative model call. The active agent remains the semantic reasoner under existing authority and spend limits.

## Risks / Trade-offs

- Incorrect source interpretation can teach a false alias or summary. Cite the original source and retain ambiguity and unrelated-topic checks.
- A stored private trace can disclose hidden material. Apply normal audience and provenance rules to every linked artifact and served view.
- A repaired exposed example can overstate generalization. Keep it diagnostic and report the fresh check separately.
- Optional collection fields can vary. Map the declared schema; report missing fields rather than silently inventing a lifecycle.

## Migration Plan

1. Ship the generic workflow reference and optional collection example through the existing scaffold deployment, with no live vault change.
2. Verify an isolated example through existing tools, with source-derived expectations and ordinary answer evidence retained.
3. Restore prior guidance to undo the workflow; retain committed incident history and use governed corrections for accepted data edits.

## Verified Outcome

The workflow shipped in 0.142.0 as the scaffold reference `references/context-learning.md`, its skill route, and an optional example collection.

- Correction: blind-judged real sessions on an invented vault answered the diagnosed miss wrongly before a source-backed correction and correctly after it. The unrelated-topic control stayed correct. One session per cell, so these are per-case observations, not an effect size.
- Fresh transfer: inconclusive. The baseline already answered the fresh case.
- Incident filing: an engaged session filed an incident mapped onto a differently named collection schema. Filing stays with the engagement policy, as decided above.
- Stale target: `tests/test_drift_guard.py::test_edit_with_stale_hash_refuses_and_leaves_file_untouched` and `tests/test_records_bulk_upsert.py::test_a_stale_guard_refuses_the_whole_batch`.
- Restricted reader: `tests/test_origin_disclosure_sweep.py`; incident records use the same disclosure doors as their sources.
- Unavailable check and held-out exclusion: the shipped reference requires both, and no server-side component exists to test. The proof runs froze each key before any answer and used no held-out material.

Remaining limits:

- The evidence shows that the guidance works on single cases. It does not measure how often agents start the workflow unprompted.
- Back-reference and supersession repair still has regex paths outside the shared list helper, and pages that the old code corrupted need a one-off repair.
