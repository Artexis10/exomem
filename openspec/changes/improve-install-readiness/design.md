# Design — Install Readiness

## Context

The repo already has the hard pieces for reproducible Python setup: `uv.lock`,
`tool.uv.sources` for the CUDA torch index, CI through `uv run`, optional extras,
and platform service scripts that expect `uv sync`. The mismatch is at the public
entrypoint: local setup still starts with `python -m venv` and `pip install -e .`,
so users bypass the lockfile and then debug import/runtime failures manually.

## Decisions

- **`uv` is the canonical path.** README and SETUP-LOCAL should lead with
  `uv sync` and `uv run python -m kb_mcp ...`. This uses the lockfile and the
  explicit torch source configuration. `pip install -e .` stays documented as a
  fallback because the package still supports normal Python installs.
- **`doctor` is CLI-only.** It diagnoses the local host, environment, PATH,
  imports, and vault path. Exposing that over MCP/REST would be confusing and
  could leak host details to remote clients.
- **Profiles are explicit.** `lean` checks the baseline text/BM25 path. `hybrid`
  adds embeddings dependencies and sidecar state. `media` adds extraction
  dependencies and Tesseract. `remote` adds public URL/OAuth/service env checks.
- **Read-only by construction.** `doctor` never initializes a vault, writes `.env`,
  downloads models, reconciles embeddings, starts services, or mutates files. It
  only inspects env vars, paths, imports, PATH executables, and existing sidecars.
- **Runtime soft-fail behavior is unchanged.** Optional capabilities can still be
  absent at runtime; `doctor --profile <capability>` marks missing components as
  failures only because the user explicitly asked to validate that capability.

## Output Shape

Human output groups checks by status and shows remediations next to failures or
warnings. JSON output is stable:

```json
{
  "success": true,
  "profile": "lean",
  "checks": [
    {"id": "python.version", "status": "pass", "message": "...", "remediation": null}
  ]
}
```

`success` is true when no check has `status == "fail"`.

## Risks

- Importing torch can be slow. Limit the torch import to the `hybrid` and `media`
  profiles, where the user explicitly asked for heavy capability validation.
- A wheel install may not have the repo lockfile beside the package. Treat `uv`
  and console-script discovery as warnings, not lean-profile failures.
