# Design

## Context

See `proposal.md` for the candidate and its delivery boundary.

The existing compilation review accepts a list of Sources. The note writer already supports per-note validation and exact draft tokens. Source closure and source back-references share each note's atomic publication.

A local offline case on 2026-10-26 grouped five invented Sources into two notes. It measured eight application calls: two queue reads, two compilation contexts, two previews and two commits. Outstanding Sources fell from five to zero.

The calls used 2,996 argument JSON bytes and 25,028 decoded-response JSON bytes. Summed call wall time was 1,885.2 ms. Elapsed time was 6.893 seconds with startup, or 8.518 seconds with shutdown. These are structured payload bytes and local timings, not wire bytes, tokens or production latency.

The case used no model. It proves the workflow, not semantic quality or comparative gain. An earlier 41-call adoption pilot measured another workflow and supplies no matched baseline.

## Goals / Non-Goals

**Goals:** reduce repeated transport and duplicated context while preserving each note's existing authority, semantic decisions and replay behavior.

**Non-Goals:** server-authored grouping, weaker relation review, combined authority across notes, cross-note atomicity, a new worker or a durable batch journal.

## Decisions

### D1. Try the existing shared context first

One compilation review can carry the Sources for several related output notes. Each note keeps only its own citations and review decision.

For the measured case, this removes one of eight calls by construction. Its latency, payload and quality effects remain unmeasured. Prefer this existing route if it provides the useful saving.

### D2. Candidate batching preserves the existing leaves

If shared context is insufficient, add typed item input to the existing note creation family. Reuse the note leaf and draft lifecycle for each item. Keep one implementation behind MCP, REST and CLI adapters.

Preview returns one independent draft token and relation-review result per item. Commit carries each item's exact token and disposition. A common envelope cannot satisfy an item's missing decision.

Execute items in declared order, with separate governed publication and receipts. A later failure cannot erase an earlier committed note. Return the distinction instead of claiming batch atomicity.

Reuse existing receipt lookup and draft replay. Do not create a batch ledger merely to combine transport. If existing identities cannot reconcile an interrupted item, stop the prototype and revise this design before adding persistence.

### D3. Measure the candidate against the cheapest existing route

Use the same invented Sources, authored notes, model, prompts and exact product tree for both arms. Prove stored bytes, citations and outstanding counts before measuring cost.

First run an offline integration comparison. Report calls, structured bytes, wall time, retries and per-item outcomes. Preserve startup and shutdown as separate windows.

Only after a candidate exists, evaluate the agent's grouping and note content on realistic and hostile inputs with real models. Obtain any required spend agreement first. Keep held-out material separate from iteration fixtures.

Do not select an API from a one-case result. A recommendation must show a matched operational benefit and no unacceptable quality loss. Report uncertainty instead of choosing a loss tolerance on the user's behalf.

## Risks / Trade-offs

- **Partial completion can be mistaken for failure of every item.** Return item receipts and reconcile unknown outcomes before any retry.
- **A larger response can erase the round-trip saving.** Measure structured bytes as well as call count.
- **Shared context can mix unrelated provenance.** Retain per-note citation checks and test source isolation.
- **A new envelope adds schema and recovery maintenance.** Prefer the existing shared-context workflow when its measured benefit is sufficient.

## Migration Plan

This proposal enables no runtime behavior. If selected, the candidate ships additively through the ordinary reviewed release after integration and real-model proof.

Existing single-note calls remain the fallback. Stop using batch input to undo client adoption; retain all committed notes and receipts. No data rewrite or maintenance operation forms part of rollback.
