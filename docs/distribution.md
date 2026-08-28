<!-- authority:non-specification -->

# Distribution: getting Exomem into each client

The scaffold at `src/exomem/_scaffold/_Schema/` is the source of truth for skill
artifacts. Generated skill copies are checked by CI; hosted definitions,
marketplace metadata, hooks, fixtures, and promotion records are maintained by
their own channel contracts.

## Distribution channels

| Surface | Canonical channel | What users install or connect |
|---|---|---|
| Claude Code plugin | This Git repository's Claude Code plugin marketplace | `Artexis10/exomem` plugin (skills and hooks), plus a configured remote or stdio MCP route |
| Claude.ai, Desktop, Mobile, Code, and Cowork | Claude Connector Directory plus the independent public Claude plugin channel | The hosted connector; the plugin bundle adds skills where the client supports them |
| Hosted ChatGPT and Codex | One universal OpenAI Plugin Directory entry | The hosted OpenAI plugin and its MCP connection |
| Self-hosted/local Claude Code and Codex | `exomem setup` (or explicit `codex mcp add`) | A local or remote MCP route plus disk-installed skills and hooks |
| Cursor and generic MCP clients | Client MCP configuration | A local stdio or remote HTTP MCP route; `bootstrap()` supplies the portable contract when skills are unavailable |

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

The plugin does not create a usable server route when `mcp_url` is blank. After
installing it, configure one of the supported routes:

```bash
exomem setup --mcp-url https://<host>/mcp
# or, for a local server
exomem setup --stdio
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

## Self-hosted and generic MCP clients

Self-hosted routes are supported channels, not directory fallbacks. Run
`exomem setup` for Claude Code or Codex, or register Codex explicitly with
`codex mcp add` as described in the
[AI assistant guide](ai-assistant-guide.md#codex-cli). Cursor and other generic
MCP clients can use either a local stdio command or a remote HTTP endpoint; see
[remote-quickstart.md](remote-quickstart.md) for the remote path.

## Fallbacks

Use manual skill archives or custom instructions when a client has no supported
skill channel. Generate archives with:

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

Release Please handles versioning and the release workflow handles enabled
publication channels. Around a release:

1. Merge feature and fix PRs to the default branch; Release Please opens or
   updates a release PR.
2. After that PR passes CI, merge it. Release Please creates the tag and GitHub
   Release; the release workflow publishes PyPI and GHCR only when
   `PYPI_PUBLISH_ENABLED=true` and `GHCR_PUBLISH_ENABLED=true`, respectively.
3. Regenerate `plugins/claude-code/` if the scaffold changed; CI checks sync.
4. Upgrade each self-hosted service with `scripts/upgrade.ps1` or
   `scripts/upgrade.sh` and verify its live health version.
5. Re-upload fallback web-client archives only when `SKILL.md` changed.

## MCP transport

The server defaults to HTTP. Hand-written stdio client configuration must pass
`--transport stdio`; omitting it starts an HTTP server instead of an MCP stdio
session.
