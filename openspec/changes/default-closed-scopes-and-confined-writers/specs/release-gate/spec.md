## ADDED Requirements

### Requirement: Restricted writers are confined to their writable scopes and inbox

A restricted writer is a resolved principal that is not the owner under a non-empty policy, or the owner reached through a connector whose denied set is not empty. A restricted writer's writable scopes SHALL come only from the `writable_scope_ids` of the client entry that its verified client binding matches; a caller that matches no entry SHALL have none. The host SHALL refuse a connector configuration in which a client's writable scopes are not policy scopes, meet its own denied set, belong to a client with an empty denied set, or are not denied to the default context and to every other client with a non-empty denied set. The host SHALL also refuse a configuration in which a declared residual scope is missing from the default denied set.

For every write (create, edit, replace, append, observe, move source and destination, delete, reclassify and registry overlay publication), a restricted writer SHALL be admitted only in one of two cases. In the first case, the target lies in a configured capture path; the membership that its path alone gives is non-empty, lies within the writer's writable scopes and is readable by it in full; and the proposed bytes keep exactly that membership. In the second case, the target exists, the writer reads it in full, and its scope set before and after the write is non-empty and lies within the writer's writable scopes. A move destination and a reclassified path SHALL be judged as creations. The decision SHALL precede any existence check or generated-name allocation, and every refusal SHALL be `WRITE_REFUSED: target is unavailable`. Every writer, the owner included, SHALL keep each capture page's membership equal to its path's membership. A restricted writer's Source capture SHALL land in its kind's folder under the first capture path, in sorted order, where the writer may create; with no such path, the capture SHALL be refused. An explicit destination SHALL never be redirected.

A write door's side effects on a page outside a restricted writer's writable scopes and inbox SHALL be skipped: neither written nor refused. These side effects are activity log and index entries, a cited Source's `ingested_into` back-reference, and link rewrites in linking pages. A skipped side effect on a page withheld from the writer SHALL leave every response detail as it is with no such page. Workflow state that a door keeps under the governance tree, namely curation runs and episode journals, SHALL be written for a restricted writer, because no audience is served that tree; the policy documents and the activation manifest SHALL stay refused. A vocabulary change that the vocabulary authority has granted to the caller SHALL be admitted to the registry storage that the grant names. Without that grant, the caller SHALL receive the vocabulary authority's own refusal. No other write SHALL gain authority from a vocabulary grant.

#### Scenario: A fenced connector cites a shared Source
- **WHEN** a fenced connector creates a note in its inbox whose `sources:` names a Source that it may read but not write
- **THEN** the note is written, the Source's bytes are unchanged, and the response warns that the back-reference was skipped

#### Scenario: Activity pages outside the inbox
- **WHEN** a fenced connector creates a page in its inbox, and the activity log and the top index lie outside its writable scopes
- **THEN** the page is written and the log and the top index are unchanged

#### Scenario: Workflow state of a restricted writer
- **WHEN** a fenced connector prepares, records and resumes an episode
- **THEN** each step succeeds, its journal is kept under the governance tree, and no policy document changes

#### Scenario: A vocabulary change by a non-owner session
- **WHEN** a non-owner session saves an entity type, once before the owner grants that change and once after
- **THEN** the first call receives the vocabulary authority's refusal with a request id and the registry is unchanged, and the second call changes the registry

#### Scenario: Write-down into a shared page
- **WHEN** a fenced connector edits a page it may read that carries a scope outside its writable scopes
- **THEN** it receives `WRITE_REFUSED` and the page is unchanged

#### Scenario: Declassification by move or metadata
- **WHEN** a restricted writer moves its matter page out of the matter, or edits the page's metadata so that the page leaves a writable scope
- **THEN** it receives `WRITE_REFUSED` and nothing changes

#### Scenario: Creation in another matter
- **WHEN** a restricted writer creates at a path in another client's matter, once where a withheld page occupies it and once where nothing does
- **THEN** both answers are the same `WRITE_REFUSED` and no bytes change

#### Scenario: Inbox creation and inbox privacy
- **WHEN** a fenced connector creates a page in its inbox, and another fenced connector searches, recalls and reads
- **THEN** the page is written and named in the response, and the other connector never receives it

#### Scenario: A writer without write authority
- **WHEN** a non-owner audience with no client entry writes under a governed policy, or any restricted writer edits a page under `_Schema/`
- **THEN** it receives `WRITE_REFUSED`

#### Scenario: An overlapping configuration
- **WHEN** a client's writable scope is not denied to another fenced client
- **THEN** the connector boundary is unavailable until the host repairs the configuration

## MODIFIED Requirements

### Requirement: Write doors decide their target before acting

For a restricted writer, as "Restricted writers are confined to their writable scopes and inbox" defines it, a write door SHALL decide its target page before resolving, reading or changing it. `edit_memory`, `observe_memory` by path or memory reference, `replace_memory`, and `manage_memory_file` `append`, `move` source, `delete` of a file, and `reclassify` SHALL answer a target the writer may not see exactly as a missing target, and SHALL leave it unchanged. A folder delete by such a writer SHALL be refused with one `AUDIENCE_RESTRICTED` answer whatever a folder it may see holds, and a declared recursive delete SHALL be refused before anything is read; a folder holding only pages withheld from it SHALL be answered as a missing path, as its listing is. Every write door SHALL admit its target by that requirement before resolving, reading or changing it. A withheld target, an absent target and a target the writer may see but may not write SHALL all receive the same `WRITE_REFUSED: target is unavailable` answer, and a withheld page SHALL stay unchanged, including under `overwrite`. A move that updates wikilinks SHALL rewrite the linking pages the writer may write, and SHALL report counts, paths and contract results for the linking pages the writer may see, with no index report and no derived-graph outcome, always as the committed terminal, so its answer in every response detail, on that move and every later one, is the same whether or not a withheld page linked the moved one. A linking page the writer may not write SHALL be left unchanged and the move SHALL commit, so that page's link dangles exactly as it would with `update_wikilinks=false`; the owner's audit reports it as a `broken_wikilink` or `forward_reference`. The activity log line such a writer's move or file delete appends SHALL carry only the figures that writer may see. A file delete by such a writer SHALL count, list and log only inbound links from pages it may see; a link from a page withheld from it SHALL be orphaned as `force_orphan` would orphan it, and the owner's audit reports it. A reclassification proposal by such a writer SHALL count as references only inbound links from pages it may see. A page withheld from the mover whose bytes the move does not rewrite SHALL NOT be asserted against the move, even when the move changes how that page's own links resolve; it stays in the closure the move publishes, and its standing is the owner's to review. These rulings SHALL apply to restricted writers only; on a vault with no policy and no connector configuration every caller's writes SHALL be answered as the owner's are. The owner's writes SHALL be unchanged.

#### Scenario: A write door names a withheld page

- **WHEN** a restricted writer edits, observes, replaces, appends to, moves, deletes or reclassifies a withheld page
- **THEN** the answer is byte-identical to the answer for an absent page, and the withheld page's bytes are unchanged

#### Scenario: A restricted writer deletes a folder

- **WHEN** a restricted writer deletes a folder that holds visible pages, or both visible and withheld pages
- **THEN** it receives the same `AUDIENCE_RESTRICTED` refusal each time and nothing is trashed

#### Scenario: A restricted writer deletes a folder of withheld pages

- **WHEN** a restricted writer deletes, without declaring it recursive, a folder whose every page is withheld from it
- **THEN** it receives the answer a missing path gives, and nothing is trashed

#### Scenario: A vault with no policy

- **WHEN** a caller other than the owner deletes a folder or moves a page on a vault with no policy, where an erased page's tombstone exists
- **THEN** it is answered as the owner is

#### Scenario: A destination a withheld page occupies
- **WHEN** a restricted writer creates, overwrites or moves a page to a path outside its inbox, once where a withheld page occupies it and once where nothing does
- **THEN** both answers are the same `WRITE_REFUSED` refusal and the withheld page's bytes are unchanged

#### Scenario: A move rewrites a withheld page's link

- **WHEN** a restricted writer moves a page that a withheld page links
- **THEN** the withheld page is unchanged, and the move's answer, and the answer to each later move of that page or another, match the twin without that page in every response detail

#### Scenario: A visible page only a withheld relation reaches

- **WHEN** a restricted writer's move changes how a relation a withheld page authors resolves, and that relation alone ties a visible page to the move
- **THEN** that visible page is neither judged nor reported, and the move's answer, in every response detail, matches the twin without the withheld page

#### Scenario: A withheld page the move does not rewrite

- **WHEN** a restricted writer's move, with or without updating wikilinks, changes how a non-compliant withheld page's own link resolves, and the move does not rewrite that page
- **THEN** the move's answer, in every response detail, matches the twin without that page, and that page is unchanged

#### Scenario: A withheld page links a page a restricted writer deletes

- **WHEN** a restricted writer deletes a page that a withheld page links, with or without `force_orphan`
- **THEN** the inbound refusal, the delete's answer and the activity log line match the twin without that page, in every response detail, and the owner's audit reports the withheld page's orphaned link

#### Scenario: A withheld linker the move cannot rewrite cleanly

- **WHEN** a restricted writer moves a page, with `update_wikilinks=true`, that a withheld page in an append-only tree links, or that a withheld page links whose contract would refuse the rewrite
- **THEN** the move commits with an answer, in every response detail, matching the twin without that page; that page is unchanged, and the owner's audit reports its dangling link
