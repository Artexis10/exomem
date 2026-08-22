## 1. Unix release installer

- [x] 1.1 Add release/repo-dev argument handling, PyPI venv creation, profile extras, and dotenv rendering to `install-service.sh`.
- [x] 1.2 Add idempotent launchd and systemd user-service installation with platform state/log directories.
- [x] 1.3 Gate service changes with capability and remote doctor checks, then verify authenticated `/mcp` health after start.

## 2. Cross-platform parity

- [x] 2.1 Update launchd and systemd templates for explicit service environment, release paths, and configurable bind settings.
- [x] 2.2 Add post-start authenticated `/mcp` verification to the Windows release installer.

## 3. Product guidance and verification

- [x] 3.1 Document the blessed release commands and secondary repo-dev path in quickstart and deployment guidance.
- [x] 3.2 Add installer tests for modes, extras, environment propagation, gate ordering, service-manager rendering, and endpoint status handling.
- [x] 3.3 Run focused tests, syntax checks, OpenSpec validation, diff checks, and the full test suite (the full run reaches a pre-existing TestClient hang also reproducible on `main`).
