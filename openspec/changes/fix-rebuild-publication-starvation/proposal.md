## Why

A managed Exomem cell can remain unavailable indefinitely when ordinary writer or
watcher traffic changes the disposable live lexical SQLite set while a detached
full rebuild is running. The completed rebuild then declines publication, later
refused reads start another full pass, and concurrent work can repeat the cycle
without any source or policy inconsistency.

## What Changes

- Make the detached background worker the only owner of a managed full-catalog
  rebuild while retrieval is unavailable.
- Reconcile a completed detached catalog against the latest authoritative
  watcher checkpoints under the short publication barrier instead of treating
  harmless live SQLite byte churn as a permanent veto.
- Continue to fail closed when the source projection, access policy, semantic
  identity, or retained delta cannot be proven current.
- Expose bounded repair phase, duration, and abort-reason telemetry so an
  operator can distinguish active progress, contention, and a declined publish.
- Add concurrency regressions proving ordinary writes cannot create a competing
  in-place full rebuild or starve eventual publication.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `live-index-freshness`: Managed lexical recovery must converge under ordinary
  writer and watcher traffic while retaining exact source-of-truth freshness.
- `instant-start`: Managed startup and late repair must retain one full-rebuild
  owner and publish readiness after a current detached catalog lands.

## Impact

The change is confined to lexical catalog repair/publication, managed warmup
coordination, privacy-safe repair telemetry, and their tests. It changes no MCP,
REST, CLI, vault-content, or on-disk schema contract; the lexical sidecar remains
disposable and rebuildable from Markdown.
