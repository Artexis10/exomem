# retrieve-inject-hook Specification

## Purpose
TBD - created by archiving change retrieve-inject-hook. Update Purpose after archive.
## Requirements
### Requirement: Recall Injection Defaults Off

The `UserPromptSubmit` retrieve hook (`exomem_retrieve_nudge.py`) SHALL keep its
current reminder-only `additionalContext` behavior unchanged unless
`EXOMEM_RETRIEVE_INJECT` is set truthy (per the repo's `_env_flag` truthy-parse
convention: unset, `""`, `0`, `false`, `no`, `off` — any case — count as unset).
The legacy `KB_RETRIEVE_INJECT` name is still accepted (aliased onto
`EXOMEM_RETRIEVE_INJECT` at startup). No inject-mode code path SHALL be reached,
and no network or subprocess call SHALL be attempted, when `EXOMEM_RETRIEVE_INJECT`
(nor its legacy alias) is set.

#### Scenario: Default install is untouched

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is not set in the hook's environment
- **THEN** a `UserPromptSubmit` event that passes the existing min-chars and
  cooldown gates produces the exact same `additionalContext` reminder string
  the hook produces today
- **AND** no REST request or CLI subprocess is attempted

### Requirement: REST-First Transport When Configured And Reachable

The hook SHALL attempt exactly one `POST http://127.0.0.1:8765/api/find` request
(`detail=compact`, `mode=keyword`, `limit=3`, `Authorization: Bearer
<EXOMEM_REST_API_KEY>`) with a socket timeout of about 2 seconds, before
considering any other transport, whenever `EXOMEM_RETRIEVE_INJECT` is truthy and
`EXOMEM_REST_API_KEY` is present in the hook's own environment. The hook SHALL
treat any failure of that request (connection error, timeout, non-200 status,
malformed JSON, or an envelope with `success: false`) as "REST unreachable."

#### Scenario: REST configured and reachable

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is set, and
  the local REST facade answers with `{"success": true, "data": [...compact
  hits...]}`
- **THEN** the hook's `additionalContext` includes a routing-stub block built
  from those hits
- **AND** the CLI transport is never attempted

#### Scenario: REST configured but unreachable, CLI not opted in

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is set, the
  REST request fails (any of: connection error, timeout, non-200, malformed
  JSON, `success: false`), and `EXOMEM_RETRIEVE_INJECT_CLI` is not set truthy
- **THEN** the hook falls back to today's reminder-only `additionalContext`
- **AND** no CLI subprocess is attempted

### Requirement: Opt-In CLI Transport Fallback

The hook SHALL locate an installed `exomem` or `kb` console script via `PATH`
lookup and invoke it as `find --detail compact --limit 3 --mode keyword --json
<prompt>` (subprocess timeout of about 5 seconds) whenever REST was not
attempted (no `EXOMEM_REST_API_KEY`) or failed, and `EXOMEM_RETRIEVE_INJECT_CLI` is
set truthy. The hook SHALL treat any failure of that invocation (console
script not found, non-zero exit, malformed JSON, timeout) the same as "no
hits."

#### Scenario: REST unconfigured, CLI transport opted in

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is unset, and
  `EXOMEM_RETRIEVE_INJECT_CLI` is truthy, and an `exomem` or `kb` console script is
  resolvable on `PATH`
- **THEN** the hook invokes that console script's `find` command and, on
  success, includes a routing-stub block built from its compact hits in
  `additionalContext`

#### Scenario: Neither REST nor CLI transport available

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is unset, and
  `EXOMEM_RETRIEVE_INJECT_CLI` is not set truthy (or no `exomem`/`kb` console
  script resolves on `PATH`)
- **THEN** the hook falls back to today's reminder-only `additionalContext`
- **AND** no REST request or CLI subprocess is attempted

### Requirement: Zero Results Inject Nothing Extra

The hook SHALL inject nothing beyond the existing one-line reminder — never an
empty stub-block header, never a "no results" message — when the active
transport returns zero compact hits for the prompt.

#### Scenario: A resolved transport returns no hits

- **WHEN** inject mode is active and the REST or CLI transport succeeds but
  returns zero hits
- **THEN** `additionalContext` is exactly today's reminder-only text, with no
  additional stub-block header or placeholder

### Requirement: Injected Content Is Bounded, Stub-Only Routing Data

The injected block SHALL contain only routing-stub fields already present in
`find(detail="compact")` output (`path`, `type`, `updated`, and any other
compact-dict field) — never `excerpt`, `signals`, or any other page body/text.
It SHALL show at most 3 hits and SHALL be truncated to approximately 400
characters if the formatted block would otherwise exceed that.

#### Scenario: Three or fewer hits are shown verbatim

- **WHEN** the active transport returns 1 to 3 compact hits
- **THEN** the injected block contains one line per hit, in the order
  returned, each showing only `path`, `type`, and `updated`
- **AND** no hit's `excerpt` or `signals` (neither requested nor present in
  compact mode) appears anywhere in the block

#### Scenario: An oversized block is truncated, never hit-count-limited beyond 3

- **WHEN** the formatted routing-stub block (3 hits, long titles/paths) would
  exceed approximately 400 characters
- **THEN** the block is truncated to approximately 400 characters with a
  trailing truncation marker
- **AND** the request itself never asked for more than 3 hits (`limit=3`)

### Requirement: Injection Reuses The Existing Prompt-Length Gate And Cooldown

Inject mode SHALL reuse `EXOMEM_RETRIEVE_NUDGE_MIN_CHARS` (checked before any
transport is attempted) and the existing per-session cooldown
(`EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC`) exactly as the reminder-only path does
today. Inject mode SHALL NOT introduce a second, independent trigger or a
second cooldown clock.

#### Scenario: A trivial prompt skips inject mode entirely

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy and the prompt is shorter than
  `EXOMEM_RETRIEVE_NUDGE_MIN_CHARS`
- **THEN** the hook produces no `additionalContext` at all
- **AND** no REST request or CLI subprocess is attempted

#### Scenario: The per-session cooldown suppresses a second fire

- **WHEN** inject mode fired (REST or CLI) once for a session and a second
  qualifying prompt arrives before `EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC` has
  elapsed
- **THEN** the second call produces no `additionalContext`
- **AND** no REST request or CLI subprocess is attempted for that second call

### Requirement: Obvious Control Prompts Stay Silent

The hook SHALL suppress obvious short control/status/acknowledgement prompts
before emitting reminder context or attempting any inject-mode transport. Examples
include "continue", "thanks", "are you done?", "perfect merge then to main", and
"restart the server". The skip gate SHALL be conservative: prompts that explicitly
touch KB-bearing topics (for example `kb`, `knowledge base`, `Exomem`, `notes`,
`save`, `remember`, prior conclusions, previous decisions, or "have I looked at")
SHALL still flow through the normal prompt-length and cooldown gates.

#### Scenario: Control-only prompt skips the hook

- **WHEN** a `UserPromptSubmit` event contains a short control-only prompt such as
  "perfect merge then to main gj"
- **THEN** the hook produces no `additionalContext`
- **AND** no REST request or CLI subprocess is attempted

#### Scenario: KB-bearing prompt still gets the reminder

- **WHEN** a prompt asks "what did I conclude about the kb hook design earlier?"
- **THEN** the prompt is not suppressed by the control-prompt gate
- **AND** the hook continues through the existing min-chars and cooldown gates

### Requirement: The Hook Never Blocks Indefinitely Or Raises

Every transport attempt SHALL be wrapped so that any exception (network
error, timeout, subprocess failure, malformed JSON) is caught and treated as
"no hits from this rung," never propagated. The hook process SHALL always
exit `0`.

#### Scenario: An unexpected transport error still exits cleanly

- **WHEN** the REST or CLI transport raises any exception not explicitly
  anticipated (e.g. a DNS failure, a broken pipe)
- **THEN** the hook catches it, treats that rung as failed, proceeds to the
  next rung or the nudge-only floor
- **AND** the hook process exits `0` with no traceback on stderr affecting the
  Claude Code session
