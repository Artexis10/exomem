## Why

Claude Code and Codex CLI compaction can preserve enough context for the model to continue while still leaving no independent, governed recovery point when a long-running session, connector, or resumed thread loses its working state. Exomem already installs shared read- and write-side reliability hooks into both clients, so it should also provide a local-first compaction checkpoint that survives connector failure without dumping transcripts into compiled memory.

## What Changes

- Add one client-neutral checkpoint core plus thin Claude Code and Codex CLI lifecycle adapters. Both handle `PreCompact(manual|auto)`; Claude Code additionally handles its documented `SessionEnd`, atomically recording bounded per-session recovery checkpoints before compaction and, where supported, at ordinary session exit. Codex CLI 0.144.3 does not expose `SessionEnd` and is not wired for it.
- Capture deterministic working state: session/turn identity, repository/worktree/HEAD, dirty-path summary, active OpenSpec/plan/ledger pointers, unresolved task line numbers, and transcript provenance hashes. Conversation text and tool/system payloads are not copied into the checkpoint.
- Keep the local checkpoint available even when Exomem MCP, OAuth, the network, or the vault is unavailable; use restrictive local permissions and never persist bearer material.
- Add a `SessionStart(source="compact"|"resume")` continuation path for both clients that injects a bounded checkpoint digest and an advisory governed-capture instruction into the resumed model context.
- Inject an advisory keyed by checkpoint ID. The reasoning agent—not the hook or server—uses the structural checkpoint plus its own compacted context to preserve durable stepping-stones in governed Exomem memory when available; the hook does not claim or track capture completion.
- Extend `exomem install-hook --client claude|codex|all` and `--check` to install, wire, migrate, and diagnose the same lifecycle implementation through client-specific configuration adapters while preserving unrelated hooks.
- Define a versioned normalized hook-event/output contract so another command-hook client can be added as an adapter without forking storage, privacy, rendering, or recovery logic. No unsupported client is advertised until its adapter has contract tests.
- Document recovery, privacy, retention, connector-failure, and uninstall/disable behavior.

## Capabilities

### New Capabilities

- `compaction-continuation-checkpoints`: Local-first compaction/exit recovery, compact/resume reinjection, and deferred governed Exomem capture across supported coding-agent clients.

### Modified Capabilities

- None.

## Impact

- Affects the bundled hook scripts under `src/exomem/_hooks/`, the hook installer/checker and CLI messaging, focused hook tests, and Claude Code/Codex setup documentation.
- Adds no server-side model or reasoning dependency. Checkpoint extraction is deterministic client-side measurement; semantic distillation remains the connected reasoning agent's responsibility.
- Adds no MCP/REST knowledge tool and does not change existing `UserPromptSubmit` or `Stop` behavior.
- Local checkpointing is soft-fail and bounded. Exomem synchronization is deferred and retryable rather than a prerequisite for compaction.
- Codex CLI is the required live end-to-end acceptance lane. Claude Code receives equivalent adapter, installer, envelope, and reinjection contract coverage; a live Claude run is optional rather than a release blocker.
