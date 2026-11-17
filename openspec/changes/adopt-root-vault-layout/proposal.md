## Why

Exomem uses the physical vault path as the name that people and agents read. Every structural choice therefore reaches every link, tool result and citation: the wrapper folder, the `Notes/` level, partition folders and slug filenames. A person meets the vault in three places:

- **Link text in Obsidian.** Governed writes rewrite every resolvable link to the canonical vault-rooted path. Obsidian shows link text as written, so a page reads `[[Knowledge Base/Notes/Insights/why-churn-rose-in-q3]]`. When `.obsidian` sits inside the wrapper, the path is relative to the wrapper instead: `[[Notes/Insights/why-churn-rose-in-q3]]`.
- **Paths in agent replies and tool results.** API paths are vault-rooted, and agents repeat them to the person.
- **The file tree.** A wrapper folder (when Obsidian opens the outer vault), then `Notes/`, then the type folder, then partition folders, then a slug.

Two partial fixes exist and do not reach Obsidian. `render_title_links` shows links by title to readers, and `kb_relative_form` accepts paths without the wrapper as input. Obsidian reads the real tree and the real link text, so files must move to fix what the person sees there. Obsidian's own move with "update links" cannot do the job. It repoints only the wikilinks that Obsidian resolves, runs no disclosure check, touches no pointer field, applies no naming rule, and would make the store-mode importer hold every edit it makes to an append-only page.

`make-vault-store-canonical` carried this work as design D12 and task group 9. D12 never gated the store-mode switch, but inside that change it blocked the change's archive. This change takes D12 over, corrects it, and lets the store change archive on its own.

## What Changes

- **Two layouts and an explicit record.** A vault is `nested` (the managed root is a folder inside the vault, today's layout) or `root` (the managed root is the vault root). A layout record at `_Schema/vault-layout.yaml` states the layout. Resolution never infers `root` from where a folder sits.
- **Areas from registries.** Each area folder name comes from the note-type registry or a collection profile. `Notes/` exists only in `nested`. No area name enters code.
- **Readable names.** In `root`, the default filename style is `title`, and names are unique across the whole vault. Three naming corrections (wikilink characters, leading dots, a 200-byte cap) apply to every title-derived name. The relayout renames existing derived pages to their title names. The owner's answer for new pages extends title-derived names to new compiled notes and entities in `nested` vaults, and the naming-and-organisation work builds that switch. A retitle renames no file for now.
- **Flat areas.** The relayout flattens the partition folders below areas. The group folders stay: `Sources/<Kind>/`, `Entities/<Type>/`, collections by collection, and `Evidence/<scope>/<category>/`.
- **Outside content.** Content outside the old wrapper stays unmanaged: indexed for search where it is today and never written. A top-level name that clashes with an area, `index.md` or `log.md` refuses the apply until the owner renames or removes the entry.
- **Append-only repoint.** The relayout repoints the links and pointer fields of append-only `Sources/` and `Evidence/` pages through the governed pointer write that `add-curation-relocation-protocol` builds. This change builds no write of its own.
- **Short links.** In `root`, writers emit the shortest trailing path that resolves to a single file for both Exomem and Obsidian. `nested` vaults keep today's link form. In both layouts the resolver gains Obsidian's rule as a last step: case-insensitive basename and trailing-path matching, with machinery folders kept out of the link index.
- **Old paths.** The relayout writes a durable record of each move in the vault. The prior-path index of `add-curation-relocation-protocol` reads that record and keeps old paths resolving. The managed-relative form that the hosted protected-tree guard uses agrees with the resolver's prior-path step.
- **Verified relayout.** `exomem store-mode relayout` plans, applies, verifies and reverts the move of a store-mode vault from `nested` to `root`. The plan proves that no governance scope, access tier or binding changes its members. A crash marker makes an interrupted apply resumable.
- **Owner choices, decided.** The owner decided seven choices (O1 to O7) from the read-only plan's counts on a disposable real-vault copy on 2026-11-16. They are flat areas, title names for existing derived pages, vault-wide unique names, unmanaged outside content, no rename on retitle for now, a relayout in its own window after the store-mode switch, and Obsidian-equivalent link resolution in both layouts. The requirements follow the answers, and the design records them.
- **Not in scope.** D12 never gates the store-mode switch. The relayout of an existing vault runs in store mode, after the switch, in its own maintenance window. File mode gains no relayout applier. Renames that follow retitles are deferred, and the design's "Deferred" section says what a later change needs.

## Capabilities

### New Capabilities

- `vault-layout`: the managed root and its layout record, areas named by registries, readable names and their uniqueness, short links, old paths, and the verified, reversible relayout.

### Modified Capabilities

- `adoption-studio`: in a `root` vault, a classified item's Source directory is `Sources/<Kind>/`. This follows owner choice O1, which flattens the partitions.
- `vault-file-naming`: in a `root` vault, a title name is unique across the vault with a ` (2)` suffix (O3). The retitle rule is stated once, and the relayout is the single exception to "No Existing File Is Renamed" (O2, O5).

## Impact

- **Code.** A new layout module and a read-only relayout planner, then the typed writer, the compiled-type write gate, ownership, `init`, link emission and the resolver, then the apply, verify and revert commands. The managed-root helpers and their repository gate have landed.
- **Related changes.** `make-vault-store-canonical` drops D12 and group 9, which this change builds. `add-curation-relocation-protocol` owns the prior-path index, which replaces `store_path_aliases`, and the governed pointer write. This change writes the relayout record (`_Schema/relayout-record.yaml`) that the index reads, and its apply uses the pointer write. Its naming-and-organisation work built the writer switch for title names on new pages, with the naming corrections, into the canonical `vault-file-naming` spec. `add-note-type-registry` is reopened for the area `folder` rule. `make-source-reclassification-reversible` and `add-governed-vault-consolidation` are reconciled with the governed pointer write for append-only pages.
- **Users.** Obsidian users see areas at the vault root, title filenames and short links after the relayout. Open tabs, bookmarks and plugin queries that named old paths need editing, and so do links from outside content into the managed tree, which the relayout cannot repoint. An existing vault keeps its layout until its relayout.
- **Operations.** The relayout runs offline, with the service stopped, after a store backup, in a maintenance window. Every host runs a build that knows the layout record before the relayout.
