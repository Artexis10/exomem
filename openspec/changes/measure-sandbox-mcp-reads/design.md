# Design

## Context

See proposal.md for the contract decision. The installed observer already measures parsed FastMCP calls and checks positive material through indexed paths or JSON Schema.

The observer currently fixes one bar and includes assertion time. `latency_watch` already owns nearest-rank percentiles and configured acceptance summaries. The command registry generates collection schemas.

## Goals / Non-Goals

**Goals:** Extend one observer, preserve its availability semantics, and make each case's timing and budget explicit.

**Non-Goals:** This foundation does not establish faster service behavior, a final budget, matched statistics, layer attribution, or completed programme acceptance.

## Decisions

1. Reuse the actual FastMCP observer. A second runner would duplicate transport, reporting, privacy, and failure handling.
2. Validate nested collection cases against generated operation schemas. The observer permits only `inspect` and `query`, even when another read operation exists.
3. Resolve all selected bars before connection. A bar must cover the MCP surface and tool; an unknown or incompatible selection cannot fall back.
4. Keep `programme-public-warm` as the default. Availability defaults to one sample; acceptance defaults to the largest selected minimum. Explicit sample counts retain their meaning.
5. Stop timing at the parsed terminal or transport failure. Source and schema assertions still determine availability after timing ends.
6. Report descriptive nearest-rank p50 and p95 over all measured attempts. Failed attempts remain visible; absent timing distributions remain null.
7. Keep each case independent for its selected bar, sample minimum, outcomes, and coverage. Sparse descriptive percentiles do not establish acceptance.

The validation prevents a supposedly read-only measurement from dispatching unsupported collection operations. A mistaken refusal costs one measurement run. Generated schemas minimize that cost.

### Wider instrument programme

The same instrument will cover records list, query and item read; planning list, query and item read; note read; `search_memory`; and `recall_context`.

Collection inventory uses `inspect`; collection queries and selected item reads use `query`. Case arguments define those workloads without new product operations.

Private measurements use one frozen sandbox corpus and independently authored source truth. Provider inputs, copied files, stored rows, and expected identities need counts and spot checks before timing.

Matched arms must use the same cases, corpus, runtime conditions, and boundary. Reports retain per-case differences and confidence intervals. An interval containing zero means no change.

Layer diagnostics distinguish transport, authentication, governance, egress passes, rendering, store work, and serialization. Missing layers remain unknown; unattributed elapsed time is not assigned to a layer.

A later CI gate will use neutral fixtures and configured budgets through the same observer. Private corpus data and source keys stay outside the repository.

The 150 ms p95 target remains provisional until matched evidence supports the applicable budget. Every optimization must preserve privacy, governance, source custody, and seal guarantees.

## Risks / Trade-offs

- Sparse cases produce unstable percentiles → Label them descriptive and preserve configured acceptance minimums.
- Different bars describe different populations → Resolve and retain each selected bar independently.
- Observer assertions distort caller measurements → Run assertions after the parsed terminal timer ends.
- Private source truth could escape through evidence → Keep raw cases private and reports content-free.
- The existing capability is not canonical yet → Preserve its full requirement and defer archive until its owning change synchronizes.

## Migration Plan

1. Extend the observer and verify its actual FastMCP calls with the scoped suite.
2. Freeze sandbox inputs and independent source truth before measured comparisons.
3. Establish matched evidence and layer attribution before accepting a latency fix.
4. Deliver verified changes through the ordinary independent review and release process.

Rollback restores the previous observer package and case configuration. This foundation changes no stored data or service configuration.
