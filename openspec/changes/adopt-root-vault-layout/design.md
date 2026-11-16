## Context

Exomem uses the physical vault path as the name that people and agents read, so every structural choice reaches every link, tool result and citation. Today every governed page lives under a wrapper folder, with up to seven folders below it, and filenames are kebab slugs of the title. Governed writes rewrite each resolvable link to the vault-rooted path (`normalize_body_wikilinks`). Obsidian shows link text as written, so a page reads `[[Knowledge Base/Notes/Insights/why-churn-rose-in-q3]]`. When `.obsidian` sits inside the wrapper, Exomem renders the path relative to the wrapper (`obsidian_uses_kb_root`): `[[Notes/Insights/why-churn-rose-in-q3]]`.

This change took over design D12 and task group 9 of `make-vault-store-canonical`. D12 holds in direction: short physical paths, title filenames and minimal link text. This design keeps D12's sound parts. It corrects four places where D12 was wrong: disclosure, undo, foreign content and crash safety outside the store. It also removes four places where D12 was heavier than needed: renames on retitle, flattening partitions, the overlay rewrite and living inside the store change.

**What D12 got right and this design keeps.**
- A relayout is a single store transaction.
- `store_pages.path` stays the store key, so no table changes key and the whole-table equivalence proof holds.
- A path alias table keeps old paths resolving.
- Links use the shortest trailing path that resolves to a single file.
- Derived names are unique across the vault, and a date prefix comes from a recorded date.
- Three naming corrections apply to every layout.
- A requirement that says `Knowledge Base/<rest>` means `<managed root>/<rest>`. The canonical specs mention the wrapper 114 times in 29 files (plain text count, 2026-11-16), and none of them needs an edit.

**Closest existing tool.** Obsidian's own move with "update links" repoints only the wikilinks that Obsidian resolves. It runs no disclosure check, touches no pointer field (`evidence_file`, `parent_media`), applies no naming rule, and sees only files under its own root. In store mode the outside-edit importer would hold each edit it makes to an append-only page. It cannot do the relayout, so custom work is justified. Most parts of that work are existing code (see "Reuse, not rewrite").

**Measured facts.** The counts below come from a real-vault copy of about 13.9k pages that an earlier design pass used (the store change's spike records its copy as 13.7k pages). They were taken from that earlier copy and not rechecked, because they need the private copy. The read-only plan reproduces them on any copy.
- 19,743 of 20,437 links are path links, and 1,997 of those include the wrapper.
- 287 titles contain `#`.
- 1,505 pages need a suffix within their current folder, and 1,651 need a suffix across the whole vault.
- 1,998 pages share a title with another page.
- 91% of Markdown files carry an `exomem_id`, with no duplicates.
- 70 to 82 of 3,114 Evidence names collide if partitions flatten.

Facts checked against today's code:
- The helper sites that built or compared the wrapper as a string (512 lines in 131 files) now route through the managed-root helpers in `src/exomem/kbdir.py`.
- A semgrep rule refuses a new `kb_dirname()` or `kb_prefix()` call outside that module.
- The store schema is at version 17 and has no alias or layout table.
- The typed writer reads the shipped registry only.
- `_APPEND_ONLY = ("Sources", "Evidence")` sits in code (`access.py`).
- Governance scopes select by path globs and refs, and only `paths` and `refs` can select a non-Markdown item.

## Goals / Non-Goals

**Goals**
- A person sees short physical paths, title filenames and minimal link text in Obsidian, in tool results and in agent citations.
- No governance scope, access tier or instance binding gains or loses a member through the relayout, and every pointer field follows its target.
- Every relayout can be undone.
- A crash during the relayout leaves a vault that refuses to start and resumes or reverts, never a vault that reads a missing policy as empty.
- New vaults start in `root`. No path in a `root` vault holds the wrapper.
- The owner decides each choice that changes what the vault looks like, with counts from the read-only plan.

**Non-Goals**
- D12 never gates the store-mode switch. The relayout of an existing vault runs in store mode.
- No file-mode relayout applier, unless owner choice O6 asks for it.
- No removal of duplicate content and no retitle of recap pages. Separate dedup work owns both. Pages that share a title keep their titles and get distinct filenames.
- No short links in existing `nested` vaults. A later homonym makes a bare link ambiguous, file mode cannot repair it, and the vault relayouts anyway.
- No rewrite of governance YAML, `_access.yaml` or note-type overlay `folder` values.

## Decisions

### L1. The layout is an explicit record

A vault has a layout:
- `nested`: the managed root is a folder inside a larger vault. This is today's layout. It stays for vaults that also hold content Exomem does not manage, and for every existing vault until its relayout.
- `root`: the managed root is the vault root. New vaults and new Cloud cells start here.

The managed root is the folder that holds `_Schema/`. A record at `_Schema/vault-layout.yaml` in the managed root of a `root` vault holds `layout` and `unmanaged`. The values `nested` and `root` form a closed set that the record's schema fixes. `init` and the relayout write the record. In store mode it is a stored page, so it commits with the relayout and projects like any page.

Inference from a folder is unsound. `_is_vault` accepts `Knowledge Base/_Schema/SKILL.md`, `.exomem/schema/SKILL.md` or a stored sentinel, and the shipped schema now lives at `.exomem/schema/`. A nested vault could be misread as `root`. Every path would then lose its wrapper, and path selectors would stop matching. Resolution has four cases:
1. A record that says `root` gives `root`.
2. No record and only the wrapper `_Schema/` gives `nested`. This covers every existing vault.
3. No record and `_Schema/` at the root only refuses, and the error names the missing record.
4. No record and `_Schema/` in both places refuses, and the error names both folders.

While a record exists, a leftover second `_Schema/` is reported by status and doctor and never refused. Both folders exist during the relayout's projection window, and a refusal would leave a crashed relayout unresolvable.

`EXOMEM_KB_DIRNAME` stays as a test override. An upgrade moves no file and changes no layout.

### L2. Managed-root helpers

The managed-root helpers and their repository gate have landed. All code reaches the managed root through the `managed_*` helpers in `kbdir.py`. `kb_dirname()` and `kb_prefix()` remain only inside that module, and the semgrep rule in the repository's quality configuration refuses a new call elsewhere. In `nested` the helpers return today's strings exactly.

What remains is the layout argument. `managed_join` and `managed_split` take an optional layout. The process default suits the service and the cells. The planner, Cloud import and tests pass a layout explicitly. A requirement or tool text that says `Knowledge Base/<rest>` means `<managed root>/<rest>`, except that in `root` the path `Knowledge Base/Notes/<Area>/<rest>` means `<Area>/<rest>` for a compiled area, because the `Notes/` level drops.

### L3. A single layout translation for every path reader (disclosure)

The relayout moves paths, and path selectors decide disclosure. Governance scopes select by path globs and path refs. Only `paths` and `refs` can select a non-Markdown item, so Evidence binaries depend on them. `_access.yaml` tiers compare prefixes relative to the wrapper. Vocabulary instance bindings, topic carriers of `kind: folder`, homes and project-key folders bind to paths as well.

A selector that matches before the relayout and not after moves a page into the residual scope. An empty membership resolves to the widest disclosure, which releases the page. A concrete case: a scene frame inherits its video's scopes only when `parent_media` names the video and the frame sits in `<video>.frames/`. If the video and its frames move and `parent_media` keeps the wrapper path, the frame loses its video's narrower scopes. The same holds for `evidence_file`, `data_file`, `governance_companion.artifact_path` and `raw_protection.artifact_path`.

The decision:
1. `layout.translate_path(pattern, layout)` is the single function that reads path selectors. It reads note-type and home `folder` values, governance `paths`, `exclude_paths` and `refs`, `_access.yaml` tiers, instance bindings and folder topic carriers.
2. In `root`, `translate_path` drops a leading wrapper segment and reads `Notes/<Area>/...` as `<Area>/...` for a registered compiled area or home.
3. Governance YAML and `_access.yaml` are never rewritten. Selectors are read through the translation.
4. A path `ref` or a filename glob that names a page the relayout renames cannot be translated. `--plan` lists each of them, and the operator converts it to an id ref. Membership already matches `exomem_id`.
5. For every scope, tier and instance binding, `--plan` computes the member set by identity before and after. `--apply` refuses on any difference, and `--verify` repeats the check. The check reuses the existing membership kernels.
6. Pointer fields get the move writer's transforms, and `parent_media` joins that set. The move writer repoints the other pointer fields today (`move_file._repoint_artifact` and its siblings) and does not repoint `parent_media`.

### L4. Areas come from registries, and the folder value is read, never rewritten

Registries name every area, and code never spells an area name:
- A compiled type's `folder` is its area name. The `Notes/` parent exists only in `nested`.
- A `folder` value that starts with `Notes/` reads as the `nested` parent plus the area name. The same read-time rule covers homes. An existing overlay stays valid.
- The relayout does not rewrite overlay `folder` values. A rewrite would add a byte change to verify and to undo, and the note-type registry spec refuses a change of `folder` on an existing entry.
- Note types with role `source`, `evidence` and `entity` get a `folder` attribute, which changes no entry's meaning.
- `Records` and `Planning` come from the collection profiles.
- The machinery folders (`_Schema`, `_trash`, `_archive`, `_attachments`, `_compiled`) keep their names. They are a closed product set.
- `init` derives its folders from the registries, which also fixes the missing `Notes/Procedures` folder. The append-only areas derive from roles `source` and `evidence`.

The typed writer reads the shipped registry only. An overlay-defined compiled area therefore needs `add-note-type-registry` task 2.4 (folders, partitions and stems onto the registry). Coordinate with that change's owner, or take that part in the root layout PR.

### L5. Readable names and the naming corrections

`root` makes `title` the default filename style. The setting from `add-readable-note-filenames` still overrides it, and `nested` keeps `slug` as its default. The name rule is `sanitize_title_filename` with three corrections, which apply in both layouts:
- Remove `#`, `^`, `[` and `]`, because the wikilink grammar gives them meaning. Reuse `_LINK_TITLE_REFUSED`, which sits next to the parser, and add `^`. Add no second set.
- Strip leading dots. A dot-named file is outside the store (ownership rule 2), and Obsidian hides it.
- Cap the whole name at 200 UTF-8 bytes as well as at the character cap, cutting at a code-point boundary. ext4 and APFS allow at most 255 bytes per name.

A name is derived when the page's note type names its files from the page's frontmatter `title`, as a title stem with or without a date prefix. Every other name is fixed: an Evidence original, a Records or Planning recipe name, a generated navigation page (`index.md`, `log.md`, `hub.md`), and any page whose type has no title rule or that sits outside every registry area. `title` means that frontmatter value only. An H1 or a filename stem never stands in for it, and a page with no frontmatter title keeps its name. The relayout needs only this derived or fixed class.

A date prefix comes from the page's recorded date and never from the date of a write. For a `started` stem it is the `started` value. For a `dated` stem it is the frontmatter `created` value, falling back to the page's first store audit row. D12's derived-name rule covered compiled stems only. Source names are `YYYY-MM-DD-<slug>` (date of capture), and entity names have no declared stem. The pack declares a `stem` for source and entity before the relayout re-derives them. For any dated stem, the relayout keeps the date prefix of the current filename when it parses, so the relayout never moves a date.

### L6. Uniqueness uses two name sets and the casefold key

Two sets of names answer two questions:
- **Name uniqueness** counts owned Markdown pages outside the machinery folders, so a trashed page never holds a name.
- **Link uniqueness** counts every Markdown file that Obsidian sees, machinery folders included.

Both compare under the NFC-casefold key (`held_fs.vault_name_key`). Exomem's stem lookup is case-sensitive (`resolver.stems.get(cleaned)`) and Obsidian's is not. The check "exactly a single file for both" must use the casefold key.

On a collision of a derived name in a `root` vault, the writer appends ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix counts toward the 200-byte cap, so the writer shortens the title part first. In store mode the check reads the store, never the projection. A slug name, and a `title` name in a `nested` vault, keep the `-2` contract of `add-readable-note-filenames`. A derived name yields to fixed names whatever the path order. The relayout resolves collisions among derived names in ascending order of the pages' current paths, so a second plan run gives the same names. Whether uniqueness is vault-wide or per folder is owner choice O3.

### L7. Links, the Obsidian rule and the resolver order

`link_form(target, name_group, layout)` sits beside the resolver. In `root` it returns the shortest trailing path of the target, cut at a folder boundary, that is unique among the Markdown files Obsidian sees, under the casefold key. In `nested` it returns today's form. It needs only the homonym group of the target's basename: an indexed lookup in store mode and the resolver's stem buckets in file mode. It never scans all paths. `normalize_body_wikilinks`, `render_wikilinks_for_vault` and the f-string emitters of `[[...]]` route through it.

The path is `[[Name]]` when no other file Obsidian sees has that name. A grouped page needs a real path suffix. When `Scan` is not unique, `Evidence/alpha/receipts/Scan.md` gets `[[receipts/Scan]]` if that is unique, then `[[alpha/receipts/Scan]]`. It never gets `[[Evidence/Scan]]`, which is a suffix of no page. Graph rows keep the resolved vault path, so derived rows keep their meaning. A link repoint changes only the link's target text, and the link still resolves to the same page.

A single function encodes Obsidian's basename-or-path resolution rule. The writer, the planner and `--verify` all use it. The rule is closed, because Obsidian's behaviour fixes it, and the function carries a single-line reason in the code saying so.

The resolver's order becomes:
1. exact path;
2. the path with the wrapper prefixed;
3. the path alias (store mode);
4. a retired collection path;
5. the trailing path (new);
6. stem, then title, for bare names.

Only links that resolve nowhere today change, because the trailing-path step is the last path step. A retired collection path keeps precedence over it.

In store mode, append-only pages in `Sources/` and `Evidence/` accept link repoints and nothing else, each with its own audit reason (the `command-surface` delta of this change). File mode keeps today's `APPEND_ONLY` refusal.

Whether the trailing-path step and the naming corrections reach existing `nested` vaults is owner choice O7. The read-only plan computes both and wires nothing.

### L8. Identity and path aliases

`store_pages.path` stays the key. Every derived table is keyed by path and reused unchanged. A page-id key would still rekey the derived rows on every move and would break the equivalence proof. The move-safe identity already exists: `exomem_id` and the store's identities table, which every move updates.

Store schema 18 adds `store_path_aliases(old_path PRIMARY KEY, page identity, current_path, relayout_id, created_revision)`. Every store move updates it in the same transaction.

The alias step sits inside the shared managed-relative form, `kbdir.kb_relative_form`, after exact paths. The hosted protected-tree guard and the executors share that form. Three guard bypasses came from two rel-forms that disagreed, so a second alias lookup in the resolver would repeat the fault. An alias to a page that the caller may not see answers exactly as a missing page. The store change's rollback exports the table when the store has such a table.

### L9. The relayout command

`exomem store-mode relayout` has four actions. The planner takes the target layout as a parameter, so `--plan` and `--revert` use the same code.

**`--plan [--json]`.** It runs on any vault in file or store mode and writes nothing. It is deterministic and sorted by current path. It reports:
- moves, renames, ` (2)` suffixes, truncations, link edits per page (append-only pages separately) and pointer-field edits;
- each unregistered `Notes/<X>` folder, foreign top-level entries and their links into the managed tree, top-level name collisions and pages it cannot map;
- every scope, tier or binding whose member set would change, with counts and sample ids, and path refs and globs that need id refs;
- where `.obsidian` sits;
- the counts that each owner choice depends on, such as "pages flattening would move: N; scopes flattening would change: K".

**Placement.** Under the recommended choice O1 (partitions stay), the path map is a prefix rewrite plus renames of derived basenames. It drops the wrapper, drops `Notes/` before a registered compiled area or home, and gives each derived basename the form of the resolved style. A page in an exempt or unregistered folder keeps its path relative to the managed root, so only the wrapper segment drops. A `Notes/<X>` folder whose `<X>` names no registered area is unregistered, keeps its `Notes/` level, and `--plan` lists it. Pinned and fixed names keep their names. Two pinned names that would take a single path leave a page the plan cannot map.

**`--apply`.** It runs with the service stopped. It refuses and names itself on each failed precondition:
- store mode is active, no item is held, the projection queue is empty, a store backup exists and free disk space is at least the size of the store;
- no page is unmappable and the disclosure diff is zero;
- every path ref to a renamed page is converted to an id ref;
- the top-level namespace is free: no foreign top-level entry collides with an area, `index.md` or `log.md`.

The managed upgrade migrator never runs a relayout.

The store transaction cannot cover the disk-canonical files under the wrapper: `_Governance/`, `_access.yaml` and the collection replica that carries the store-mode decision. A start between the commit and the renames of those files would read a missing policy as an empty policy, which opens everything. A pre-layout build on another host would find no replica at the old path and start in file mode. The apply therefore runs this sequence:
1. Write the `relayout_pending` marker.
2. Commit the single store change: page moves, repointed pages, aliases and the layout record.
3. Run the offline projector drain that the store-mode rollback uses.
4. Move the disk-canonical files.
5. Clear the marker.

While the marker stands, the service start and every CLI read refuse with a code that names `--apply` (resume) and `--revert`. Governance root resolution refuses when the root-layout `_Governance/` is missing and the wrapper copy exists. A copy of the replica stays at the old path, so a pre-layout build refuses with `VAULT_STORE_ELSEWHERE`. Doctor reports that copy and removes it on request.

**`--verify`.** It proves:
- every owned page maps to exactly a single new path, and the set of `exomem_id` values is unchanged;
- the member set of every scope, tier and binding is equal by identity;
- every link occurrence resolves to the same page before and after, and every rewritten link passes the Obsidian rule;
- page bytes equal the old bytes with the planned link and pointer-field edits applied, and binaries keep their hashes;
- a fresh derivation equals the stored rows, and graph edges are equal once paths are mapped to ids.

The rehearsal adds two checks that `--verify` does not run. It compares the recall canary case by case with 95% intervals. Exact equality is not the claim, because link text and filename-derived titles change search tokens. It also runs the revert and the rollback drills on the relaid-out vault.

**`--revert`.** The relayout records each page's old path (an alias row) and each link span's old text (the relayout's audit). The revert plans back to `nested`:
- every page that existed at the relayout returns to its recorded path;
- every recorded link span that is still byte-identical gets its old text back;
- a page created after the relayout takes nested placement by rule and today's link form.

It runs offline under the same marker sequence. Run it before a store-mode rollback when `nested` is wanted back.

**Foreign content stays unmanaged by default.** D12 made vault-root Markdown outside the old wrapper owned. That gives Exomem write authority over content it never managed: store custody, held outside edits and governed writes. `_access.yaml` read-only trees and excluded sibling trees sit outside the wrapper today. D12 also did not handle name collisions at the root, such as a foreign `Sources/`, `index.md` or `log.md` beside an area of the same name. The layout record lists, as `unmanaged`, the top-level entries that sat outside the old wrapper at relayout time. They keep today's meaning: indexed for search, never written. `--plan` lists them and their links into the managed tree, which the relayout cannot repoint. Ownership treats the vault root as the managed root, minus dot names, `_Imports` and the unmanaged list. Owner choice O4 can adopt these entries instead.

**Cost of a move.** In file mode, a move renames a file, rewrites each linking page with its own guard and fan-out, and refuses when an append-only page links the target. A file-mode relayout of 14k pages is not practical. In store mode the relayout is a single transaction that re-derives about 8k pages. That is close to a cold build: 82 s for the lexical index plus 50 s for the graph on the spike's copy (unverified for this change). Projection follows. The queue's move intent (operation `move` with the old path, store schema 16) lets the projector rename a page file instead of deleting and rewriting it, which needs the optional addition of store task 4.1. Without it, a move projects as a delete plus a write, which takes roughly 80 minutes. With it, the relayout writes about 8k rewritten files at the spike's 7.5 files per second, roughly 18 minutes (unverified).

**Rollback keeps the layout.** A store-mode rollback keeps `root`, and file mode serves it, because both modes share the layout code. The rollback's precondition of an empty projection queue means no leftover wrapper `_Schema/` exists when it runs. To get `nested` back, run `--revert` first.

**Guarantees to the tool surface.**
- Each owned page keeps its `exomem://memory/<uuid>` reference across moves, retitles and the relayout.
- No path in a `root` vault contains the wrapper.
- A derived name identifies a single page in a `root` vault. Where names collide (fixed names, outside-edit imports), the shortest trailing path identifies the page. A title does not identify a page, so a tool that shows titles must disambiguate them.
- An old path still resolves through the alias table, for a caller who may see the page.

**How an existing vault moves.** The store-mode switch is a separate decision. After the relayout ships:
1. Make sure every host runs a build that knows the layout record, and stop the other hosts' services.
2. Pause file sync to other devices.
3. Back up the store.
4. Run `exomem store-mode relayout --plan`. Fix each refusal it names: convert path refs to id refs, and rename a colliding top-level folder.
5. Stop the service.
6. Run `exomem store-mode relayout --apply`. Expected result: a summary of moves, renames and link edits, then "projection complete".
7. Run `exomem store-mode relayout --verify`. Expected result: every check passes.
8. If the plan said `.obsidian` sits inside the wrapper, move `.obsidian` to the vault root.
9. Start the service, open the vault root in Obsidian and resume sync.

To undo, stop the service and run `exomem store-mode relayout --revert`. If nothing was written since the relayout, restoring the store backup also works.

### L10. Registry mechanisms and reuse

**Mechanisms that this change adds or keeps, each with a single-line reason in the code.**
- The layout values `nested` and `root`: a closed set that the record's schema fixes.
- Obsidian's forbidden characters, `_LINK_TITLE_REFUSED` plus `^`: a closed set that the wikilink grammar fixes.
- The Obsidian resolution rule: a closed rule that Obsidian's behaviour fixes.
- The machinery folders: a closed product set. Today two sets must agree (`VAULT_SCAN_SKIP_DIRS` and `find_corpus.EXCLUDED_DIR_NAMES`), and they should be a single list. This is reported as old debt.
- The move writer's pointer fields: a closed set of page-format fields.

No area or folder name enters code. Areas come from the note-type `folder` attribute and the collection profiles. Old instances that this change does not touch (`_APPEND_ONLY`, `init._FOLDERS`, hard-coded `Records` constants) are replaced as the root layout PR reaches them.

**Reuse, not rewrite.** `semantic_writes.rewrite_wikilinks_for_move`, `move_file._repoint_artifact`, `_repoint_back_references` and `_repoint_parts`, `unique_path`, `sanitize_title_filename`, `WikilinkResolver`, the governance membership kernels, `vault_store.verify`, the multi-move machinery of `vault_store.directory`, and the offline projector entry of the store-mode rollback. The applier builds a single store change on that machinery. Derived rows take a path-map `UPDATE`, and only pages whose bytes change are re-derived. `--verify` proves that a fresh derivation equals the stored rows.

**Load target.** The product's load target is recorded in the design of `make-vault-store-canonical`: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links. In `root` each write adds a name-group lookup for uniqueness and a name-group lookup for each link it writes. No corpus walk is added to any route. The relayout runs offline. The plan over the 20,000-page drill generator finishes within twice a cold lexical build on the same host, with bounded memory.

### L11. Delivery order

Evidence comes first. Each step names what it waits on. The sizes are judgments, not measurements.

| Step | Outcome | Waits on | Size |
|---|---|---|---|
| Managed-root helpers | No code outside `kbdir.py` builds or compares the wrapper. Done. | nothing | done |
| Read-only plan and layout model | Layout record resolution, `translate_path`, `link_form`, the Obsidian rule and `store-mode relayout --plan`. No behaviour change. | nothing | 4 to 6 days |
| Owner choices | O1 to O7 decided from the plan's counts on a disposable real-vault copy. | the plan | the owner's time |
| Root layout for new vaults | `exomem init` creates a `root` vault, and file mode and store mode serve it. | the plan's layout model and link rule; `add-note-type-registry` task 2.4 for overlay-defined areas | 8 to 12 days |
| Relayout apply, verify and revert | A store-mode `nested` vault moves to `root` and can move back. | the plan, the root layout and the owner choices; the offline projector entry and per-effect writer authority that #2805 and #2818 delivered | 8 to 12 days |
| Cloud | Cloud import maps `nested` trees to `root` through the planner, and export emits the cell's own layout. | the relayout and Cloud task 6.2 of `make-vault-store-canonical` | 4 to 6 days |
| Filenames follow retitles | Only if O5 says build. | the relayout | 5 to 8 days |

The read-only plan ships no behaviour change. The trailing-path resolution and the naming corrections are computed by the read-only plan. They are wired into the resolver and the writers only after owner choice O7, so the plan cannot change any link or name that exists today.

Cuts considered. Task order after the writer PRs would delay the owner's evidence to the end and would land the largest refactor while store PRs were open. Root for new vaults first would decide O1 to O4 without counts from a real vault. The chosen order front-loads the disclosure finding and the owner's decisions, proves `root` serving before any vault moves, and does not wait on the store change.

## Owner choices (open)

Each choice changes what a person sees. Examples use an invented insight "Why churn rose in Q3", a research note "Churn drivers" in project "Alpha", and a source article "Pricing basics" of domain "Economics". "Today" means a nested vault whose Obsidian opens the outer vault.

`relayout --plan` supplies the counts that each choice depends on, on a disposable real-vault copy and before anything is built for good. The requirements marked below as depending on a choice stay as written until the answer arrives. Then the answer rewrites them, and a requirement that no longer applies is removed with its scenarios.

**O1. Partition folders below an area.** This is a choice of how the relayout places pages.
- *Keep (recommended for the first relayout).* `Research/Alpha/Churn drivers.md` and `Sources/Articles/Economics/2026-05-02-Pricing basics.md`. The tree goes area, then project, domain or medium, then the note. Links read `[[Churn drivers]]`. Cost: nothing beyond the relayout. Path scopes on project folders keep working. Flattening stays possible later as another pass of the same planner.
- *Flatten (D12).* `Research/Churn drivers.md` and `Sources/Articles/2026-05-02-Pricing basics.md`. The tree is shallower, and a domain-only reclassification moves no file. Cost: more collisions inside a folder. Every path scope on a partition folder must become a `projects:` selector before the relayout, and the plan refuses until then. Homes and project-key folders change meaning, and three specs change (adoption-studio, reclassification and vault-layout). The typed writer applies no partition, no project, domain or medium folder and no second taxonomy level. The group folders that stay follow a closed role value or a collection profile: `Sources/<Kind>/`, `Entities/<Type>/`, `Records/<collection>/`, `Planning/<collection>/` and `Evidence/<scope>/<category>/`. Every other role, compiled included, has no group folder.
- Depends on O1: requirements "Pages sit flat inside areas", "Relayout places every page by rule" (the flattening clause) and the `adoption-studio` delta.

**O2. Filenames.**
- *Titles (D12, recommended).* `Insights/Why churn rose in Q3.md`, linked `[[Why churn rose in Q3]]`. Obsidian's tree, tabs, search and graph show readable names. Cost: nearly every page is renamed at the relayout. Homonym titles get ` (2)`. Bookmarks, outside notes and scripts that use old names break, and the alias table covers only Exomem calls.
- *Slugs.* `Insights/why-churn-rose-in-q3.md`, linked `[[why-churn-rose-in-q3]]`. Only folders change.
- Depends on O2: "Derived names are unique across the vault" and "Relayout places every page by rule".

**O3. Where names must be unique (`root` with titles).**
- *Vault-wide (D12, recommended).* A second "Budget" becomes `Patterns/Budget (2).md`. Links are `[[Budget]]` and `[[Budget (2)]]`. A person who types `[[Budget]]` in Obsidian gets a single page.
- *Per folder.* `Insights/Budget.md` and `Patterns/Budget.md` stay unsuffixed. Links become `[[Insights/Budget]]` and `[[Patterns/Budget]]`. A typed `[[Budget]]` is ambiguous: Obsidian picks a page and Exomem leaves it unresolved.
- Depends on O3: "Derived names are unique across the vault" and "Links use the shortest trailing path that resolves to a single file".

**O4. Content outside today's wrapper.**
- *Stays unmanaged (recommended).* Exomem indexes it and never writes it, as today. Links from it into the managed tree break in Obsidian after the relayout, and the plan lists each of them.
- *Adopted (D12).* Exomem stores, governs and repoints it, and holds a person's concurrent edits to it for review. Cost: wider authority and a bigger store. Excluded tiers must still stay out of the store.
- Depends on O4: "Relayout places every page by rule". Its current text states the adopt option. The recommended default rewrites it to the unmanaged rule of decision L9.

**O5. Filenames that follow retitles.**
- *Not now (recommended).* After a retitle the file keeps its name, as in every vault today. The relayout needs only the derived or fixed class, and pins matter only once renames on write exist. Today an explicit name is always a kebab slug.
- *Build it.* A title edit renames the file and repoints its inbound links in the same write, in a `root` vault in store mode with the `title` style. A pinned or fixed name never changes, an outside-edit import never renames, and a date prefix never comes from the write date. Cost: write amplification without a bound on hubs. At the load target's hub of 10,000 links, a single title edit would rewrite 10,000 pages, which is close to a cold build, on an interactive write. It also costs the pin record, the suffix-versus-refusal rule for a chosen name and the style-flip rule. If built, it needs a bound, such as deferring the repoints to a background governed job above a set count of linking pages.
- Depends on O5: "A name that a caller chooses is pinned" and "A governed write in a root vault renames a derived name to match the title". If the answer is "not now", these two requirements and tasks group 7 move to a later proposal or are deleted.

**O6. Timing.**
- *After the store-mode switch, in a separate window (recommended).* A single change at a time, so a regression points at a single cause, and a store-mode rollback stays simple.
- *In the same window as the switch (D12 allows it).* A single outage, but harder to attribute.
- *Before the switch, with a file-mode offline relayout.* A second applier with its own crash journal and a file-mode exception for link-only repoints in append-only pages. About a week more. It pays only if the switch slips far.
- Affects no requirement text. It adds a task group if the third option wins.

**O7. A small change for existing nested vaults in file mode.** Exomem would resolve partial-path links such as `[[receipts/Scan]]` the way Obsidian does. Today those links do not resolve and get no graph edge. The naming corrections (`#`, `^`, `[`, `]`, leading dots and the 200-byte cap) change new names in vaults that set the `title` style.
- *Accept (recommended).* Only links that do not resolve today change, and names stop breaking Obsidian links.
- *Gate to `root`.* A layout branch more.
- Depends on O7: the wiring of trailing-path resolution and the naming corrections, and "Links use the shortest trailing path that resolves to a single file".

**What would change a recommendation.**
- Few partition-scoped selectors and few extra collisions in the plan's counts make flattening cheap. Then take D12's flat layout (O1).
- A vault whose Obsidian opens the outer vault and holds much foreign content may suit `nested`. The relayout's value drops, and title names for new pages may be enough.
- If the switch slips by months and the owner wants the layout sooner, build the file-mode applier (O6).
- If the rehearsal shows the single transaction exceeding the disk or time budget at 20,000 pages, split `--apply` into phases under the marker.
- If Obsidian does not resolve trailing paths or homonyms as assumed, fall back to paths relative to the vault root inside grouped areas.

## Risks / Trade-offs

- **Disclosure.** The central risk is that a relayout moves a page out of a narrower scope. The disclosure diff refuses it in the plan, the apply and the verify. A path ref or glob that names a renamed page needs the operator's conversion to an id ref before the apply.
- **Crash outside the store.** The store transaction does not cover `_Governance/`, `_access.yaml` or the replica. The marker sequence, the refusal while it stands and the governance root refusal contain it.
- **Data move on a real vault.** The relayout moves every page of a real vault in a single pass. Backup, plan, verify, revert and a rehearsal on a disposable copy contain it, and the rehearsal repeats on native Windows.
- **Test corpus.** A plain text count on 2026-11-16 finds 11,023 `Knowledge Base` occurrences on 10,892 lines in 906 of 1,497 test files, and 27 `EXOMEM_KB_DIRNAME` overrides. These stay on `nested`, so `root` needs its own test lane at the boundaries: ownership, resolver, recall scopes, write gate, governance, migrate and relayout.
- **Process-wide managed root.** The managed root assumes a single vault per process, which matches the service and the cells.
- **Collection store.** The `Records` and `Planning` views belong to the collection store, which must re-root its projected views during the relayout. Coordinate with that store's owner.
- **Later homonyms.** A later fixed name or outside-edit import can add a second file with a name that an existing bare `[[Name]]` already uses, and the link becomes ambiguous. This is accepted debt, and this change adds no repoint rule for it.
- **Obsidian homonyms.** A bare `[[Name]]` may resolve in Obsidian to a homonym in `_trash` or `_archive`. Writers count every Obsidian-visible file when they choose the short form.
- **Old builds.** An older build pointed at a `root` vault could create a fresh wrapper folder. The vault-side marker of the store change's rollback design refuses a build that carries the check. A build from before it ignores the marker, so each host must upgrade before the relayout. The replica copy at the old path covers builds that predate the layout record.
- **Append-only repoint.** The store-mode repoint of links in append-only pages is specified (`command-surface` delta) and built (task 5.6) in this change.
- **Scale.** Each added per-write cost is a bounded lookup. The relayout and the retitle option are the only operations that rewrite many pages. The first is offline and the second is gated by owner choice O5 and a bound.

**Not verified.**
- Obsidian's behaviour for partial paths, case-insensitive matching and extensionless links comes from its documented behaviour, not from a test. The read-only plan's fixture vault should confirm it a single time.
- Every count from the real-vault copy, as listed in Context.
- Where a given vault's `.obsidian` sits.
- The relayout's transaction time, projection time and re-embedding time at the load target.
- The sizes in the delivery table.
