## Context

`runtime_readiness` builds the `/health/ready` payload from
`readiness.retrieval_admission`, whose three states (`ready`, `warming`,
`unavailable`, plus `unverified` before any warm) `find` also routes on. The
request path serves a bounded lag through
`lexstore.runtime_retrieval_catalog_admission`: strict proof first, then the
catalogue's own published checkpoint per scope.

## Decisions

- **`lagging` lives only in the runtime-readiness payload.** `find` keeps its
  states. `build_runtime_readiness` accepts `lagging` as a closed state, counts it
  as ready for `status`, and appends no reason.
- **Proof flag, not inference.** `readiness.retrieval_proven` records that this
  process proved equality since its managed warm began. Every site that sets the
  `retrieval_catalog` event sets it, only while the runtime is managed. A standby
  proves unmanaged, so its proof never counts. `manage_runtime`, `begin_warm`,
  `reset` and `unmanage_runtime` clear it, so a promoted worker re-proves.
- **One helper for the fallback.** The published-projection half of
  `runtime_retrieval_catalog_admission` moves into a helper that schedules no
  repair, so the probe does not run the strict proof a second time. The request
  path keeps its call shape. The probe adds one rule, `require_live_projection`:
  both scopes need a live projection. A lost projection is not a lag. A
  published catalogue from another process instance is served by recall, so
  health follows recall and reports it as a lag, and its age shows a catalogue
  that cannot catch up.
- **Health follows what recall serves.** `find` serves a published catalogue
  only in the strict state `unavailable`, which begins when the whole warm has
  finished. While the optional preloads run, `retrieval_admission` returns
  `warming` and recall refuses. `lagging` therefore needs the strict state
  `unavailable`, and health never says ready while recall refuses.
- **Age measures a stuck catalogue.** `catalogue_unchanged_seconds` counts from
  when this process first saw the current published checkpoint pair while
  lagging, on a monotonic clock, kept as process state. Only a changed pair
  restarts it. A probe that is not lagging does not, and a strictly ready answer
  already implies a newer pair.
- **No cache for `lagging`.** Losing a live projection moves no generation the
  ready-proof cache watches, so each probe proves again while the catalogue
  lags.

## Risks

- Every probe while lagging runs the strict proof and the published-projection
  read. This matches what a recall request already costs.
- A consumer that reads only `retrieval.state == "ready"` as serving would treat
  `lagging` as not serving. None in this repository does; the cell controller
  reads pod readiness.
- No consumer alerts on a large `catalogue_unchanged_seconds` yet. That is
  follow-up work.
