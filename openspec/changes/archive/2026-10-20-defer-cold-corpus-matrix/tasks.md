## 1. Background corpus-matrix load

- [x] 1.1 Serve a managed recall's dense lane only from a resident or caught-up matrix; otherwise start one background load and report `matrix_warming`.
- [x] 1.2 Read the sidecar off the index lock, install and catch up under it, and fall back to one locked full read when writes outran the catch-up bound.
- [x] 1.3 Report a failed load as `failed` behind a backoff that doubles with consecutive failures, cleared by any successful full load.
- [x] 1.4 Make inline readers wait for a running load, keep offline callers on the inline load, and route the startup warm through the same loader without creating a missing sidecar.
- [x] 1.5 Tests, one per behaviour: a cold matrix loads once in the background without stalling a save; a matrix within the catch-up bound serves; one beyond it defers; a failed load reports `failed` without retrying at once; an inline reader waits for the running load; an offline caller loads inline; the warm creates no sidecar.
