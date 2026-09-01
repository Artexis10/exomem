## Why

After a restart, a large Windows vault can report runtime readiness while ordinary keyword and hybrid recall still synchronously reconcile or scan the full corpus. Held-file custody makes that work safe but expensive enough to exceed the public ingress deadline, producing disruptive HTTP 504s from otherwise healthy ChatGPT and POLLY connections.

## What Changes

- Make the maintained lexical catalog the first retrieval asset warmed at startup, ahead of parsed-page, resolver, semantic, and model caches.
- Refuse incomplete large-corpus lexical plans with the existing bounded `RETRIEVAL_INDEX_WARMING` outcome while single-flight background repair proceeds; never fall through to a request-time full-corpus scan.
- Publish retrieval admission in runtime readiness and withhold overall readiness while the maintained lexical catalog cannot safely serve ordinary recall.
- Add production-sized cold-start regression coverage proving that request work is independent of corpus size and that readiness transitions only after lexical admission is available.

## Capabilities

### New Capabilities

- `runtime-retrieval-readiness`: Defines content-free runtime reporting and admission semantics for the maintained lexical recall catalog.

### Modified Capabilities

- `instant-start`: Preserve immediate transport startup while making pre-lexical requests fail fast with a typed warming outcome instead of performing unbounded fallback work.
- `live-index-freshness`: Move unknown or large lexical repair entirely off the request path and keep exact bounded deltas as the only foreground repair.
- `find-recall-efficiency`: Warm the maintained lexical catalog before optional full page, resolver, semantic, and model caches.

## Impact

- Retrieval and warm-up internals in `src/exomem/find.py`, `src/exomem/lexstore.py`, `src/exomem/warmup.py`, and readiness projection code.
- The `/health/ready` payload and status code gain a content-free retrieval-admission dependency.
- Existing exact catalog warming error semantics are extended to ordinary keyword and hybrid retrieval when a safe maintained catalog is incomplete.
- No canonical Markdown, ranking formula, model, or tool schema changes.
