## Why

The retrieve hook's inject mode (`EXOMEM_RETRIEVE_INJECT=1`) is meant to append
up to three KB routing stubs to the reminder so prior pages are in context
before the agent decides whether to search. On a real prompt it appended
nothing, and the log line could not tell that apart from success.

Two causes, both in the hook. It queried `ask_memory` in `keyword` mode, which
is an all-tokens-present gate over the whitespace-split query: any prompt with a
punctuation-attached token or one term absent from every page returns zero
hits, and every substantive prompt has both. Measured on one vault
(2026-09-11): a two-word domain phrase 3 hits, the same phrase with a colon 0, a 3 KB
ticket prompt 0; hybrid mode 3 relevant hits for the same prompt in 1.5 s over
REST and 2.4 s via the CLI. On a maintainer's transcripts the stub rate was
63% before the 0.73.1 hook refresh and 2% after. The REST rung also never ran
on a managed install, because `EXOMEM_REST_API_KEY` is persisted only in the
service `EnvironmentFile` and never in the client shell (#1142), so the slower
CLI rung carried every prompt.

## What Changes

- The inject lane queries in hybrid mode on both rungs and reads the hit list
  out of either REST envelope shape (`data` as a list, or `data.hits` when the
  facade attaches a marker such as `degraded` or `warming`).
- The REST socket timeout rises from about 2 s to 4 s; both rungs draw on one
  shared wall-clock budget of 8 s under the registered 10 s hook timeout, and
  a rung that would start with under 0.5 s left is skipped.
- When `EXOMEM_REST_API_KEY` is absent from the environment, the hook reads it
  from the managed install's `service.env` (Linux `$XDG_CONFIG_HOME/exomem/`,
  macOS `~/Library/Application Support/Exomem/`, none on Windows;
  `EXOMEM_SERVICE_ENV` overrides), reversing the installer's `systemd_quote`
  escaping. A key read from disk is sent only to a loopback host; a
  non-loopback `EXOMEM_HOST` disables the REST rung for it. A key exported into
  the environment keeps today's behaviour.
- The stub block is bounded by whole lines (600 chars): a line that does not
  fit is dropped and counted in a trailing `- … N more not shown` marker, never
  cut inside a path.
- The session and client-wide cooldown stamps are written after the transport
  ran, so a hook the client kills mid-ladder does not also silence the next
  cooldown window.
- Each fired nudge logs `lane=<rest|cli|none|off> hits=<n>` ahead of the prompt
  head, never the key.

Not in this change: `install-hook --check` reporting which inject lane is
reachable (the second half of #1142), and the nudge firing on harness task
notifications (#1141).

## Impact

- `src/exomem/_hooks/exomem_retrieve_nudge.py` and its verbatim copy under
  `plugins/claude-code/hooks/`; `tests/test_retrieve_inject.py`;
  `QUICKSTART.md`; `benchmarks/membench/trackc/injection_ladder.py` comments.
- Spec `retrieve-inject-hook`: REST-first transport, CLI fallback and bounded
  stub block requirements modified; key resolution, loopback binding, shared
  budget, late cooldown stamp and lane logging added.
- Hybrid recall embeds the prompt server-side (or in the CLI process), so an
  inject-mode prompt costs about 1.2 s over REST and 2.5 s via the CLI on a
  laptop CPU lane, against about 1 s today for a block that was always empty.
