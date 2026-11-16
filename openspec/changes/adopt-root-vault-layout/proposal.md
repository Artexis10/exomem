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
- **Readable names.** In `root`, the default filename style is `title`. Three naming corrections (wikilink characters, leading dots, a 200-byte cap) apply to every title-derived name. The owner's answer for new pages extends title-derived names to new compiled notes and entities in `nested` vaults, and the naming-and-organisation work builds that switch.
- **Append-only repoint.** The relayout repoints the links and pointer fields of append-only `Sources/` and `Evidence/` pages through the governed pointer write that `add-curation-relocation-protocol` builds. This change builds no write of its own.
- **Short links.** In `root`, writers emit the shortest trailing path that resolves to a single file for both Exomem and Obsidian. `nested` vaults keep today's link form. The resolver gains a trailing-path step.
- **Old paths.** The relayout writes a durable record of each move in the vault. The prior-path index of `add-curation-relocation-protocol` reads that record and keeps old paths resolving. The managed-relative form that the hosted protected-tree guard uses agrees with the resolver's prior-path step.
- **Verified relayout.** `exomem store-mode relayout` plans, applies, verifies and reverts the move of a store-mode vault from `nested` to `root`. The plan proves that no governance scope, access tier or binding changes its members. A crash marker makes an interrupted apply resumable.
- **Owner choices.** Seven choices (O1 to O7) decide what the vault looks like. The read-only plan supplies the counts before anything is built for good. Requirements that depend on a choice are rewritten from the answer. O2 and O7 have an answer for new pages already, and the rest of each stays open.
- **Not in scope.** D12 never gates the store-mode switch. The relayout of an existing vault runs in store mode, after the switch. File mode gains no relayout applier unless choice O6 asks for it.

## Capabilities

### New Capabilities

- `vault-layout`: the managed root and its layout record, areas named by registries, readable names and their uniqueness, short links, old paths, and the verified, reversible relayout.

### Modified Capabilities

- `adoption-studio`: in a `root` vault, a classified item's Source directory is `Sources/<Kind>/`. This delta depends on owner choice O1 and is dropped if partitions stay.

## Impact

- **Code.** A new layout module and a read-only relayout planner, then the typed writer, the compiled-type write gate, ownership, `init`, link emission and the resolver, then the apply, verify and revert commands. The managed-root helpers and their repository gate have landed.
- **Related changes.** `make-vault-store-canonical` drops D12 and group 9, which this change builds. `add-curation-relocation-protocol` owns the prior-path index, which replaces `store_path_aliases`, and the governed pointer write. This change writes the relayout record that the index reads, and its apply uses the pointer write. `add-readable-note-filenames` and `add-note-type-registry` are reopened for the naming corrections and the area `folder` rule. `make-source-reclassification-reversible` and `add-governed-vault-consolidation` are reconciled with the governed pointer write for append-only pages.
- **Users.** Obsidian users see areas at the vault root, title filenames and short links after the relayout. Open tabs, bookmarks and plugin queries that named old paths need editing. An existing vault keeps its layout until its relayout.
- **Operations.** The relayout runs offline, with the service stopped, after a store backup, in a maintenance window. Every host runs a build that knows the layout record before the relayout.
