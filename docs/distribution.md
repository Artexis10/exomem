# Distribution: getting exomem into each client

One source of truth for every channel: `src/exomem/_scaffold/_Schema/`. Skills are
never hand-copied — each channel is generated from it, and CI fails if a generated
copy drifts.

| Client | MCP server | Skills | Hooks | User effort |
|---|---|---|---|---|
| Claude Code | plugin (auto-registers) or `exomem setup` | all 10 | yes | one command |
| Codex | `exomem setup` | all 10 | yes | one command |
| claude.ai | remote connector (OAuth) | manual upload | **platform has none** | connector + N uploads |
| ChatGPT | custom connector | manual upload | **platform has none** | connector + N uploads |
| Cursor / generic MCP | manual `mcp.json` | none | none | config + `bootstrap()` |

The split is not arbitrary: Claude Code and Codex load skills **from disk**, so we
can install them. The web clients have no filesystem and no install API, so a human
uploads an archive. No amount of engineering closes that gap.

## Claude Code — the plugin

The "marketplace" is not a store you submit to for review. It is **this git
repository**. `.claude-plugin/marketplace.json` at the repo root is the whole
listing, so publishing means merging to `main`:

```
/plugin marketplace add Artexis10/exomem
/plugin install exomem@exomem
```

One install carries the MCP server, all ten skills, and the hooks. Declared
`mcpServers` start automatically when the plugin is enabled — there is no separate
`claude mcp add`.

The plugin tree at `plugins/claude-code/` is **generated**. Never edit it by hand:

```bash
exomem package-skills --plugin-root plugins/claude-code
```

`tests/test_plugin_sync.py` fails if the committed tree and the scaffold disagree,
which is what stops the plugin copy from silently rotting.

Discoverability beyond that is listing the repo in community plugin indexes; there
is no Anthropic review queue to wait on.

## claude.ai and ChatGPT — upload

```bash
exomem package-skills            # dist/skills/*.zip, one per skill
exomem package-skills --vault "/path/to/vault"   # overlay your real project keys
```

Each archive has `SKILL.md` at its root, which is the layout the uploaders expect.
Upload under Settings (claude.ai: Capabilities → Skills; ChatGPT: Skills). Pair it
with a connector pointing at your server — see
[remote-quickstart.md](remote-quickstart.md).

Neither platform has hooks, so capture there is skill-driven rather than automatic.
The `bootstrap()` MCP tool is the fallback contract for any client that can't load
skills at all.

## Release checklist

Release Please handles the version bump and PyPI publish. Around it:

1. Merge to `main` → release-please cuts the tag, publishes to PyPI, pushes GHCR
   images.
2. `exomem package-skills --plugin-root plugins/claude-code` if the scaffold
   changed, and commit the result. CI catches you if you forget.
3. On each self-hosted box: `pwsh -File scripts/upgrade.ps1` (Windows) or
   `bash scripts/upgrade.sh` (macOS/Linux). It asserts the live `/health` version,
   records the managed profile, and reconciles any existing uv-tool CLI to that
   exact release without copying the service's heavy extras.
4. Re-upload the web-client archives only when `SKILL.md` itself changed — the MCP
   surface upgrades with the server, so most releases need no re-upload.

## Why `--transport stdio` is everywhere

The server defaults to **HTTP**. A client config that omits `--transport stdio`
starts a web server on port 8765 instead of speaking MCP, and the client reports a
generic connection failure. Every generated config passes it explicitly; do the
same in anything hand-written.
