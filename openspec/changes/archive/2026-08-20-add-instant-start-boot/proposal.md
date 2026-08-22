## Why

Today `build_server()` synchronously preloads bge-base + bge-reranker + CLIP (~30s warm-load;
minutes on first-ever download) and runs the lexical cache warm-up before `mcp.run()` listens. A
stdio client (Claude Code) or the remote connector sees dead air at every server start; a
first-ever run can look hung for minutes. Keyword/BM25 `find` needs none of that. This change
makes boot lexical-first and instant: the transport listens immediately, everything warms in one
background daemon thread (`exomem-warm`), and requests that arrive mid-warm degrade softly and
visibly instead of blocking on model locks.

## What Changes

- New `src/exomem/readiness.py`: per-component `threading.Event`s for `lexical`/`embeddings`/
  `reranker`/`clip`; `begin_warm()` / `finish_warm()` / `mark_ready(component)`.
  `should_defer(component)` is true only while a warm is active and the component isn't ready yet
  — once `finish_warm()` runs, it is false forever, so a FAILED preload falls back to today's
  inline lazy-load + soft-degrade semantics exactly. `defer(component, item)` atomically records
  deferred write-embed work under the same lock `mark_ready` uses to set-event-and-drain, closing
  the write/drain race. `warming_info()` returns `{components, since_s}` while warming.
- `warmup.py` gains `warm_all(vault_root)`: strict order — existing lexical warm (pages, BM25 both
  scopes, resolver, embedding/CLIP matrices) → mark `lexical` ready → bge preload → mark
  `embeddings` ready + drain deferred write-embeds → reranker preload → mark `reranker` ready →
  CLIP preload → mark `clip` ready → log `warm complete` with durations. Every step keeps the
  existing soft-fail pattern; model preloads still respect `EXOMEM_DISABLE_EMBEDDINGS` /
  `clip_enabled()`. Also `start_background(vault_root)`: `begin_warm()` then a daemon thread
  running `warm_all` with `finish_warm()` in a `finally`.
- `server.py`'s boot block becomes: `EXOMEM_EAGER_BOOT=1` → synchronous `warm_all` (bit-for-bit the
  old blocking behavior, the rollback lever for deploys); else, when warm-up is enabled,
  `start_background()`. Nothing blocks `mcp.run()` — same default for stdio and http.
- Request-path defer gates (the critical piece — the hazard is LOCK-BLOCKING, not exceptions: a
  hybrid `find` calling `get_model()` while the warm thread holds the model lock would block for
  minutes, and the existing try/except fallback never fires): `_find_semantic`'s vector lane, CLIP
  lane, and rerank stage each check `readiness.should_defer(...)` BEFORE touching a model getter;
  when deferring they record the lane as skipped in timings and append the component to a
  request-scoped `degraded` list. `embeddings.upsert_after_write` defers its re-embed work items
  for the post-warm drain instead of loading the model.
- Response marker: when any lane was deferred, `op_find` returns the envelope form
  `{"hits": [...], "warming": {"components": [...], "since_s": N}}` instead of the bare list (the
  declared return type is already `list | dict`; pack/timings envelopes already exist as
  siblings). Window is ~30s per process start; minutes only on first-ever model download.
- Thread-safety hardening for the warm thread racing requests: a double-checked build lock in
  `bm25.BM25Index._fresh_corpus` (one build, others wait — never worse than today's inline cold
  build) and the same pattern for `find._get_query_resolver`. The parsed-page cache stays
  lock-free by design (GIL-atomic dict ops on immutable values; worst case one redundant parse).
- New CLI subcommand `exomem warm`: explicitly pre-downloads/loads bge + reranker (+ CLIP when
  enabled) with HF progress bars on the TTY, optional `--vault` to also warm lexical caches;
  per-step durations; exit 0/1. Lets users/deploy scripts choose when to pay the GB-scale first
  download. Respects `EXOMEM_DISABLE_EMBEDDINGS` (skips with an explanatory message).
- `doctor` (`hybrid`/`media` profiles) gains a read-only `models.cache` check: inspects the local
  HF hub cache dirs for the three models (no network, never downloads — preserves doctor's
  read-only charter); warn-level with remediation "run `exomem warm`".
- `tests/conftest.py` autouse fixture sets `EXOMEM_DISABLE_WARMUP=1` (suite never spawns the warm
  thread); new tests in `tests/test_instant_start.py` drive readiness/warm/defer deterministically
  via injected fakes and events.

No reasoning surface is added. Readiness is process telemetry — it measures what the server is
doing (warm in flight, which components landed), never a judgment about note content. The
`warming` marker exposes that measurement in-band so a caller can tell lexical-only recall from
full recall during the boot window. `exomem warm` is deterministic CLI plumbing around model loads
that already happen today; it changes nothing about when or how a model is used.

## Capabilities

### New Capabilities

- `instant-start`: non-blocking boot for stdio and http, lexical-first background warm ordering,
  non-blocking degradation for `find` lanes that arrive mid-warm, the `warming` response marker,
  deferred write-embedding with post-warm drain, the `EXOMEM_EAGER_BOOT` escape hatch, warm
  readiness logging, and the explicit `exomem warm` model pre-download command.

### Modified Capabilities

- `install-readiness`: the `doctor` `hybrid`/`media` profile gains a read-only `models.cache`
  check for local HF hub model cache presence, remediated by `exomem warm`.

## Impact

- Code: `src/exomem/server.py` (boot block), `src/exomem/warmup.py` (`warm_all`,
  `start_background`), `src/exomem/readiness.py` (new), `src/exomem/find.py` (`_find_semantic`
  defer gates, `_get_query_resolver` build lock), `src/exomem/embeddings.py`
  (`upsert_after_write` defer), `src/exomem/bm25.py` (`BM25Index._fresh_corpus` build lock),
  `src/exomem/commands.py` (`op_find` warming envelope), `src/exomem/__main__.py` (`exomem warm`
  subcommand), `src/exomem/doctor.py` (`models.cache` check).
- Surfaces: the unified `find` command's MCP/CLI/REST envelope gains an optional `warming` sibling
  field (additive — present only when a lane deferred); a new CLI-only `exomem warm` admin verb,
  parallel to `doctor`/`backfill-media` (not on the unified command registry).
- Tests: `tests/test_instant_start.py` (new — readiness state machine, lexical-first warm
  ordering, request-path defer gates, deferred-write drain, eager-boot parity), `tests/conftest.py`
  (`EXOMEM_DISABLE_WARMUP=1` autouse), doctor tests for `models.cache`, CLI tests for `exomem warm`.
- Dependencies: none new. Existing optional embeddings/reranker/CLIP dependencies and their
  soft-fail behavior are unchanged; `readiness` uses only the stdlib `threading`/`time`.
