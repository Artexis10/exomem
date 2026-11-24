# Proposal

## Why

The product name changes from Exomem to Evermatter before the commercial launch. The product domain is `evermatter.ai`. The company site stays `substratesystems.io`. Users already run the product locally and connect agents to the hosted service. The rename must not break an install, a stored vault, an agent configuration or a hosted connection.

## What Changes

- Every surface that a person reads or types says Evermatter. That covers the PyPI distribution, the CLI, the README and docs, the MCP server title, plugin and skill descriptions, image names, the MCP registry entry, the repository name and the product website.
- Internal identifiers keep the `exomem` name. That covers the Python import package, `EXOMEM_*` settings, state and config paths, vault frontmatter keys such as `exomem_id`, database identifiers, the MCP server key that clients store (and so the `mcp__exomem__*` tool prefix), plugin and skill identifiers, and wire names such as cookies and token audiences.
- The old names keep working through a compatibility window:
  - each release is published under both the `evermatter` and the `exomem` PyPI names;
  - the `exomem` and `kb` commands stay;
  - images already published under the old names stay pullable;
  - GitHub redirects the old repository URL after the move to the `substrate-systems` organisation.
- The hosted service gets hosts on `evermatter.ai`. The old Cloud host keeps serving, so no connected agent signs out. The host move is specified and built in the control-plane repository (`move-cloud-to-evermatter-hosts`), because that code lives there.
- The licence cut of `adopt-fair-source-licence` (its section 3) ships in the same release as this rename.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `exomem-identity`: separates the product name, Evermatter, from the internal name, `exomem`. It adds the external-surface requirement and the compatibility window.

## Impact

- **Packaging:** `pyproject.toml` (distribution name, scripts, URLs), a second build under the `exomem` name, the release workflow's PyPI publish step, the trusted-publisher configuration on PyPI.
- **Distribution:** image names in `cloud-images.yml` and `release-please.yml`, `server.json`, `plugins/*` manifests and generated plugin trees, `docs/capabilities.md`.
- **Docs:** `README.md`, `QUICKSTART.md`, `docs/`, the shipped scaffold's prose, `TRADEMARK.md`.
- **Code:** user-facing strings only (CLI help and banners, MCP server title and instructions, doctor and setup output). No module, setting or path rename.
- **Other repositories:** the website, the hosted gateway, the OAuth authorization server and Cloud email live in the control-plane repository and follow its own change.
- **Live sessions:** about fifteen open pull requests touch `src/exomem`. Because the module and settings keep their names, the rename does not conflict with them beyond the docs and string lines it edits.
