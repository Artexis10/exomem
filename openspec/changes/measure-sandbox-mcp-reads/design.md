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
3. Resolve all selected bars before connection. A bar must cover the MCP surface, the tool and the case's `deep` flag; an unknown or incompatible selection cannot fall back.
4. Keep `programme-public-warm` as the default. Availability defaults to one sample; acceptance defaults to the largest selected minimum. Explicit sample counts retain their meaning.
5. Stop timing at the parsed terminal or transport failure. Source and schema assertions still determine availability after timing ends.
6. Report descriptive nearest-rank p50 and p95 over all measured attempts. Failed attempts remain visible; absent timing distributions remain null.
7. Keep each case independent for its selected bar, sample minimum, outcomes, and coverage. Sparse descriptive percentiles do not establish acceptance.
8. Give `LatencyBar` one `covers(tool, deep)` predicate. The observer's pre-connection check, the summarizer's match and observation-bar selection all call it. Before this, the observer checked the tool and surface only, so it accepted cases that the summarizer then reported as `unavailable`: a typed tool outside the registered command names, or a case on the other `deep` population. The canary keeps its own tool tokens as trusted through a `make_sample` flag, so the registered-name filter for live traffic stays unchanged.
9. Extract the timed call as `measure_call(client, case, timeout)` and expose `load_cases`. A paired runner owns its arms and alternation and times nothing itself; it measures through the observer. A case may carry an optional `group` label for the runner. The observer ignores it, so the report shape is unchanged.
10. Bump the contract to `request-latency-v5` without touching earlier rows. It adds the `mcp-read-paired` reference and one pending bar on the `mcp-worker-socket-v1` boundary, which excludes the manager hop. The bar does not observe live traffic, so live samples keep their dispatch-boundary bars.
11. Run the paired gate on the real managed worker, not on an in-process server. The service's own startup, lease coordinator, loopback guard and socket are the boundary that clients cross, and the contract names that boundary. One runner serves the generated public fixture in CI and a private prepared vault on the operator's host. Both modes share the arms, the timer and the statistics. The private mode adds only the target report and the host CPU pressure.
12. Judge each gating group by its interval against its allowance, with Bonferroni over the contract's gating groups. An interval that straddles the allowance warns and passes, because a shared runner cannot resolve a small regression in one run, and a gate that fails on noise costs every release. A broken instrument fails, because a missing pair or failed case makes every interval wrong. The allowance stays null until paired runs of identical code set it.
13. Refuse, rather than correct, the two states that invalidated earlier private runs. State is keyed by the vault path, so a restored copy starts cold and rebuilds inside the timed window. A derived collection in `rebuilding` makes the store retry under the writer lease and adds a tail to unrelated reads. A refusal costs one rerun; a timed run in either state costs a wrong verdict.
14. Note and search cases assert a ranked non-empty answer through a response schema, and only the note read case pins one page. The reference prose repeats across pages, so no passage identifies one page; the generator cannot author a unique key without changing the reference corpus.

The validation prevents a supposedly read-only measurement from dispatching unsupported collection operations. A mistaken refusal costs one measurement run. Generated schemas minimize that cost.

### Wider instrument programme

The same instrument will cover records list, query and item read; planning list, query and item read; note read; `search_memory`; and `recall_context`.

Collection inventory uses `inspect`; collection queries and selected item reads use `query`. Case arguments define those workloads without new product operations.

Private measurements use one frozen sandbox corpus and independently authored source truth. Provider inputs, copied files, stored rows, and expected identities need counts and spot checks before timing.

Matched arms must use the same cases, corpus, runtime conditions, and boundary. Reports retain per-case differences and confidence intervals. An interval containing zero means no change.

Layer diagnostics distinguish transport, authentication, governance, egress passes, rendering, store work, and serialization. Missing layers remain unknown; unattributed elapsed time is not assigned to a layer.

A later CI gate will use neutral fixtures and configured allowances through the same observer, comparing the head checkout with the pinned release. Private corpus data and source keys stay outside the repository.

The six collection-store groups carry a configured p95 target of 100 ms. It is the private-sandbox acceptance bar and never a shared-runner gate. The paired allowances are provisional until paired runs of identical code establish them, and list operations are reported without gating. Every optimization must preserve privacy, governance, source custody, and seal guarantees.

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
