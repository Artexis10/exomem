## ADDED Requirements

### Requirement: Graph rebuild failures retain actionable evidence
The graph coordinator SHALL log the original exception and checkpoint identity when a registered builder stops before coverage. The public mutation terminal SHALL remain content-free and SHALL report a stable failure code plus remediation selected from the current epoch classification. A generic failure projection SHALL NOT erase the exception chain used by service diagnostics.

#### Scenario: Recoverable builder failure recommends ordinary recovery
- **WHEN** a graph builder fails and the post-failure epoch remains current or recovery-required
- **THEN** the terminal recommends retrying the same mutation identity or ordinary reconcile and the service log retains the original exception

#### Scenario: Ambiguous builder failure recommends explicit reset
- **WHEN** a graph builder fails and the post-failure epoch is unavailable
- **THEN** the terminal preserves the committed canonical outcome and names `maintain_memory(mode="reconcile", dry_run=false, rebuild_graph=true)` as the derived-state repair

### Requirement: Ambiguous graph lineage resets only by explicit derived-state quarantine
When and only when a caller explicitly requests `rebuild_graph=true` for an unavailable epoch, Exomem SHALL isolate the exact live graph SQLite set and graph floor/checkpoint artifacts through same-filesystem reversible renames under the existing mutation boundary, then rebuild graph state from canonical Markdown using the existing full-publication protocol. It SHALL NOT move or rewrite canonical Markdown, canonical-outcome receipts, activity logs, or non-graph sidecars. Unsafe path type, reparse point, identity change, partial move, or open-reader replacement refusal SHALL fail closed.

#### Scenario: Dry run previews without quarantine
- **WHEN** a caller sets `dry_run=true` and `rebuild_graph=true` for unavailable lineage
- **THEN** the report states that a derived reset is applicable without moving any file or registering rebuild work

#### Scenario: Explicit reset rebuilds only derived graph state
- **WHEN** unavailable lineage is safely quarantined and a full rebuild succeeds
- **THEN** graph status becomes current for the canonical corpus and every canonical Markdown hash remains unchanged

#### Scenario: Partial quarantine rolls back
- **WHEN** one derived artifact cannot be moved before the complete live set is isolated
- **THEN** Exomem restores prior moves in reverse order and does not begin a clean rebuild

#### Scenario: Rebuild failure never mixes lineages
- **WHEN** the old live set is completely quarantined but the clean rebuild fails
- **THEN** old artifacts remain isolated, no old companion file is attached to a new main database, and the response exposes a bounded quarantine identifier for operator recovery
