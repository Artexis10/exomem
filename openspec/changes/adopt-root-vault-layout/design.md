## Context

Exomem uses the physical vault path as the name that people and agents read, so every structural choice reaches every link, tool result and citation. Today every governed page lives under a wrapper folder, with up to seven folders below it, and filenames are kebab slugs of the title. Governed writes rewrite each resolvable link to the vault-rooted path (`normalize_body_wikilinks`). Obsidian shows link text as written, so a page reads `[[Knowledge Base/Notes/Insights/why-churn-rose-in-q3]]`. When `.obsidian` sits inside the wrapper, Exomem renders the path relative to the wrapper (`obsidian_uses_kb_root`): `[[Notes/Insights/why-churn-rose-in-q3]]`.

This change took over design D12 and task group 9 of `make-vault-store-canonical`. D12 holds in direction: short physical paths, title filenames and minimal link text. This design keeps D12's sound parts, including its flat layout below areas. It corrects four places where D12 was wrong: disclosure, undo, foreign content and crash safety outside the store. It also removes three places where D12 was heavier than needed: renames on retitle, the overlay rewrite and living inside the store change. The owner has decided the seven open choices (see "Owner choices").

**What D12 got right and this design keeps.**
- A relayout is a single store transaction.
- `store_pages.path` stays the store key, so no table changes key and the whole-table equivalence proof holds.
- Old paths keep resolving, through a durable record of each move and a single prior-path index (L8).
- In `root` vaults, links use the shortest trailing path that resolves to a single file, and `nested` vaults keep today's link form.
- Partition folders below an area flatten (O1).
- Derived names are unique across the vault, and a date prefix comes from a recorded date.
- Three naming corrections apply to every title-derived name, in `root` vaults and, for new pages (O2), in `nested` vaults.
- A requirement that says `Knowledge Base/<rest>` means `<managed root>/<rest>`. The canonical specs mention the wrapper 114 times in 29 files (plain text count, 2026-11-16), and none of them needs an edit.

**Closest existing tool.** Obsidian's own move with "update links" repoints only the wikilinks that Obsidian resolves. It runs no disclosure check, touches no pointer field (`evidence_file`, `parent_media`), applies no naming rule, and sees only files under its own root. In store mode the outside-edit importer would hold each edit it makes to an append-only page. It cannot do the relayout, so custom work is justified. Most parts of that work are existing code (see "Reuse, not rewrite").

**Measured facts.** The read-only plan reproduces every count below on any copy. The counts come from two sources, and the private copies are not in the repository.

From `relayout --plan` on a disposable real-vault copy, 2026-11-16. They decided O1 to O7:
- The plan's flattening alternative counts only title-named pages of the note types that declare a partition: 683 more of them move. They take no extra suffix, because derived names are already unique across the vault (O3). The count leaves out the domain level below `Sources/<Kind>/` and pages with fixed names in partition folders (navigation pages, pages without a title, sidecars and artifacts), so collisions between fixed names under full flattening are not measured yet (task 5.1b). No governance scope changes members, because the copy defines none, and the alternative does not evaluate `_access.yaml` tiers. Evidence keeps its scope and category folders, so an earlier pass's 70 to 82 colliding Evidence names no longer apply.
- The relayout renames 12,119 existing derived pages to their title names.
- Names unique across the vault need 1,452 ` (2)`-type suffixes. Names unique only per folder would need 1,379. These replace an earlier pass's 1,651 across the vault and 1,505 per folder.
- Three top-level entries outside the old wrapper clash with an area name, `index.md` or `log.md`: two empty folders and a stray view file.
- Of 76 links that Obsidian resolves and Exomem does not, 59 point into the `_Schema/` folder, 14 are bare names that differ only in letter case, and 3 point into a folder outside the wrapper.

From a copy of about 13.9k pages that an earlier design pass used (the store change's spike records its copy as 13.7k pages). These are not rechecked:
- 19,743 of 20,437 links are path links, and 1,997 of those include the wrapper.
- 287 titles contain `#`.
- 1,998 pages share a title with another page.
- 91% of Markdown files carry an `exomem_id`, with no duplicates.

Facts checked against today's code:
- The sites that built or compared the wrapper through `kb_dirname()` or `kb_prefix()` (512 lines in 131 files) now call the managed-root helpers in `src/exomem/kbdir.py`. The literal `Knowledge Base` still appears on 308 lines of 101 Python files under `src/exomem` (plain text count, 2026-11-16), in user-facing errors, docstrings, examples and the demo.
- A semgrep rule refuses a new `kb_dirname()` or `kb_prefix()` call outside that module.
- The store schema is at version 17. This change adds no table: the layout record and the relayout record are pages.
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
- The owner decided each choice that changes what the vault looks like, with counts from the read-only plan.

**Non-Goals**
- D12 never gates the store-mode switch. The relayout of an existing vault runs in store mode.
- No file-mode relayout applier. The relayout runs in store mode, after the store-mode switch (O6).
- No rename on retitle for now (O5). See "Deferred".
- No removal of duplicate content and no retitle of recap pages. Separate dedup work owns both. Pages that share a title keep their titles and get distinct filenames.
- No short links in existing `nested` vaults. A later homonym makes a bare link ambiguous, file mode cannot repair it, and the vault relayouts anyway.
- No rewrite of governance YAML, `_access.yaml` or note-type overlay `folder` values.

## Decisions

### L1. The layout is an explicit record

A vault has a layout:
- `nested`: the managed root is a folder inside a larger vault. This is today's layout. It stays for vaults that also hold content Exomem does not manage, and for every existing vault until its relayout.
- `root`: the managed root is the vault root. New vaults and new Cloud cells start here.

The managed root is the folder that holds `_Schema/`. A record at `_Schema/vault-layout.yaml` in the managed root holds `layout` and, in `root`, `unmanaged`. The values `nested` and `root` form a closed set that the record's schema fixes. `init` and the relayout write the record, and `--revert` writes a `nested` record in the wrapper `_Schema/` and removes the root record in the same store change. In store mode it is a stored page, so it commits with the relayout and projects like any page.

Inference from a folder is unsound. `_is_vault` accepts `Knowledge Base/_Schema/SKILL.md`, `.exomem/schema/SKILL.md` or a stored sentinel, and the shipped schema now lives at `.exomem/schema/`. A nested vault could be misread as `root`. Every path would then lose its wrapper, and path selectors would stop matching. Resolution reads the record in the vault root's `_Schema/` first and then the record in the wrapper's `_Schema/`. In store mode it reads the stored page when the file is absent, as `_is_vault` does for its sentinel. The first record found decides. Resolution has five cases:
1. A record that says `root` gives `root`.
2. A record that says `nested` gives `nested`.
3. No record and `_Schema/` only in the wrapper, or nowhere, gives `nested`. This covers every existing vault.
4. No record and `_Schema/` at the root only refuses, and the error names the missing record.
5. No record and `_Schema/` in both places refuses, and the error names both folders.

Each refusal names the remedy, and the record that fits depends on the refusal:
- `_Schema/` only at the vault root means the vault was laid out as `root`. Write a `root` record in that folder.
- `_Schema/` in both places with no record means an interrupted relayout or a stray folder. Write the record for the layout that the pages are in, which is `nested` when the pages sit under the wrapper. A wrong record hides every page.

The two delivered refusals differ. The refusal for a vault-root `_Schema/` with no record (`LAYOUT_RECORD_MISSING`) names the `root` record only. The refusal for `_Schema/` in both places with no record (`LAYOUT_AMBIGUOUS`) names both records and when each applies (`root` when the vault root is the managed root, `nested` when it is not). Each refusal already names the record that fits it.

While a record exists, a leftover second `_Schema/` is reported and never refused. `layout.inspect` reports it, the plan carries it in its diagnostics, and status and doctor report it (task 4.6a). Both folders exist during the relayout's projection window, and a refusal would leave a crashed relayout unresolvable.

`EXOMEM_KB_DIRNAME` stays as a test override. An upgrade moves no file and changes no layout. The first delivery calls the resolution from the read-only plan only. Status and doctor then report its result without refusing (task 4.6a). Vault resolution and service start call it from the root layout step (task 4.6b), so no existing code path changes behaviour before then. Before that wiring reaches service start, doctor reports an unrelated vault-root `_Schema/`, so that no host meets the refusal first at service start. The report reads `layout.inspect` and turns each `LayoutError` into a finding with its code and remedy, so the remedy text lives once, in the error. A review of the report found four defects, which task 4.6b lists with the fix for each.

### L2. Managed-root helpers

The managed-root helpers and their repository gate have landed. Code reaches the managed root through the `managed_*` helpers in `kbdir.py`. `kb_dirname()` and `kb_prefix()` remain only inside that module, and the semgrep rule in the repository's quality configuration refuses a new call elsewhere. In `nested` the helpers return today's strings exactly. The helpers do not rewrite the literal `Knowledge Base` text that remains in user-facing errors, docstrings, examples and the demo (308 lines in 101 Python files, plain text count, 2026-11-16). Task 4.9 makes that text layout-neutral.

What remains is the layout argument (task 4.6b). `managed_join` and `managed_split` take an optional layout. The process default suits the service and the cells. The planner, Cloud import and tests pass a layout explicitly. A requirement or tool text that says `Knowledge Base/<rest>` means `<managed root>/<rest>`, except that in `root` the path `Knowledge Base/Notes/<Area>/<rest>` means `<Area>/<rest>` for a compiled area, because the `Notes/` level drops.

### L3. A single layout translation for every path reader (disclosure)

The relayout moves paths, and path selectors decide disclosure. Governance scopes select by path globs and path refs. Only `paths` and `refs` can select a non-Markdown item, so Evidence binaries depend on them. `_access.yaml` tiers compare prefixes relative to the wrapper. Vocabulary instance bindings, topic carriers of `kind: folder`, homes and project-key folders bind to paths as well.

A selector that matches before the relayout and not after moves a page into the residual scope. An empty membership resolves to the widest disclosure, which releases the page. A concrete case: a scene frame inherits its video's scopes only when `parent_media` names the video and the frame sits in `<video>.frames/`. If the video and its frames move and `parent_media` keeps the wrapper path, the frame loses its video's narrower scopes. The same holds for `evidence_file`, `data_file`, `governance_companion.artifact_path` and `raw_protection.artifact_path`.

The decision:
1. `layout.translate_path(value, target, registry)` is the single function that reads path selectors. It reads note-type and home `folder` values, governance `paths`, `exclude_paths` and `refs`, `_access.yaml` tiers, instance bindings and folder topic carriers.
2. In `root`, `translate_path` drops a leading wrapper segment and reads `Notes/<Area>/...` as `<Area>/...` for a registered compiled area or home.
3. Governance YAML and `_access.yaml` are never rewritten. Selectors are read through the translation.
4. A path `ref` or a filename glob that names a page the relayout renames cannot be translated. `--plan` lists each of them, and the operator converts it to an id ref. Membership already matches `exomem_id`.
5. For every scope, tier and instance binding, the check compares the members before and after. `--apply` refuses on any difference, and `--verify` repeats the check. The check reuses the existing membership kernels. The delivered plan compares a scope by identity, a tier by each file's tier at its path, and an instance binding by whether its translated path value still holds the same number of files. Comparing a binding's members by identity is the work of `--apply` and `--verify` (tasks 5.2 and 5.4). The same holds for a file whose scopes come from a document container's binding or from a companion page: the plan counts it under `files_not_evaluated` with its reason, because it reads neither source, and `--apply` and `--verify` evaluate it.
6. Pointer fields get the move writer's transforms, and `parent_media` joins that set. The move writer repoints the other pointer fields today (`move_file._repoint_artifact` and its siblings, which include `governance_companion.artifact_path`) and does not repoint `parent_media` or the three path fields of a document container member's binding (`container_member.parent_path`, `parent_page` and `artifact_path`). The relayout repoints all four kinds, and the move writer's gap in the container fields stays old debt.

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

`root` makes `title` the default filename style. The setting from `add-readable-note-filenames` still overrides it. By the owner's answer (O2), new compiled notes and entities take title-derived names in `nested` vaults too. The naming-and-organisation work (`add-curation-relocation-protocol`) builds that writer switch on the rule below, and this change builds none of it for `nested`. This change renames existing derived pages only at the relayout.

The name rule lives in a single place, `vault.py`: `corrected_title_name` is `sanitize_title_filename` with the corrections below, and `fit_filename` applies the byte cap with the date prefix and the collision suffix. Both layouts use it, and the plan reads it through `layout.place` and `layout.allocate`. The three corrections apply to every title-derived name:
- Remove the characters of `LINK_FILENAME_REFUSED`: `_LINK_TITLE_REFUSED`, which sits next to the wikilink parser, plus `^`. They are `/`, `#`, `|`, `[`, `]`, the backtick, CR, LF and `^`. The wikilink grammar gives them meaning. Each becomes a space and the spaces collapse, as the sanitiser does for the characters it already refuses. That set is derived once, so the parser's grammar is read once.
- Strip leading dots. A dot-named file is outside the store (ownership rule 2), and Obsidian hides it.
- Cap the whole name at 200 UTF-8 bytes (`MAX_FILENAME_BYTES`), as well as at the character cap, cutting at a code-point boundary. The date prefix, the collision suffix and the extension count toward the cap, and the title part shrinks first. ext4 and APFS allow at most 255 bytes per name.

A name is derived when the page's note type names its files from the page's frontmatter `title`, as a title stem with or without a date prefix. Every other name is fixed: an Evidence original, a Records or Planning recipe name, a generated navigation page (`index.md`, `log.md`, `hub.md`), a page named after the file it describes, and any page whose type has no title rule or that sits outside every registry area. `title` means that frontmatter value only. An H1 or a filename stem never stands in for it, and a page with no frontmatter title keeps its name. The relayout needs only this derived or fixed class. There is no pin: owner choice O5 defers renames on retitle, so nothing records a name that a caller chose, and the relayout treats every title-named page as derived.

A date prefix comes from the page's recorded date and never from the date of a write. For a `started` stem it is the `started` value. For a `dated` stem it is the frontmatter `created` value, falling back to the page's first store audit row. D12's derived-name rule covered compiled stems only. Source names are `YYYY-MM-DD-<slug>` (date of capture), and entity names have no declared stem. The pack declares a `stem` for source and entity before the relayout re-derives them. For any dated stem, the relayout keeps the date prefix of the current filename when it parses, so the relayout never moves a date.

### L6. Uniqueness uses two name sets and the casefold key

Two sets of names answer two questions:
- **Name uniqueness** counts owned Markdown pages outside the machinery folders, so a trashed page never holds a name.
- **Link uniqueness** counts every Markdown file that Obsidian sees, machinery folders included.

Both compare under the NFC-casefold key (`held_fs.vault_name_key`). Exomem's stem lookup is case-sensitive (`resolver.stems.get(cleaned)`) and Obsidian's is not. The check "exactly a single file for both" must use the casefold key.

On a collision of a derived name in a `root` vault, the writer appends ` (2)`, ` (3)` and so on, taking the lowest number that no page holds. The suffix counts toward the 200-byte cap, so the writer shortens the title part first. In store mode the check reads the store, never the projection. A slug name, and a `title` name in a `nested` vault, keep the `-2` contract of `add-readable-note-filenames`. A derived name yields to fixed names whatever the path order. The relayout resolves collisions among derived names in ascending order of the pages' current paths, so a second plan run gives the same names. Uniqueness is vault-wide, by the owner's answer (O3).

### L7. Links, the Obsidian rule and the resolver order

`obsidian_links.link_form(target, index)` is a pure function in `src/exomem/obsidian_links.py`, which `vault.py` can import. It returns the shortest trailing path of the target, cut at a folder boundary, that is unique among the Markdown files Obsidian sees, under the casefold key. When no trailing path is unique, it returns the target's full vault path, which resolves by the exact-path step of the rule. It takes no layout. A writer in `root` calls it, and a writer in `nested` keeps today's form. The `index` maps the basename key of each file to the paths that hold it. The planner builds that index once from the pages it reads.

A writer needs only the homonym group of the target's basename: an indexed lookup in store mode and the resolver's stem buckets in file mode. It never scans all paths. That lookup belongs to group 4 (task 4.4). `normalize_body_wikilinks`, `render_wikilinks_for_vault` and the f-string emitters of `[[...]]` route through `link_form`.

The path is `[[Name]]` when no other file Obsidian sees has that name. A grouped page needs a real path suffix. When `Scan` is not unique, `Evidence/alpha/receipts/Scan.md` gets `[[receipts/Scan]]` if that is unique, then `[[alpha/receipts/Scan]]`. It never gets `[[Evidence/Scan]]`, which is a suffix of no page. Graph rows keep the resolved vault path, so derived rows keep their meaning. A link repoint changes only the link's target text, and the link still resolves to the same page.

`obsidian_links.IndexResolver` (and `resolve_link`) encode Obsidian's basename-or-path resolution rule. The rule resolves an exact vault path first. It then takes the strict union of the basename matches and the trailing-path matches, and resolves only when that union is a single file. A link text that two files satisfy is ambiguous, whichever file Obsidian would pick, so a writer never relies on Obsidian's choice. The writer, the planner and `--verify` all use it. The rule is closed, because Obsidian's behaviour fixes it, and the module docstring states the rule and its source.

**What Obsidian does, from documentation.** A check of Obsidian's API typings and of a forum answer (not labelled as staff) on 2026-11-16 found three things:
- An exact vault-root path wins over a same-named file in a subfolder. Only the forum answer says so.
- The API typings imply that a link text matches the end of a file's path.
- Which homonym a bare link opens, whether matching ignores case, and how it treats Unicode normalisation are undocumented.

The rule leans on none of the undocumented parts. It compares under the NFC-casefold key and treats a shared text as ambiguous. Only its exact-path-first step rests on the forum answer. No test in the app has confirmed any of it. The in-app check on a fixture vault is an operator-run step of task 5.7, because Obsidian is a desktop app that no automation here can drive.

The resolver's order becomes:
1. exact path;
2. the path with the wrapper prefixed;
3. the prior path (the prior-path index of `add-curation-relocation-protocol`, L8);
4. a retired collection path;
5. stem, then title, for bare names;
6. Obsidian's strict rule over the link index: the case-insensitive basename and the trailing path (new).

Step 6 runs last, so only links that resolve nowhere today change. A retired collection path keeps precedence over it. A link text that two files satisfy stays unresolved at step 6. The link index leaves out the machinery folders, so a link into `_Schema/` stays unresolved in Exomem, although Obsidian resolves it. Writers still count the files in those folders when they pick a short form (L6).

The relayout repoints the links and pointer fields of append-only `Sources/` and `Evidence/` pages through the governed pointer write that `add-curation-relocation-protocol` builds, in file mode and store mode. A pointer is a link target, a registry-declared pointer field or a page's own folder. The write rests on a ruling: Sources and Evidence content stays immutable, while their pointers become correctable through a governed, previewed, audited, reversible write. This change builds no write of its own and carries no delta of `command-surface`.

Step 6 reaches existing `nested` vaults too, as the owner accepted (O7). The plan on the 2026-11-16 copy found 76 links that Obsidian resolves and Exomem does not. Of these, 59 point into the `_Schema/` folder and stay unresolved on purpose, 14 are bare names that differ only in letter case, and 3 point into a folder outside the wrapper. The resolver work is therefore mainly case-insensitive basename matching under the NFC-casefold key (`held_fs.vault_name_key`), plus the trailing-path step (task 4.4). A `nested` writer keeps today's link form.

### L8. Identity and prior paths

`store_pages.path` stays the key. Every derived table is keyed by path and reused unchanged. A page-id key would still rekey the derived rows on every move and would break the equivalence proof. The move-safe identity already exists: `exomem_id` and the store's identities table, which every move updates.

Old paths keep resolving through the prior-path index that `add-curation-relocation-protocol` owns. That index is derived and sits beside the identities. It replaces `store_path_aliases`, and its resolver step sits in `vault._normalize_wikilink`. This change adds no store table. The index takes its rows from two sources:
- a moved page's `moved_from` frontmatter;
- the relayout record, which this change writes.

The relayout record is a vault-resident, durable record with a row for each moved file: its identity (or none, for a file without an `exomem_id`), its old path and its new path. It is `_Schema/relayout-record.yaml` under the managed root, in this format:

```yaml
schema_version: 1
relayouts:
  <relayout id>:
    reverted: false
    rows:
      - exomem_id: <id or null>
        old_path: <path before>
        new_path: <path after>
```

The prior-path index reads it. `reverted` is true for an entry that a revert undid, and such an entry claims no prior path. The revert writes its own entry, whose rows run from the root-layout path back to the nested path. In store mode the record is a stored page, as the layout record is. The index rebuilds from both sources. A page that carries `moved_from` from an earlier governed move keeps it, and its relayout row adds the newer prior path. Resolution, visibility, reserved paths and the index's squatter guard apply to a relayout row exactly as to a `moved_from` row, so a prior path to a page that the caller may not see answers as a missing page. The relayout writes no `moved_from` into pages, so page bytes change only in link spans and pointer fields.

The resolver never calls `kbdir.kb_relative_form`. The prior-path step sits in the resolver, and the managed-relative form that the hosted protected-tree guard and the executors share must agree with it. Three guard bypasses came from two rel-forms that disagreed, so task 5.8 tests that the resolver and the guard read a relayout row as the same current path. The relayout record sits in the vault, so a store-mode rollback keeps it with the other pages and this change adds no export of its own (task 5.9).

### L9. The relayout command

`exomem store-mode relayout` has four actions. The planner takes the target layout as a parameter, so `--plan` and `--revert` use the same code.

**`--plan [--full]`.** It runs on any vault in file or store mode and writes nothing, and it always prints JSON with the outcome sentence first. `--full` lists every entry instead of the first 20 of each listing. It is deterministic and sorted by current path. It reports:
- moves, renames, ` (2)` suffixes, truncations, link edits per page (append-only pages separately) and pointer-field edits;
- each unregistered `Notes/<X>` folder, the unmanaged top-level entries and their links into the managed tree, top-level name collisions and pages it cannot map;
- every scope, tier or binding whose member set would change, with counts and sample ids, and path refs and globs that need id refs;
- where `.obsidian` sits;
- the counts that fed the owner choices (the report's `alternatives`), such as "pages flattening would move: N; scopes flattening would change: K".

**Placement.** The path map is a prefix rewrite, a flattening of partition folders and renames of derived basenames. It drops the wrapper, drops `Notes/` before a registered compiled area or home, and moves each page out of a partition folder below the group level into its group folder (O1), or into the area when the area has no group folder. It gives each derived basename the form of the resolved style (O2), unique across the vault (O3). A page in an exempt or unregistered folder keeps its path relative to the managed root, so only the wrapper segment drops. A `Notes/<X>` folder whose `<X>` names no registered area is unregistered, keeps its `Notes/` level, and `--plan` lists it. Fixed names keep their names. A page named after the file it describes keeps its name, so the pair stays bound: the page `<file name>.md` of any file, and the page `<stem>-notes.md` of an unprotected Markdown original. A Markdown original that carries its own type and title keeps its name together with its `.md.md` page. Two fixed names that would take a single path leave a page the plan cannot map.

**`--apply`.** It runs with the service stopped. It refuses and names itself on each failed precondition:
- store mode is active, no item is held, the projection queue is empty, a store backup exists and free disk space is at least the size of the store;
- no page is unmappable and the disclosure diff is zero;
- every path ref to a renamed page is converted to an id ref;
- the top-level namespace is free: no unmanaged top-level entry collides with an area, `index.md` or `log.md`. The owner renames or removes a clashing entry, and the apply then proceeds.

The managed upgrade migrator never runs a relayout.

The store transaction cannot cover the disk-canonical files under the wrapper: `_Governance/`, `_access.yaml` and the collection replica that carries the store-mode decision. A start between the commit and the renames of those files would read a missing policy as an empty policy, which opens everything. A pre-layout build on another host would find no replica at the old path and start in file mode. The apply therefore runs this sequence:
1. Write the `relayout_pending` marker.
2. Commit the single store change: page moves, repointed pages, the relayout record and the layout record.
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

**`--revert`.** The relayout record holds each page's old path, and the relayout's audit holds each link span's old text. The revert plans back to `nested`:
- every page that existed at the relayout returns to its recorded path;
- every recorded link span that is still byte-identical gets its old text back;
- a page created after the relayout takes nested placement by rule and today's link form;
- a `nested` layout record in the wrapper `_Schema/` replaces the root record in the same store change;
- a relayout record of the revert's own moves is written in that change, so root-layout paths keep resolving.

It runs offline under the same marker sequence. Run it before a store-mode rollback when `nested` is wanted back.

**Outside content stays unmanaged.** D12 made vault-root Markdown outside the old wrapper owned. That would give Exomem write authority over content it never managed: store custody, held outside edits and governed writes. `_access.yaml` read-only trees and excluded sibling trees sit outside the wrapper today. D12 also did not handle name collisions at the root, such as a foreign `Sources/`, `index.md` or `log.md` beside an area of the same name. By the owner's answer (O4), the layout record lists, as `unmanaged`, the top-level entries that sat outside the old wrapper at relayout time. They keep today's meaning: indexed for search where they are today, never written. `--plan` lists them and their links into the managed tree, which the relayout cannot repoint. A top-level entry whose name clashes with an area, `index.md` or `log.md` refuses `--apply` until the owner renames or removes it. Ownership treats the vault root as the managed root, minus dot names, `_Imports` and the unmanaged list.

**Cost of a move.** In file mode, a move renames a file, rewrites each linking page with its own guard and fan-out, and refuses when an append-only page links the target. A file-mode relayout of 14k pages is not practical. In store mode the relayout is a single transaction that re-derives about 8k pages. That is close to a cold build: 82 s for the lexical index plus 50 s for the graph on the spike's copy (unverified for this change). Projection follows. The queue's move intent (operation `move` with the old path, store schema 16) lets the projector rename a page file instead of deleting and rewriting it, which needs the optional addition of store task 4.1. Without it, a move projects as a delete plus a write, which takes roughly 80 minutes. With it, the relayout writes about 8k rewritten files at the spike's 7.5 files per second, roughly 18 minutes (unverified).

**Rollback keeps the layout.** A store-mode rollback keeps `root`, and file mode serves it, because both modes share the layout code. The rollback also keeps the relayout record in the vault, because the record is a page. The rollback's precondition of an empty projection queue means no leftover wrapper `_Schema/` exists when it runs. To get `nested` back, run `--revert` first.

**Guarantees to the tool surface.**
- Each owned page keeps its `exomem://memory/<uuid>` reference across moves, retitles and the relayout.
- No path in a `root` vault contains the wrapper.
- A derived name identifies a single page in a `root` vault. Where names collide (fixed names, outside-edit imports), the shortest trailing path identifies the page. A title does not identify a page, so a tool that shows titles must disambiguate them.
- An old path still resolves through the prior-path index, for a caller who may see the page.

**How an existing vault moves.** The store-mode switch is a separate decision, and the relayout runs after it, in its own maintenance window (O6). After the relayout ships:
1. Make sure every host runs a build that knows the layout record, and stop the other hosts' services.
2. Pause file sync to other devices.
3. Back up the store.
4. Run `exomem store-mode relayout --plan`. Fix each refusal it names: convert path refs to id refs, and rename or remove a clashing top-level entry.
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

**Load target.** The product's load target is recorded in the design of `make-vault-store-canonical`: 20,000 pages, about 100,000 units and 120,000 edges, with a hub of 10,000 links. In `root` each write adds a name-group lookup for uniqueness and a name-group lookup for each link it writes. No corpus walk is added to any route. The relayout runs offline. The plan over a generated 20,000-page vault finishes within twice a cold lexical build on the same host, with bounded memory. The drill `tests/test_relayout_drill.py` measured this on a single host, in the delivering run: 20,000 pages and 80,000 links, plan 22.2 s against a 303 s cold lexical build, and a tracemalloc peak of 101 MB against 204 MB of page bodies. It runs only when `EXOMEM_RELAYOUT_DRILL_PAGES` is set.

### L11. Delivery order

Evidence comes first. Each step names what it waits on. The sizes are judgments, not measurements.

| Step | Outcome | Waits on | Size |
|---|---|---|---|
| Managed-root helpers | No code outside `kbdir.py` calls `kb_dirname()` or `kb_prefix()`. Done. | nothing | done |
| Read-only plan and layout model | Layout record resolution (called by the plan only), `translate_path`, `link_form`, the Obsidian rule and `store-mode relayout --plan`. No behaviour change. | nothing | done (this change's PR A) |
| Owner choices | O1 to O7 decided from the plan's counts on a disposable real-vault copy, 2026-11-16. | the plan | done |
| Root layout for new vaults | `exomem init` creates a `root` vault, and file mode and store mode serve it. | the plan's layout model and link rule; `add-note-type-registry` task 2.4 for overlay-defined areas | 8 to 12 days |
| Relayout apply, verify and revert | A store-mode `nested` vault moves to `root` and can move back. | the plan, the root layout and the decided owner choices; the prior-path index and the governed pointer write of `add-curation-relocation-protocol`; the offline projector entry and per-effect writer authority that #2805 and #2818 delivered | 8 to 12 days |
| Cloud | Cloud import maps `nested` trees to `root` through the planner, and export emits the cell's own layout. | the relayout and Cloud task 6.2 of `make-vault-store-canonical` | 4 to 6 days |

The read-only plan ships no behaviour change. Only the plan calls the layout resolution, `translate_path`, `link_form` and the naming corrections, and the plan computes the trailing-path resolution. Group 4 wires the layout resolution into vault resolution and service start, and wires link form and naming corrections into `root` vaults. Group 4 also adds the resolver's new last step to both layouts (O7). New title-derived names in `nested` vaults follow the naming-and-organisation work. No link or name that exists today changes before then, apart from links that resolve nowhere today.

Cuts considered. Task order after the writer PRs would delay the owner's evidence to the end and would land the largest refactor while store PRs were open. Root for new vaults first would decide O1 to O4 without counts from a real vault. The chosen order front-loads the disclosure finding and the owner's decisions, proves `root` serving before any vault moves, and does not wait on the store change.

## Owner choices (decided 2026-11-16)

The owner decided all seven choices from `relayout --plan` on a disposable real-vault copy. Each entry gives the answer first, then the counts, the option that the owner did not take and the requirements that follow. Examples use an invented insight "Why churn rose in Q3", a research note "Churn drivers" in project "Alpha", and a source article "Pricing basics" of domain "Economics". "Today" means a nested vault whose Obsidian opens the outer vault.

**O1. Partition folders below an area: flatten.** The tree is `Research/Churn drivers.md` and `Sources/Articles/2026-05-02-Pricing basics.md`. The group folders stay: `Sources/<Kind>/`, `Entities/<Type>/`, `Records/<collection>/`, `Planning/<collection>/` and `Evidence/<scope>/<category>/`. Every other role, compiled included, has no group folder, and the typed writer adds no project, domain or medium folder. A domain-only reclassification moves no file.
- Plan on the copy: 683 more title-named pages of partitioned note types move, with no extra suffix. The domain level below `Sources/<Kind>/` and fixed-name pages in partition folders are not counted yet (task 5.1b).
- A vault whose path scope names a partition folder fails the disclosure diff. The plan then refuses until the operator converts that scope to a `projects:` selector.
- Not taken: keep the partitions (`Research/Alpha/Churn drivers.md`).
- Follows: the requirements "Pages sit flat inside areas" and "Relayout places every page by rule", and the `adoption-studio` delta (flat `Sources/<Kind>/`).

**O2. Filenames: title names, and existing derived pages rename at the relayout.** The page is `Insights/Why churn rose in Q3.md`, linked `[[Why churn rose in Q3]]`. Obsidian's tree, tabs, search and graph show readable names.
- Plan on the copy: 12,119 existing derived pages rename to their title names.
- New pages take title names, in `nested` vaults too, with the three naming corrections. A writer switch that the naming-and-organisation work (`add-curation-relocation-protocol`) builds delivers it, on the single rule in `vault.py` (`corrected_title_name`, `fit_filename`). This change builds none of that switch.
- Cost: bookmarks, outside notes and scripts that use old names break. The prior-path index covers only Exomem calls.
- Not taken: slug names (`Insights/why-churn-rose-in-q3.md`).
- Follows: the requirements "Derived names are unique across the vault" and "Relayout places every page by rule".

**O3. Name uniqueness: vault-wide.** A second "Budget" becomes `Patterns/Budget (2).md`, with links `[[Budget]]` and `[[Budget (2)]]`. A person who types `[[Budget]]` in Obsidian gets a single page.
- Plan on the copy: 1,452 suffixes vault-wide against 1,379 per folder, so vault-wide costs 73 more.
- Not taken: unique per folder (`Insights/Budget.md` and `Patterns/Budget.md`, linked by path). A typed `[[Budget]]` would be ambiguous: Obsidian picks a page and Exomem leaves it unresolved.
- Follows: the requirements "Derived names are unique across the vault" and "Links use the shortest trailing path that resolves to a single file".

**O4. Content outside today's wrapper: stays unmanaged.** Exomem indexes it for search where it does today and never writes it. The plan lists each unmanaged top-level entry with its links into the managed tree. Those links break in Obsidian after the relayout, because the relayout cannot repoint them. A top-level name that clashes with an area, `index.md` or `log.md` refuses `--apply` until the owner renames or removes the clashing entry.
- Plan on the copy: 3 clashing entries, two of them empty folders and the third a stray view file.
- Not taken: adopt the content. Exomem would store, govern and repoint it, and hold a person's concurrent edits for review, at the cost of wider authority and a bigger store.
- Follows: the requirement "Relayout places every page by rule" and decision L9.

**O5. Filenames follow retitles: not now.** After a retitle the file keeps its name, as in every vault today. The relayout needs only the derived or fixed class and treats every title-named page as derived.
- The requirements "A name that a caller chooses is pinned" and "A governed write in a root vault renames a derived name to match the title" leave the spec delta, with the pin clauses of two other requirements. See "Deferred".
- Not taken: build it now. A title edit would rename the file and repoint its inbound links in the same write, at a cost that has no bound on hubs.
- Follows: the requirements "A page name is derived or fixed" and "Derived names are unique across the vault", which carry no pin clause.

**O6. Timing: after the store-mode switch, in its own maintenance window.** A single change at a time means a regression points at a single cause, and a store-mode rollback stays simple.
- Not taken: the same window as the switch, which gives a single outage that is harder to attribute. Not taken either: a file-mode offline relayout before the switch, which needs a second applier with its own crash journal and a file-mode exception for link-only repoints in append-only pages, about a week more.
- Follows: no requirement text and no task group.

**O7. Links resolve the way Obsidian does, in both layouts: accepted.** Exomem resolves a partial-path link such as `[[receipts/Scan]]` and a bare name that differs only in letter case, as Obsidian does. Only links that resolve nowhere today change. A `nested` writer keeps today's link form.
- Plan on the copy: 76 links resolve for Obsidian and not for Exomem. Of these, 59 point into the `_Schema/` folder, which Exomem leaves out of its link index on purpose, so they stay unresolved. 14 are bare names that differ only in letter case. 3 point into a folder outside the wrapper.
- The resolver work is therefore mainly case-insensitive basename matching (NFC-casefold, as `held_fs.vault_name_key`) plus the trailing-path step (task 4.4).
- The three naming corrections (the characters of `LINK_FILENAME_REFUSED`, leading dots and the 200-byte cap) apply to new title-derived names in `nested` vaults, as under O2.
- Not taken: gate the trailing-path step to `root`.
- Follows: the requirement "Links use the shortest trailing path that resolves to a single file".

**What would reopen a decision.**
- If the rehearsal shows the single transaction exceeding the disk or time budget at 20,000 pages, split `--apply` into phases under the marker.
- If Obsidian does not resolve trailing paths or homonyms as assumed, fall back to paths relative to the vault root inside grouped areas.
- If the store-mode switch slips by months and the owner wants the layout sooner, build the file-mode applier (O6).

## Deferred

Renames that follow retitles (O5) are not built. The spec delta holds no pin requirement and no rename-on-write requirement, and tasks.md lists the former task group 7 as a plain list. A later change that builds them needs three things:
- A pin record, as a store column or an audit entry, for a name that a caller chose at create, move or rename time. An editor rename that the outside-edit import reads as a governed move counts as a chosen name. A pinned name is never renamed, and a `root` vault refuses a chosen name that another page holds.
- A rename rule. In a `root` vault in store mode with the `title` style, a governed write renames a derived name that does not match its page's frontmatter title, in the same transaction, repoints every inbound link and keeps the old path as a prior path. A name matches when it equals the naming rule's output under the `title` or the `slug` style. The date prefix comes from the page's recorded date, never from the write date. An outside-edit import, a `nested` vault and a change of filename style rename nothing.
- A bound on inbound repoints for hubs. At the load target's hub of 10,000 links, a single title edit would rewrite 10,000 pages on an interactive write, which is close to a cold build. The bound can defer the repoints to a background governed job above a set count of linking pages.

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
- **Append-only repoint.** The relayout cannot repoint links in append-only pages without the governed pointer write that `add-curation-relocation-protocol` builds. The apply waits on that change (task 5.6). This change builds no such write.
- **Prior-path index.** The relayout record is useful only when the prior-path index of `add-curation-relocation-protocol` reads it. The apply waits on that index, and task 5.1 fixes the record's name and format, which that index reads.
- **Scale.** Each added per-write cost is a bounded lookup. The relayout is the only operation that rewrites many pages, and it runs offline. Rename on retitle, which would rewrite many pages on a write, is deferred (O5).

**Not verified.**
- Obsidian's behaviour for partial paths, homonyms, case-insensitive matching and Unicode normalisation comes from its API typings and a forum answer, not from a test in the app (L7). Task 5.7 holds the operator-run check.
- Every count from the earlier copy that Context lists as not rechecked. The counts of 2026-11-16 are the plan's output on a private copy, so the repository holds no copy to repeat them on.
- Where a given vault's `.obsidian` sits.
- The relayout's transaction time, projection time and re-embedding time at the load target.
- The sizes in the delivery table.
