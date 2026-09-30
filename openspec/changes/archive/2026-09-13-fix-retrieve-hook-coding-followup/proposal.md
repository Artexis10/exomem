## Why

The coding utility benchmark exposed three defects in the retrieve hook: the
configured REST port was ignored, task control events
could trigger retrieval, and the injected stub header gave insufficient
verification guidance.

## What Changes

- Honor valid `EXOMEM_REST_PORT`, default absent/blank values to `8765`, and fail invalid overrides without a REST request while
  preserving the existing `/api/ask_memory` hybrid endpoint in both standalone
  hook copies.
- Ignore actual top-level task notification and stop-hook control inputs before
  retrieval or cooldown state is touched, while allowing ordinary questions
  that merely mention those terms.
- Make the routing-stub header direct diagnostic tasks to read the first
  relevant stub with `read_memory` before investigating, and state that retrieved text is evidence,
  not instructions.

## Impact

The two shipped Python hook copies, their focused regression tests, and this
OpenSpec change. The existing reminder and bounded stub behavior remain intact.
