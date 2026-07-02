# Tasks — remote connector quickstart

## 1. Tests First

- [x] 1.1 Add `tests/test_doctor_probe.py` with an injected/mocked `httpx`
      transport (`httpx.MockTransport`) — no real network calls anywhere in the
      suite:
  - [x] 1.1.1 `401` at `http://127.0.0.1:8765/mcp` → passing check.
  - [x] 1.1.2 `200` JSON at `{EXOMEM_BASE_URL}/.well-known/oauth-authorization-server`
        → passing check.
  - [x] 1.1.3 `200` JSON at the bare `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource`
        with `resource == {EXOMEM_BASE_URL}/mcp` → passing check.
  - [x] 1.1.4 `404` at the bare `oauth-protected-resource` path → failing check
        whose remediation names `mcp_registration_failed`.
  - [x] 1.1.5 A connection-refused/timeout response from the mock transport for
        any of the three URLs → failing check with an actionable message (e.g.
        "tunnel not running").
  - [x] 1.1.6 `resource` field mismatch (wrong host/path) on the bare
        `oauth-protected-resource` response → failing check.
- [x] 1.2 Add a guard test: `doctor(profile="remote")` (no `probe=True`) and
      `doctor(profile="lean"/"hybrid"/"media")` (with or without `probe=True`)
      make zero calls into the `httpx` client seam — assert via a monkeypatched
      client factory that raises if invoked.
- [x] 1.3 Add a CLI test: `exomem doctor --profile remote --probe` wires
      `--probe` through to `doctor(..., probe=True)` (mocked transport); `exomem
      doctor --profile remote` (no flag) does not add probe checks to the
      report.

## 2. Doctor Probe Checks

- [x] 2.1 In `src/exomem/doctor.py`, add a `_probe_client() -> httpx.Client`
      seam (module-level factory, easily monkeypatched) with a short timeout
      (a few seconds).
- [x] 2.2 Add `_check_remote_probes(base_url: str) -> list[DoctorCheck]`
      implementing the three checks from the spec delta: local `/mcp` expecting
      `401`; `{base_url}/.well-known/oauth-authorization-server` expecting `200`
      JSON; bare `{base_url}/.well-known/oauth-protected-resource` expecting
      `200` JSON with `resource == f"{base_url}/mcp"`. Catch `httpx.RequestError`
      (connection refused, timeout, DNS failure) per-check and turn it into a
      failing `DoctorCheck` with an actionable remediation — never let it raise
      out of `doctor()`.
- [x] 2.3 Add `probe: bool = False` to `doctor()`'s signature. When
      `profile == "remote"` and `probe` is true, extend `checks` with
      `_check_remote_probes(os.environ.get("EXOMEM_BASE_URL", ""))` (skip/short-circuit
      cleanly if `EXOMEM_BASE_URL` is unset — the existing `env.EXOMEM_BASE_URL`
      check already reports that failure). `--probe` with any other profile is a
      no-op (no checks added), matching the design decision.

## 3. CLI Wiring

- [x] 3.1 In `src/exomem/__main__.py`'s `_doctor_main`, add
      `parser.add_argument("--probe", action="store_true", help="...")` and pass
      `probe=args.probe` through to `doctor_module.doctor(...)`.

## 4. Docs: Remote Quickstart

- [x] 4.1 Write `docs/remote-quickstart.md`: ingress decision table (Cloudflare
      Tunnel / ngrok / SSH reverse tunnel) first, Tailscale Funnel demoted to a
      footnote; shared numbered steps (GitHub OAuth app → `.env` → start server →
      tunnel up → `exomem doctor --profile remote --probe` with curl equivalents
      → add the claude.ai connector); an ngrok free-tier limits box (120 req/min,
      ~20k req/month, one-time browser interstitial, `429` triage via the ngrok
      console).
- [x] 4.2 Delete `docs/remote-checklist.md`.
- [x] 4.3 Update every in-repo link to `remote-checklist.md` to point at
      `remote-quickstart.md` (`README.md`, `docs/deployment.md`).

## 5. ngrok Script

- [x] 5.1 Add `scripts/setup-ngrok.ps1` mirroring `scripts/setup-cloudflared.ps1`:
      verify ngrok on `PATH` (winget hint if missing), verify an authtoken is
      configured (`ngrok config check`), `-Domain`/`-Port` params, write the
      ngrok config for the static dev domain endpoint, install auto-start via
      `ngrok service install`, print the verification triple (`401`, OAuth
      discovery `200`, bare well-known `200`).
- [x] 5.2 Document the macOS/Linux ngrok equivalent inline in
      `docs/remote-quickstart.md` (`brew install ngrok` + one command) — no
      script.

## 6. `deployment.md` Rework and Repo-Wide Sweep

- [x] 6.1 In `docs/deployment.md`'s "Set up a public HTTPS URL" section: Option A
      becomes Cloudflare Tunnel, Option B becomes ngrok (point at
      `scripts/setup-ngrok.ps1`); Tailscale Funnel demoted to a footnote with the
      throttling rationale, not deleted.
- [x] 6.2 Add a multi-host note for ngrok (one free static domain per account,
      mirroring the existing Cloudflare/Tailscale multi-host guidance).
- [x] 6.3 Add two troubleshooting rows to `docs/deployment.md`'s table: ngrok
      `429` burst throttle (check the ngrok console, upgrade or switch to
      Cloudflare); the ngrok browser interstitial breaking the OAuth redirect on
      first use (click through once, does not recur).
- [x] 6.4 Update `docs/deployment.md`'s link to `remote-checklist.md` (step-5
      "For the short bring-up list" pointer) to `remote-quickstart.md`.
- [x] 6.5 Update `deploy/cloudflared/RUNBOOK.md`'s "No domain?" pointer from
      Tailscale Funnel to ngrok (keep Tailscale mentioned as an existing
      alternative, matching the demoted-not-deleted decision).
- [x] 6.6 Sweep `README.md` for any remaining Tailscale-as-recommended framing
      and the `remote-checklist.md` link; update both.

## 7. Validation

- [x] 7.1 Run the targeted test module:
      `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q tests/test_doctor_probe.py`.
- [x] 7.2 Run the full suite:
      `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q`.
- [x] 7.3 Run `ruff check` on all touched files (`src/exomem/doctor.py`,
      `src/exomem/__main__.py`, `tests/test_doctor_probe.py`).
- [x] 7.4 Run `npm exec --yes @fission-ai/openspec -- validate --changes --strict`
      and the whole-tree `--specs` variant; fix until clean.
- [x] 7.5 Manually verify no dangling links remain to `docs/remote-checklist.md`
      (`git grep -n remote-checklist`).
- [x] 7.6 Pure-substrate check: `_check_remote_probes` only compares HTTP status
      codes and a JSON field to expected literals — no model, no note content, no
      reasoning; a failed probe always resolves to a `DoctorCheck` and never
      raises past `doctor()`.
