# Design — Retrieve-Inject Hook

## Context

`kb_retrieve_nudge.py` is a standalone, stdlib-only Python script (the
`.sh` sibling is a thin wrapper that just resolves `python3`/`python` and
`exec`s it — see `kb-retrieve-nudge.sh`). It is deliberately decoupled from the
`exomem` package: it never `import exomem`, so it keeps working even when the
interpreter Claude Code's shell resolves is not the one `exomem` is installed
into. Today it reads the `UserPromptSubmit` event JSON off stdin, gates on
prompt length (`KB_RETRIEVE_NUDGE_MIN_CHARS`, default 20) and a per-session
cooldown file (`KB_RETRIEVE_NUDGE_COOLDOWN_SEC`, default 300), and on a pass
prints `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
"additionalContext": REMINDER}}` — a static instruction string, never real KB
content.

Two retrieval surfaces already exist and are reused as-is:

- **REST** (`server.py`): `POST /api/find` is part of the generated REST
  facade (`_register_rest`, `commands.py` registry entry
  `("find", op_find, 1, False, False, "query", _MCRC)` — `find` carries the
  `rest` surface). It is **POST with a JSON body**, gated by
  `Authorization: Bearer <EXOMEM_REST_API_KEY>` (constant-time compare or a
  minted `rest`-scoped token), disabled (503) unless `EXOMEM_REST_API_KEY` is
  set server-side. The response is the shared envelope
  `{"success": true, "data": [...compact hit dicts...]}` or
  `{"success": false, "error": {...}}`. Default bind is `127.0.0.1:8765`.
- **CLI** (`__main__.py` → `_core_op_main`): `python -m exomem find --detail
  compact --limit 3 --mode keyword --json "<query>"` (or the installed
  console scripts `exomem`/`kb`, declared in `pyproject.toml`'s
  `[project.scripts]`). `__main__.py` imports `from . import server` at
  module scope, so even a keyword-only `find` invocation pays the import cost
  of `fastmcp`, `starlette`, and the GitHub OAuth provider chain before any
  query runs — cold-start is dominated by that import chain plus interpreter
  startup, not by corpus size or by loading any embedding model (keyword mode
  never touches `bge`/CLIP/reranker).
- **Compact stub shape** (`find.py::Hit.as_compact_dict`): `path`, `type`,
  `scope`, `title`, `updated`, plus optional `media_type`, `media_file`,
  `clip_match_at`, `scene_frame`(+`scene_match_at`), `transcript_match_at`,
  `outside_kb`, `status`, `superseded_by`. `excerpt` and `signals` are never
  present in this shape — there is no separate guard needed to keep bodies out
  of the injected block.

## Goals / Non-Goals

**Goals:**

- Turn the existing instruction-only nudge into real retrieved content when a
  fast path exists, without adding a second registered hook or a second
  cooldown clock.
- Never make `UserPromptSubmit` noticeably slower on an install where fast
  retrieval isn't available — REST unconfigured/unreachable and CLI transport
  not explicitly opted into must cost exactly what today's hook costs (no
  network call attempted at all).
- Keep the hook stdlib-only (no new runtime dependency, no shelling out to
  `curl`/`jq`).
- Keep the default (`KB_RETRIEVE_INJECT` unset) behavior byte-identical to
  today, verified by the existing `test_install_hook.py` retrieval-gate tests
  passing unmodified.

**Non-Goals:**

- No change to `find`'s ranking, registry, REST route, or CLI wiring — this
  change is a consumer of those surfaces, not a modifier.
- No hybrid/vector retrieval from the hook (see Decision: keyword-only).
- No excerpt/body injection (see Decision: stubs only).
- No new always-on process/daemon.

## Decisions

### In-place upgrade behind `KB_RETRIEVE_INJECT`, not a second hook

A sibling `kb-retrieve-inject.sh` would mean two `UserPromptSubmit`
registrations, two cooldown clocks, and a real risk of firing twice (nudge +
inject) on the same prompt with no shared gate. Upgrading `kb_retrieve_nudge.py`
in place keeps `install_hook.py`'s `_HOOK_SPECS`, the settings.json wiring, and
the default hook `timeout` (10s) completely unchanged, and makes the inject
payload strictly additive to the existing gate rather than a parallel trigger.
`KB_RETRIEVE_INJECT` follows the `_env_flag` truthy-parse convention introduced
alongside diarization/vision-caption gating (`extract.py::_env_flag`: unset,
`""`, `0`, `false`, `no`, `off` → disabled, case-insensitive) rather than a bare
`bool(os.environ.get(...))` check — the same bug class that check just got
fixed for elsewhere in this repo (`KB_DIARIZE=0` reading as opted-in) must not
be reintroduced here.

### Transport ladder: REST first, CLI opt-in second, nudge-only floor

1. **REST**, attempted only when `EXOMEM_REST_API_KEY` is present in the
   hook's own environment (the shell that launches Claude Code — not
   necessarily the server's service environment; documented as a setup
   requirement). One POST attempt, `timeout=2s` at the socket level. Any
   failure — connection refused, timeout, non-200, malformed JSON,
   `success: false` — is treated as "unreachable" and falls straight through
   to the next rung. No separate health-check round trip; the real call
   doubles as the reachability probe, keeping this to exactly one request.
2. **CLI**, attempted only when REST was not attempted or failed, **and**
   `KB_RETRIEVE_INJECT_CLI` is truthy. Locate `exomem` then `kb` via
   `shutil.which`; if neither resolves, treat CLI as unavailable (fall to
   nudge-only) rather than trying `sys.executable -m exomem` — the hook's
   interpreter is deliberately not assumed to have `exomem` importable (see
   Context). `timeout=5s` at the subprocess level, generous headroom over the
   observed cold-start range and still well inside the 10s Claude Code hook
   timeout.
3. **Nudge-only floor**: identical to today's output. Reached whenever REST
   isn't configured/reachable and CLI isn't opted in (or isn't resolvable).

`KB_RETRIEVE_INJECT_CLI` is independent of REST reachability by design: a user
who hasn't set `EXOMEM_REST_API_KEY` but wants inject behavior anyway can opt
into the CLI's latency explicitly; a user who has REST configured but hits a
transient outage does **not** silently fall through to a multi-second CLI call
merely because REST failed — that would surprise someone who chose REST
specifically to keep the hook fast. Both routes are still gated by the same
opt-in flags rather than either occurring by default.

### Keyword mode only, never hybrid/vector

`mode="keyword"` is pure BM25-free substring/token matching (`find.py`
docstring: "keyword mode preserves the original case-insensitive substring
matching") — no embeddings, no GPU, no CrossEncoder. Hybrid/vector mode can
report a `warming` envelope while models load in the background after a server
restart (`commands.py`'s `degraded`/`warming` handling) — a hook has no
business reasoning about that state or paying for a cold semantic lane. Fixing
the mode to `keyword` makes the REST/CLI call's latency and behavior
independent of server warm-up state and of whatever embedding/reranker
configuration the install has.

### Stubs only — no excerpts, no bodies, capped block

`detail="compact"` is requested on every call; `Hit.as_compact_dict()`
structurally omits `excerpt` and `signals`, so there is no separate
content-stripping step that could be forgotten. Zero hits → inject nothing
beyond the existing one-line reminder. The formatted stub block (header + up
to 3 `- path (type, updated)` lines) is truncated to ~400 chars with a
trailing marker if it would exceed that, keeping the worst case small and
predictable regardless of title length.

### Same min-chars gate, same cooldown — no new gating knobs

Inject mode reuses `KB_RETRIEVE_NUDGE_MIN_CHARS` and
`KB_RETRIEVE_NUDGE_COOLDOWN_SEC` verbatim. The trivial-prompt gate runs before
any transport attempt (saves the round trip on short prompts exactly as it
does today), and the cooldown continues to bound how often the hook does
network/subprocess work per session, in exchange for not re-fetching on every
single prompt in a long thread. This was considered against firing inject on
every substantial prompt regardless of cooldown (freshest possible recall per
prompt) — rejected because it would turn every prompt in an active session
into a network call (or a multi-second subprocess with CLI opted in), which
contradicts "the hook must stay fast" for the common multi-turn case; the
existing cooldown default (300s) still yields several fresh injections across
a real working session.

## Rejected Alternatives

- **GET `/api/find?query=...` (query string)** — the actual registered REST
  routes are `POST`-only (`_register_rest`, `methods=["POST"]`) with a JSON
  body; a GET would just 405. Corrected from the initial framing of this
  change.
- **Shelling out to `curl`/`jq`** — the existing hooks are explicitly
  stdlib-only Python (not bash-with-curl); reusing `urllib.request` inside the
  already-selected Python process avoids a `curl.exe`/PATH dependency
  (relevant on a minimal Windows install) and an extra process spawn per
  request. Rejected in favor of stdlib `urllib.request` with a short timeout.
- **CLI transport via `sys.executable -m exomem`** — assumes the hook's
  resolved interpreter has `exomem` importable, which is false whenever hooks
  run under a bare system Python while `exomem` lives in a project venv (the
  common case this repo's own hooks are built to survive). Rejected in favor
  of locating the `exomem`/`kb` console script via `shutil.which`, which
  carries its own correct shebang/venv regardless of which Python resolved.
- **CLI-by-default (no opt-in)** — cold-start (interpreter + `fastmcp`/
  `starlette`/OAuth import chain, see Context) is too slow for a hook that
  blocks model start on every substantial prompt; would regress the "hook
  must stay fast" invariant for any install without the REST facade running.
- **Hybrid/vector mode instead of keyword** — couples hook latency/behavior to
  embedding-model warm-up state and GPU availability; rejected for the same
  reason `find`'s own `warming` envelope exists — a hook has no way to wait
  out or reason about that state.
- **Full excerpt/body injection** — token bloat (excerpts run up to
  `EXCERPT_MAX_LEN` — 220 chars — each, in `find.py`) and staleness risk (an
  injected excerpt has no freshness guarantee at read time); a bare stub is a
  pointer the model verifies with `get`, not a claim to trust directly.
- **A separate always-on retrieval daemon** — against this project's
  single-NSSM-service, no-sidecar simplicity; would need its own lifecycle,
  health checks, and update path for a problem the existing service already
  solves when reachable.

## Risks / Trade-offs

- A user sets `EXOMEM_REST_API_KEY` in the server's service environment but
  not in the shell that launches Claude Code → REST rung is skipped (falls to
  CLI-opt-in-or-nudge) even though the service is up. Documented in
  SETUP-LOCAL as a setup requirement (export it in the same profile Claude
  Code inherits from); not auto-detected, since the hook process cannot read
  another process's environment.
- Legacy `KB_MCP_REST_API_KEY`-only installs (pre-rename env var, promoted to
  `EXOMEM_REST_API_KEY` inside the `exomem` package at import time via
  `env_compat.promote_legacy()`) are not auto-promoted here, because the hook
  deliberately never imports `exomem`. Such installs fall through to
  CLI-opt-in-or-nudge until they set `EXOMEM_REST_API_KEY` directly — a scoped
  limitation, not a regression (today's hook doesn't read either var).
- A REST call that times out right at the 2s boundary still costs ~2s on that
  one prompt before falling back — bounded and rare (only on an unreachable
  server with a slow-failing connection, e.g. a firewall black-hole rather
  than a clean refusal), well inside the 10s hook timeout.

## Migration Plan

No data migration. `KB_RETRIEVE_INJECT` unset is the shipped default — every
existing install is unaffected until a user opts in. `install_hook.py` needs
no re-run for existing installs (same script path, same wrapper, same
settings.json entry); the upgraded script is picked up next time
`install-hook` is (re-)run, or immediately for anyone who edits
`~/.claude/hooks/kb_retrieve_nudge.py` via a fresh `exomem` install/upgrade.

## Open Questions

None for implementation.
