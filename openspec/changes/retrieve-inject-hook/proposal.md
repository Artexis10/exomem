## Why

exomem's `UserPromptSubmit` hook (`kb_retrieve_nudge.py`, wired via
`kb-retrieve-nudge.sh`) currently injects an **instruction**: a one-line reminder
telling Claude to run `find` before answering. That is a nudge, not a guarantee —
the model can (and over a long thread, does) forget or skip it. Competitor analysis
(engram-memory) found that the one genuinely mechanistic layer they have is recall
**injection**: retrieved content placed directly into context, with zero model
action required to benefit from it.

exomem already has the ideal payload for this, at zero extra cost to compute:
`find(detail="compact")` routing stubs (`path`, `type`, `scope`, `title`,
`updated`, plus lifecycle/media markers — no `excerpt`, no `signals`), designed
from the ground up to be token-cheap (~15-40 tokens/hit). This upgrades the
retrieve hook from nudge-only to **score-gated retrieve-and-inject**: on a
substantial prompt, fetch the top-3 compact stubs for it and inject them as
`additionalContext` alongside the existing reminder — so relevant prior KB
pages are already in front of the model before it decides whether to search.

This must not regress installs where fast retrieval isn't available. Two real
constraints rule out a naive "always call `find`" design:

1. `UserPromptSubmit` **blocks model start until the hook returns** — the hook
   must stay fast (this is already why the existing hooks are gated + stdlib-only).
2. exomem's own hook scripts are deliberately **stdlib-only and decoupled from the
   `exomem` package** — they run under whatever `python3`/`python` the user's shell
   resolves, which is not guaranteed to be the interpreter/venv that has `exomem`
   (or its heavy dependencies) installed.

The design below (a REST-first, keyword-mode-only, opt-in ladder with a
nudge-only floor) is chosen specifically to respect both constraints.

## What Changes

- `kb_retrieve_nudge.py` gains an **inject mode**, upgraded in place (no new
  sibling hook, no new registered `UserPromptSubmit` entry) behind
  `KB_RETRIEVE_INJECT` (opt-in, default off — byte-identical behavior to today
  when unset). This keeps one hook, one settings.json entry, one cooldown clock.
- **Transport ladder**, evaluated in order, first usable rung wins:
  1. **REST** — if `EXOMEM_REST_API_KEY` is set in the hook's own environment,
     POST `http://127.0.0.1:8765/api/find` with a short (2s) socket timeout,
     `{"query": <prompt>, "detail": "compact", "limit": 3, "mode": "keyword"}`,
     `Authorization: Bearer <key>`. Fast (service is already warm) and the
     common case for anyone who already runs the REST facade.
  2. **CLI (opt-in)** — only if `KB_RETRIEVE_INJECT_CLI` is also set truthy:
     locate the `exomem` (or `kb`) console script on `PATH` and run
     `exomem find --detail compact --limit 3 --mode keyword --json <prompt>`
     with a generous (5s) subprocess timeout. This works on any install but pays
     Python/import cold-start plus a corpus walk per prompt, so it stays opt-in.
  3. **Nudge-only (floor)** — REST not configured/unreachable and CLI not opted
     in: identical output to today. The hook body never blocks or errors past
     this point; any transport failure falls straight through to this rung.
- **Keyword mode only**, never hybrid/vector: no embeddings, no GPU, no
  model-warm-up coupling (`find`'s hybrid path can itself report `warming` while
  models load — the hook must never depend on that state). Pure-substrate: this
  capability runs no model; it places an already-computed BM25/substring lexical
  match into context, with zero added reasoning.
- **Score/relevance gating**: zero compact hits → inject nothing beyond the
  existing one-line reminder (today's behavior, unchanged). Hits are capped at 3
  (the query's own `limit=3`) and the formatted block is capped at ~400 chars.
  Stubs only — `detail="compact"` structurally omits `excerpt`/`signals`, so no
  separate excerpt-stripping guard is needed; body/full content is never fetched
  or injected.
- The prompt-length gate reuses the existing `KB_RETRIEVE_NUDGE_MIN_CHARS`
  (default 20) and the existing per-session cooldown
  (`KB_RETRIEVE_NUDGE_COOLDOWN_SEC`, default 300) unchanged — inject mode is a
  payload upgrade on the same trigger, not a new, independently-firing trigger.
- `install_hook.py` wiring is structurally unchanged: same `_HOOK_SPECS` tuple,
  same `UserPromptSubmit` registration, same default hook `timeout` (10s, ample
  headroom over both the 2s REST and 5s CLI transport timeouts).
- Docs: SETUP-LOCAL's hooks section and README's one-liner gain the inject
  opt-in and its env vars. Scaffold/reference docs stay generic — no change
  needed there (the hook script content is already covered by the existing
  leak-guard).

Out of scope: hybrid/vector retrieval from a hook, a separate always-on
retrieval daemon, injecting excerpts or full page bodies, and any change to
`find`'s ranking, registry, or REST/CLI surface mechanics themselves.

## Capabilities

### Added Capabilities

- `retrieve-inject-hook`: score-gated retrieve-and-inject upgrade to the
  `UserPromptSubmit` KB hook — a REST-first, keyword-mode-only transport ladder
  that injects top-3 compact routing stubs (or falls back to today's
  reminder-only nudge) with no server-side reasoning added.

## Impact

- Code: `src/exomem/_hooks/kb_retrieve_nudge.py` only (in place). No change to
  `src/exomem/_hooks/kb-retrieve-nudge.sh` (still a thin interpreter-resolving
  exec wrapper), `src/exomem/install_hook.py`, `find.py`, `commands.py`, or
  `server.py` — this change consumes the existing `find(detail="compact")` /
  `/api/find` / CLI `find` surfaces as-is, it does not modify them.
- No capability in `openspec/specs/` currently owns hook behavior — the
  capture/retrieve nudge hooks predate this repo's spec-driven process (added
  in `a5400ef`/`6ace72b`, before `openspec/` existed). This proposal adds
  `retrieve-inject-hook` as a new capability to give the `UserPromptSubmit`
  hook's retrieve-and-inject contract a durable spec; it does not touch
  `install-readiness` (uv/doctor local-setup concerns) or any `find-*`
  capability (ranking/registry internals), since neither owns hook behavior and
  neither is modified here.
- Docs: `SETUP-LOCAL.md` section 7 ("Make the KB automatic") and `README.md`'s
  hook one-liner gain the inject opt-in and its tuning env vars.
- Tests: `tests/test_install_hook.py` additions for the byte-identical default
  (`KB_RETRIEVE_INJECT` unset) case, plus a new `tests/test_retrieve_inject.py`
  covering the transport ladder and formatting as pure-logic unit tests
  (network/subprocess calls mocked at a module-level seam — no real network
  call in the suite), matching the `doctor.py`/`test_doctor_probe.py`
  monkeypatched-seam precedent.
