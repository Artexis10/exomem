## MODIFIED Requirements

### Requirement: REST-First Transport When Configured And Reachable

The hook SHALL attempt exactly one `POST http://<host>:8765/api/ask_memory`
request (`detail=compact`, `mode=hybrid`, `limit=3`, `Authorization: Bearer
<key>`) with a socket timeout of about 4 seconds, bounded by the shared inject
budget, before considering any other transport, whenever `EXOMEM_RETRIEVE_INJECT`
is truthy and a key resolves (from the hook's environment, or from the managed
install's `service.env`). `<host>` is `EXOMEM_HOST` or `127.0.0.1`. The hook
SHALL treat any failure of that request (connection error, timeout, non-200
status, malformed JSON, or an envelope with `success: false`) as "REST
unreachable." The hook SHALL read the hit list from either envelope shape the
facade emits: `data` as a list, or `data.hits` when the facade attaches a marker
such as `degraded` or `warming`.

#### Scenario: REST configured and reachable

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, a key resolves, and the local
  REST facade answers with `{"success": true, "data": [...compact hits...]}` or
  `{"success": true, "data": {"hits": [...], ...}}`
- **THEN** the hook's `additionalContext` includes a routing-stub block built
  from those hits
- **AND** the CLI transport is never attempted

#### Scenario: REST configured but unreachable, CLI not opted in

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, a key resolves, the REST request
  fails (any of: connection error, timeout, non-200, malformed JSON,
  `success: false`), and `EXOMEM_RETRIEVE_INJECT_CLI` is not set truthy
- **THEN** the hook falls back to today's reminder-only `additionalContext`
- **AND** no CLI subprocess is attempted

#### Scenario: A punctuated or long prompt still finds its pages

- **WHEN** the gated prompt is a pasted ticket or a sentence with a colon or a
  comma, and the vault holds pages about its subject
- **THEN** the request is made in hybrid mode and the block carries those pages
- **AND** the hook never queries in keyword mode, whose all-tokens gate returns
  nothing for such a prompt

### Requirement: Opt-In CLI Transport Fallback

The hook SHALL locate an installed `exomem` or `kb` console script via `PATH`
lookup and invoke it as `ask_memory --detail compact --limit 3 --mode hybrid
--json <prompt>` (subprocess timeout of about 5 seconds, bounded by the shared
inject budget) whenever REST was not attempted (no key resolved, or a
file-sourced key with a non-loopback host) or failed, and
`EXOMEM_RETRIEVE_INJECT_CLI` is set truthy. The hook SHALL treat any failure of
that invocation (console script not found, non-zero exit, malformed JSON,
timeout) the same as "no hits."

#### Scenario: REST unconfigured, CLI transport opted in

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, no key resolves, and
  `EXOMEM_RETRIEVE_INJECT_CLI` is truthy, and an `exomem` or `kb` console script is
  resolvable on `PATH`
- **THEN** the hook invokes that console script's `ask_memory` command in hybrid
  mode and, on success, includes a routing-stub block built from its compact
  hits in `additionalContext`

#### Scenario: Neither REST nor CLI transport available

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, no key resolves, and
  `EXOMEM_RETRIEVE_INJECT_CLI` is not set truthy (or no `exomem`/`kb` console
  script resolves on `PATH`)
- **THEN** the hook falls back to today's reminder-only `additionalContext`
- **AND** no REST request or CLI subprocess is attempted

### Requirement: Injected Content Is Bounded, Stub-Only Routing Data

The injected block SHALL contain only routing-stub fields already present in
`ask_memory(detail="compact")` output (`path`, `type`, `updated`) — never
`excerpt`, `signals`, or any other page body/text. It SHALL show at most 3 hits
and SHALL be bounded to 600 characters by whole lines: a stub line that would
not fit is dropped and counted in one trailing `- … N more not shown` marker
line, and no path is ever cut. When not even the first stub line fits, the hook
SHALL inject nothing beyond the reminder.

#### Scenario: Three or fewer hits are shown verbatim

- **WHEN** the active transport returns 1 to 3 compact hits whose lines fit
  the bound
- **THEN** the injected block contains one line per hit, in the order
  returned, each showing only `path`, `type`, and `updated`
- **AND** no hit's `excerpt` or `signals` (neither requested nor present in
  compact mode) appears anywhere in the block

#### Scenario: An oversized block drops whole lines, never part of a path

- **WHEN** the formatted routing-stub block (3 hits, long titles/paths) would
  exceed 600 characters
- **THEN** the block keeps the header and the leading stub lines that fit
  whole, followed by `- … N more not shown`
- **AND** every path in the block is exactly a path the transport returned
- **AND** the request itself never asked for more than 3 hits (`limit=3`)

## ADDED Requirements

### Requirement: REST Key Resolves From The Managed Install

When `EXOMEM_REST_API_KEY` is absent from the hook's environment, the hook SHALL
read it from the managed install's `service.env`: `$XDG_CONFIG_HOME/exomem/service.env`
(default `~/.config/exomem/service.env`) on Linux, `~/Library/Application
Support/Exomem/service.env` on macOS, none on Windows, with `EXOMEM_SERVICE_ENV`
overriding the location. The read SHALL take the first `EXOMEM_REST_API_KEY=`
line, strip one layer of matching quotes, reverse the installer's double-quote
escaping of `\` and `"`, tolerate a leading byte-order mark, and never raise.
The key value SHALL never appear on stdout, in the log, or in an error message.

#### Scenario: Key only in service.env

- **WHEN** `EXOMEM_REST_API_KEY` is unset and `service.env` holds
  `EXOMEM_REST_API_KEY="<key>"`
- **THEN** the REST rung runs with that key against a loopback host
- **AND** the CLI rung is not attempted while REST is reachable

#### Scenario: Environment wins over the file

- **WHEN** both the environment and `service.env` carry a key
- **THEN** the environment's value is used

### Requirement: A File-Sourced Key Travels Only To Loopback

A key the hook read from `service.env` SHALL be sent only when the REST host is
a loopback literal (`127.0.0.1`, `localhost`, `::1`, `[::1]`). With any other
`EXOMEM_HOST`, the hook SHALL not attempt the REST rung for that key and SHALL
continue to the CLI rung or the reminder-only floor. A key present in the hook's
environment is not subject to this bind.

#### Scenario: Attacker-set host with a disk key

- **WHEN** `EXOMEM_REST_API_KEY` is unset, `service.env` holds a key, and
  `EXOMEM_HOST` names a non-loopback host
- **THEN** no request is made to that host
- **AND** the ladder continues to the CLI rung when opted in, else to the floor

### Requirement: Both Rungs Share One Wall-Clock Budget

The REST and CLI rungs SHALL draw on one budget of 8 seconds measured from the
start of the inject attempt, each receiving at most its own timeout and at most
the time remaining, and a rung that would start with less than 0.5 seconds left
SHALL be skipped. The bound SHALL hold on elapsed time, not only on admission:
the REST rung is waited for no longer than the remaining budget (its socket
timeout is per operation and cannot bound a server that dribbles bytes), and the
CLI subprocess timeout is the remaining budget. The budget sits under the
10 second hook timeout the installer registers.

#### Scenario: A slow REST rung shortens the CLI rung

- **WHEN** the REST rung spends 3.5 seconds before failing and the CLI rung is
  opted in
- **THEN** the CLI subprocess runs with a timeout of about 4.5 seconds

#### Scenario: A server dribbles bytes inside the socket timeout

- **WHEN** the REST facade answers 200 and then emits one byte every 2 seconds
- **THEN** the REST rung is abandoned when the budget runs out and counts as
  failed
- **AND** the hook prints within the budget, so the client's hook timeout is
  never reached

### Requirement: Cooldown Stamps Are Written After The Transport

The per-session and client-wide cooldown stamps SHALL be written after the
inject transport has run (or been skipped), immediately before the hook prints
its output, so a hook the client kills mid-ladder does not consume the next
cooldown window.

#### Scenario: The client kills the hook mid-ladder

- **WHEN** the hook process is terminated while a transport rung is running
- **THEN** no cooldown stamp has been written for that prompt
- **AND** the next qualifying prompt is nudged again

### Requirement: The Log Names The Lane And The Hit Count

Every fired nudge SHALL append one line to the client's retrieve-nudge log of
the form `<timestamp> nudge fired | lane=<rest|cli|none|off> hits=<n> | <prompt head>`,
where `none` marks a fall-through to the reminder-only floor and `off` marks
inject mode not enabled. The line SHALL never contain the key.

#### Scenario: A fall-through is visible

- **WHEN** inject mode is on and neither rung returned hits
- **THEN** the log line reads `lane=none hits=0`
