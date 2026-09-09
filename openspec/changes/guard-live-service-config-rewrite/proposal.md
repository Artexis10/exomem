## Why

The release installer renders the managed service environment file entirely from
its selected dotenv and publishes it over the live file with `os.replace`. A
running cell whose configuration has drifted away from that dotenv therefore
loses its real settings with no retained copy, and the replacement can rebind the
service to a different vault.

This happened on 2026-09-09. An upgrade run through `scripts/install-service.sh`
replaced a running cell's environment from a two-month-old checkout dotenv: the
vault path moved from the live vault to an unrelated one, a Windows-only path
returned on a Linux host, and three host-specific keys disappeared. The remote
preflight failed closed on one of the missing keys and left the service stopped,
which is the only reason the cell did not come up serving a different knowledge
base. The prior configuration was unrecoverable.

The installer already accepts that a live binding can outrank the dotenv: it
reads `EXOMEM_STATE_ROOT` back out of the managed file and preserves it across a
re-render. The vault path deserves the same treatment, because the state root,
the index and the graph are all derived from it. Nothing else about the
installer's purpose changes: the dotenv remains the source for reconfiguration.

## What Changes

- Retain the previous managed service environment file before publishing a new
  one, so a rewrite is recoverable rather than destructive.
- Refuse to rebind an existing service to a different vault during a re-render,
  and require an explicit opt-in flag for a deliberate vault move.
- Leave new installs, first-time renders and every other key unchanged, so the
  dotenv stays authoritative for reconfiguration.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `install-readiness`: the release installer's environment render gains a
  retained predecessor and a vault-rebinding refusal for services that already
  exist.
