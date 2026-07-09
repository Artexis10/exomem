## Context

The macOS installer currently renders a launchd plist around the repository
`.venv`; Linux requires users to render and install a systemd unit manually.
Windows PR #178 introduces a separate PyPI release venv, dotenv propagation, and
profile doctor gate, but does not verify the running endpoint. The Unix path must
provide the same product guarantee without requiring root or coupling the service
runtime to a checkout.

## Goals / Non-Goals

**Goals:**

- Make one rerunnable command create or update a native release service on Linux
  and macOS.
- Keep profile extras, service environment, preflight, and post-start health
  semantics aligned across Windows, Linux, and macOS.
- Preserve the existing repository-venv path for contributors.

**Non-Goals:**

- Replace Docker deployment or add a universal package-manager installer.
- Add system-wide/root services; Unix installs remain per-user.
- Automate public tunnel provisioning or GitHub OAuth application creation.

## Decisions

1. **One Unix entry point dispatches by OS.** `scripts/install-service.sh` handles
   launchd and systemd user services. This keeps the product command and profile
   mapping identical while retaining native templates.

2. **Release mode is explicit and documented first.** `--release` creates an
   external venv with `uv venv` and upgrades a PyPI requirement. `--repo-dev`
   selects the checkout `.venv`; an invocation with no mode flag retains the
   historical repo-dev behavior to avoid silently changing existing automation.

3. **Release state uses per-user platform directories.** Linux uses
   `${XDG_DATA_HOME:-~/.local/share}/exomem/service`; macOS uses
   `~/Library/Application Support/Exomem/service`. `--service-root` overrides the
   location. The venv and logs use the OS data root while generated environment
   files use the OS config root, so the running release service does not depend
   on repository files.

4. **The installed package parses dotenv.** After the venv is ready, its Python
   and `python-dotenv` parse the selected `.env`. The installer exports the map for
   doctor, writes a `0600` systemd environment file, and emits escaped launchd
   `EnvironmentVariables`. This avoids shell-sourcing arbitrary dotenv syntax and
   avoids relying on service working directory discovery.

5. **Preflight and endpoint checks define success.** The selected capability
   doctor and `doctor --profile remote` run before any service-manager mutation.
   After start, the installer polls `/mcp`; `401` is success, while `200` is an
   authentication failure. A failed post-start check stops the service but leaves
   its installed definition and logs for diagnosis.

6. **Published extras mirror Windows.** Lean installs `exomem`, hybrid adds
   `embeddings`, and media adds `embeddings,media,vision,diarization`; macOS arm64
   also adds `media-mlx`. An optional package version pins the same requirement.

## Risks / Trade-offs

- **User systemd linger may require policy approval** -> attempt to enable linger
  and warn with the exact command when the host refuses; the service still runs in
  the active user session.
- **Secrets are materialized for service managers** -> restrict generated files to
  the current user and never print values.
- **A package upgrade can fail before preflight** -> fail before changing the
  service definition and retain the existing installed service for recovery.
- **Platform services cannot be exercised in normal CI** -> test argument,
  rendering, ordering, and status logic with temporary homes and command shims;
  keep real-host verification as a documented release smoke.
