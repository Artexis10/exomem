# Design

## Decision: a read-through store, not write-maintained candidates

The store is read through and checked against file identity on each sweep. Writes do not maintain it. The reasons:

- The category's contract adds no write-time work.
- Exact per-audience results need the cross-page aggregation after egress filtering. A hidden page can join two visible pages' origins through shared Sources. Registry anchors and resolver titles come from visible pages only. A projection that drops hidden rows after aggregation cannot reproduce that.
- Edits made outside Exomem count on the next sweep without a reconcile.
- The measured cost is reading files. On the reference host a stat-only walk of the Knowledge Base takes under a second. A full read takes tens of seconds when the page cache is cold.

## Evidence split

- **Page-local evidence** is stored per page. It covers the title, wikilinks, Source and origin fallbacks, the raw status, the link keys needed for the connection check, relation clauses, and the cue-dependent frames (body-field, typed-copula, typed-label).
- **Anchor-dependent frames** (subject-relation, identity-relation) are recomputed on each sweep from the stored relation clauses. Frames are line-local, and every anchor frame needs a relation token in its clause, so this is exact.
- **Cross-page work** runs on each sweep over visible pages: eligibility, the registry index, page existence, origin collapse, the gates, and the finding.

## Validity

- The store's metadata carries the schema version, the package version, the grammar version, the predicate-table digest, and the vault root. A mismatch rebuilds the store.
- A row's cue frames carry the entity-type fingerprint and cue digest.
- File identity is the stat signature plus device and inode, with a rule that re-hashes recently changed files. On native Windows, creation time stands in for change time, so every row is verified by content hash.
- Any store fault runs the full sweep for that call. It never returns a partial answer.

## Privacy

Rows hold excerpts and clauses. Pages that are not indexable, or that are raw-protected, store no content, matching the embedding index. Governance verdicts are never cached; egress is asked on every sweep.
