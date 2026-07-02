# Design — remote connector quickstart

## Context

`doctor --profile remote` (`src/exomem/doctor.py::_check_remote_env`) today only
checks that `EXOMEM_BASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
`EXOMEM_GITHUB_USERNAME`, and `EXOMEM_JWT_SIGNING_KEY` are *set* — it has no way to
tell a user whether the tunnel is actually forwarding traffic, whether OAuth
discovery resolves through the public hostname, or whether claude.ai's gateway
probe of the bare `/.well-known/oauth-protected-resource` path (handled by the
workaround route in `server.py`, added specifically because FastMCP only serves
the RFC-9728 path-specific variant) is reachable through the live tunnel. Today a
user finds out one of these is broken only when the claude.ai connector add fails
with an opaque error, hours after they believed setup was done.

Separately, `docs/deployment.md` and `docs/remote-checklist.md` recommend
Tailscale Funnel as the default no-domain ingress. That recommendation predates
observed production behavior: Funnel's shared relay throttles the request bursts
claude.ai's connector makes during registration and periodic reconnects, which
looks like "the server is down" when the exomem process is healthy. ngrok's free
tier gives every account one static dev domain — a no-domain option that doesn't
share Funnel's relay-throttling failure mode — but it has never been documented as
a path, so users who don't own a domain have had no good option besides Funnel or
the more involved Cloudflare Tunnel (which requires owning a domain).

## Decisions

### `--probe` is opt-in, not the default

`doctor`'s core value proposition is that it's safe to run blind: no writes, no
network, no side effects, fast enough to run reflexively. Making probing the
default would mean every `doctor --profile remote` call — including ones run
before a tunnel even exists, e.g. right after `.env` is populated — makes three
outbound HTTP requests and reports confusing failures for a setup step the user
hasn't reached yet. `--probe` stays an explicit, separate flag the quickstart doc
tells users to add only after the tunnel is up, so the offline/online distinction
is a deliberate user action, not an implicit behavior change to an existing
command every script and CI job may already depend on being network-free.

### `doctor` owns preflight; no separate `exomem probe` command

**Rejected: a separate `exomem probe` command.** `doctor` already owns "is this
install ready for profile X" for lean/hybrid/media; splitting remote's live-endpoint
checks into a second command would mean two entry points for readiness, two places
to look up remediation text, and a quickstart doc that has to teach users both
commands instead of one command with one new flag. Bolting the checks onto
`doctor` as `DoctorCheck` entries keeps the existing `--json`/human-output
rendering, exit-code convention (`0`/`1`/`2`), and remediation format for free, and
keeps the mental model at "one readiness command per profile."

### Only Windows gets a scripted ngrok setup

**Rejected: scripting ngrok setup on macOS/Linux too.** The Windows script exists
because Windows service installation is genuinely fiddly — see
`scripts/setup-cloudflared.ps1`'s handling of the SYSTEM service config location
and the bare-ImagePath exit-1067 fix, the same class of problem `ngrok service
install` has on Windows. macOS/Linux ngrok setup is `brew install ngrok` followed
by one `ngrok config add-authtoken` / tunnel command — scripting two commands adds
a maintenance surface (a script to keep working across ngrok CLI versions) for no
real reduction in user effort. The quickstart documents the two commands inline
instead.

### Tailscale Funnel is demoted, not deleted

**Rejected: removing Tailscale Funnel entirely.** Existing users already run
Funnel successfully in lower-traffic scenarios, and `deploy/cloudflared/RUNBOOK.md`
already documents a Funnel-to-Cloudflare migration path that depends on Funnel
still being a recognized option. Deleting it would strand those users and any doc
cross-references. It moves to a footnote naming the specific rationale (relay
throttling under claude.ai's connector burst pattern) so a new user sees it only
as "why not," while a current Funnel user reading the same doc still finds it.

### Probe HTTP client is a mockable seam, not real network in tests

The three probes run three plain `httpx` GETs (`httpx` is already a direct
dependency — see `pyproject.toml`, and already used in the auth-cache test via
`httpx.AsyncClient`). `doctor.py` builds the client through a small internal
factory (e.g. `_probe_client() -> httpx.Client`) rather than calling
`httpx.get(...)` module-level, so `tests/test_doctor_probe.py` can monkeypatch
that seam to return a client constructed with `httpx.Client(transport=
httpx.MockTransport(handler))` — httpx's own supported test seam — and assert
exact request URLs/methods without opening a socket. This mirrors the existing
`monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)` pattern in
`tests/test_auth_cache.py` rather than introducing a new mocking approach.

## Risks / Trade-offs

- A probe that times out slows down `doctor --probe` by up to the configured
  per-request timeout (short, a few seconds) three times in the worst case (all
  three endpoints down). Acceptable: this only runs when explicitly requested,
  and a slow/hanging tunnel is exactly the failure mode the user is trying to
  diagnose.
- `--probe` combined with a non-`remote` profile is accepted as a no-op (no
  probe checks are added) rather than a usage error — simpler CLI surface than
  teaching users which profile the flag applies to, at the cost of `--probe`
  silently doing nothing outside `remote`. The human-output rendering makes this
  discoverable: no probe-prefixed check ids appear in the report.
- The bare `/.well-known/oauth-protected-resource` probe is coupled to the
  specific workaround route in `server.py`; if that route's shape changes (e.g.
  FastMCP adds native support and the workaround is removed), the probe's
  assertion on the `resource` field must move with it. This is a known coupling,
  not a hidden one — the proposal names the exact route.

## Migration Plan

No data or server migration. `docs/remote-checklist.md` is deleted in this change
and replaced by `docs/remote-quickstart.md`; every in-repo link to the old path
(`README.md`, `docs/deployment.md`) is updated in the same change so there is no
dangling-link window. Existing Tailscale Funnel deployments keep working
unchanged — the change only reorders documentation precedence and adds an
optional diagnostic; it does not touch `server.py`'s runtime behavior for
existing remote installs.
