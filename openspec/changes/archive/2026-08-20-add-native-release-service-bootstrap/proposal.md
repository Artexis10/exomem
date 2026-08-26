## Why

Linux and macOS native service setup still depends on a repository `.venv`, manual
systemd rendering, and implicit working-directory dotenv loading. Windows PR #178
establishes a PyPI-backed `-Release` path; the native product needs the same
one-command, doctor-gated guarantee on every supported desktop OS.

## What Changes

- Add `--release` and explicit `--repo-dev` modes to the Unix service installer,
  with release mode creating or updating an external PyPI-backed service venv.
- Make the same installer configure launchd on macOS and a systemd user service on
  Linux, including `.env` propagation without relying on repository cwd discovery.
- Map lean, hybrid, and media profiles to their published extras, including the
  macOS arm64 MLX media extra.
- Gate service-manager changes on dependency and remote-environment doctor checks,
  then start the service and require an authenticated `401` response from `/mcp`.
- Extend the Windows release installer with the same post-start `/mcp` verification.
- Document release mode as the blessed product path while retaining repository
  `.venv` mode for contributors.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `install-readiness`: native service installation gains cross-platform release
  environments, explicit service environment propagation, preflight gates, and
  post-start endpoint verification.

## Impact

- Service scripts and templates under `scripts/` for launchd, systemd, and NSSM.
- Deployment and quickstart documentation for the three native platforms.
- Installer contract tests and OpenSpec install-readiness requirements.
- No runtime API or MCP tool schema changes and no new production dependency.
