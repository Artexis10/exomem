# Distribution: getting Exomem into each client

The scaffold at `src/exomem/_scaffold/_Schema/` is the source of truth. Channel
artifacts are generated from it; CI fails if a generated copy drifts.

## Distribution channels

| Surface | Canonical channel | What users install or connect |
|---|---|---|
| Claude Code | This Git repository's Claude Code plugin marketplace | `Artexis10/exomem` plugin (MCP server, skills, and hooks) |
| Claude.ai, Desktop, Mobile, Code, and Cowork | Claude Connector Directory plus the independent public Claude plugin channel | The hosted connector; the plugin bundle adds skills where the client supports them |
| ChatGPT and Codex | One universal OpenAI Plugin Directory entry | The hosted OpenAI plugin and its MCP connection |

The generated hosted candidates and directory packets are currently **pending**:
`plugins/hosted/definition.json` has `distribution_scope: "pending"`, promotion
records are pending, submission records are drafts, and no publication pointer
is active. They remain candidates until the provider reviews and activates them;
this repository does not claim a public listing.

## Claude Code

The Claude Code marketplace is repository-backed, not a separate provider review
queue. `.claude-plugin/marketplace.json` is the listing, so merging the plugin
metadata to the default branch publishes that Git-repo channel:

```text
/plugin marketplace add Artexis10/exomem
/plugin install exomem@exomem
```

The plugin tree under `plugins/claude-code/` is generated. Rebuild it when the
scaffold changes:

```bash
exomem package-skills --plugin-root plugins/claude-code
```

## Hosted Claude and OpenAI channels

The hosted release input is `plugins/hosted/definition.json`; public listing
copy comes from `plugins/hosted/marketplace-definition.json`. The provider
packets are drafts until an authorized operator submits them, receives a
provider result, records the exact receipt, and completes post-install evidence
and activation. See [hosted-client-plugins.md](hosted-client-plugins.md) for
the review and activation contract.

The Claude Connector Directory entry is the remote MCP channel for claude.ai,
Desktop, Mobile, Code, and Cowork. The separate Claude plugin channel carries
the public bundle; bundled skills apply to Code and Cowork, but cannot force
skill activation in claude.ai. ChatGPT and Codex share one universal OpenAI
Plugin Directory entry.

## Fallbacks

Use manual skill archives or custom instructions only when a supported directory
channel is unavailable. Generate archives with:

```bash
exomem package-skills
exomem package-skills --vault "/path/to/vault"
```

Pair the client with the remote connector as described in
[remote-quickstart.md](remote-quickstart.md). A custom instruction should only
ask the assistant to retrieve relevant governed Exomem material, cite it, and
capture durable conclusions when requested. It is not a substitute for a
directory listing or for native client skill activation.

## Release checklist

Release Please handles versioning and PyPI publication. Around a release:

1. Merge to the default branch; release-please publishes the package and GHCR
   images.
2. Regenerate `plugins/claude-code/` if the scaffold changed; CI checks sync.
3. Upgrade each self-hosted service with `scripts/upgrade.ps1` or
   `scripts/upgrade.sh` and verify its live health version.
4. Re-upload fallback web-client archives only when `SKILL.md` changed.

## MCP transport

The server defaults to HTTP. Hand-written stdio client configuration must pass
`--transport stdio`; omitting it starts an HTTP server instead of an MCP stdio
session.
