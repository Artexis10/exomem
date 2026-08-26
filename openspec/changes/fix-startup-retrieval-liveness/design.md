## Context

The HTTP transport deliberately starts before cache and model warm-up. On a large Windows vault, the maintained lexical SQLite catalog can be stale at that point. Ordinary BM25 and keyword lanes still use legacy request-time reconciliation and full-scan fallbacks. After held-file custody made every page acquisition safer, that fallback exceeded the public ingress deadline: the origin kept working for roughly two minutes while ChatGPT received HTTP 504. Runtime readiness remained 200 because it measured coordination and mutation admission, not retrieval admission.

The existing exact category/kind path already has the desired primitives: non-walking catalog readiness, bounded foreground delta replay, single-flight background repair, and the typed `RETRIEVAL_INDEX_WARMING` result. This change extends that architecture to ordinary lexical recall rather than inventing another recovery system.

## Goals / Non-Goals

**Goals:**

- Keep MCP transport startup non-blocking.
- Make ordinary keyword and hybrid requests independent of corpus size while the maintained catalog is incomplete.
- Make the maintained lexical catalog usable before optional page, resolver, semantic, and model warm-up.
- Make `/health/ready` distinguish a live process from one that can admit ordinary retrieval.
- Preserve exact recall, access policy, held-file custody, and single-flight repair guarantees.

**Non-Goals:**

- Revert held-file custody or the instant-start transport design.
- Change ranking, retrieval quality, canonical Markdown, or model selection.
- Make graph recovery a retrieval-readiness prerequisite.
- Remove the explicit Python lexical backend or small-corpus reference implementation.

## Decisions

### Gate large-corpus lexical requests through catalog readiness

When foreground lexical repair is disallowed by the existing corpus-size bound, keyword and hybrid paths will consult the non-walking catalog-readiness seam before BM25 or substring work. An incomplete result schedules the existing single-flight repair and raises the existing typed `RETRIEVAL_INDEX_WARMING` operation outcome. A transient query failure after a successful readiness proof also returns the typed temporarily-unavailable outcome rather than scanning the corpus.

Small corpora retain bounded synchronous repair and the explicit `python` backend retains its reference implementation. This preserves development and rollback behavior without allowing production-sized automatic fallback scans.

Alternative rejected: globally enable eager boot. It avoids request-time work only by moving the same unbounded cost into service downtime and discards the transport-start work already shipped.

### Split maintained-catalog warm-up from optional cache warm-up

Warm-up becomes ordered admission and optimization phases. First, KB- and vault-scope maintained lexical catalogs are reconciled. A new `retrieval_catalog` readiness component is marked only when both scopes succeed. The semantic corpus required for mutation admission lands next. Only then do parsed pages, resolver state, matrices, and models warm under their existing soft-fail and resource-mode rules. Quiet mode still performs the disk-backed maintained-catalog and semantic-corpus phases because neither requires retaining all optional search caches in RAM.

Local service composition also becomes transport-first. The HTTP server is built and allowed to answer liveness before local retrieval, watcher, graph-drain, or media startup work is activated. After activation, required catalog and semantic-corpus state gets exclusive startup priority: watcher reconciliation, graph recovery, and media discovery wait until retrieval and mutation admission are established, or warm-up reaches a terminal failed outcome. This prevents independent startup reconcilers from contending on the process-local mutation boundary while required state is being rebuilt.

Alternative rejected: only reorder the existing steps while keeping one `lexical` marker. That would either report readiness too late after the catalog is already usable or too early when fallback pages are still cold.

### Keep watcher publication and lexical consumption at the same scope

The file watcher publishes one freshness generation for changed and deleted Markdown across the entire vault. The lexical sidecar serves both KB and vault recall scopes, so its bounded mutation consumes the complete admitted/suppressed/deleted union from that generation under one publication barrier. Only after capturing that full lexical input does the fan-out narrow memory references, graph work, and embeddings back to `Knowledge Base/`. A KB-only lexical handoff cannot prove the vault checkpoint when the same batch contains a `Sources/`, `Evidence/`, or other outside-KB edit, and would strand retrieval admission behind an otherwise current catalog.

Alternative rejected: pass the complete vault batch to every index. That would make outside-KB Markdown enter KB-scoped embedding and graph lanes merely to repair a lexical checkpoint.

### Extend runtime readiness with content-free retrieval admission

Runtime readiness projects the process-local `retrieval_catalog` component as a content-free `retrieval` block. While startup warm-up is active and the catalog is not ready, or after that phase fails, overall readiness is withheld with a stable reason. The liveness endpoint remains independent. No vault paths, queries, counts, or catalog content enter the response.

`EXOMEM_DISABLE_WARMUP` remains an explicit lazy-mode escape hatch. It reports retrieval as unverified/not admitted until a successful maintained-catalog repair marks the component ready; transport liveness still starts normally.

Alternative rejected: leave `/health/ready` coordination-only and add another endpoint. Existing deployment, failover, and acceptance tooling already treats this route as admission truth, so a second endpoint would preserve the false-positive operational failure.

### Test the invariant, not the incident timing

Regression tests use a production-sized synthetic freshness tuple and instrument the reference walk/page parser. They assert that an incomplete maintained catalog produces the typed warming outcome with zero walk or page reads, that catalog warm precedes optional page warm, and that runtime readiness transitions from 503-equivalent state to ready only after catalog admission. A mixed KB/outside-KB watcher regression also proves the complete vault generation reaches lexstore while embeddings remain KB-scoped and that both persisted catalog checkpoints can promote runtime admission.

## Risks / Trade-offs

- [Clients see a retryable warming error where they previously waited for eventual results] -> The outcome already exists in the public operation contract, is bounded, and is preferable to an edge-generated 504 with no useful remediation.
- [A catalog repair failure can hold readiness at 503] -> Liveness remains 200, logs preserve per-stage failure, and the existing single-flight repair keeps retrying without touching canonical data.
- [Watcher, graph, and media reconciliation start later on a cold catalog] -> Their existing startup scans recover changes made while the service was stopped; deferring them avoids mutation contention without losing durable work.
- [Quiet or explicitly disabled warm-up changes readiness behavior] -> Quiet still warms only the disk-backed catalog; fully disabled warm-up is explicitly lazy and reports that truth instead of claiming full admission.
- [Readiness consumers may not expect the added field/reason] -> The payload extension is additive; the existing status/reasons contract already supports not-ready causes.

## Migration Plan

1. Ship the code and delta specs in a patch release.
2. Upgrade personal and POLLY environments without deleting their rebuildable sidecars.
3. Restart one cell at a time and verify liveness, retrieval-gated readiness, transition to ready, and a cold plus hot recall through the public ingress.
4. Reconcile the personal graph after lexical admission is healthy.
5. Roll back to 0.60.1 only if the new bounded admission path prevents recovery; no canonical-data migration is involved.

## Open Questions

None. The existing catalog readiness, retry outcome, and repair worker provide the required mechanisms.
