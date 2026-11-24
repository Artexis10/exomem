# Mutation results and recovery

## Naming pages

Name a page by its title or its short `id` wherever a tool takes a page path. A title
resolves at once, including a page created a second ago. An `id` looks like `abcdefg.hijk`:
the first part names the page and survives edits and moves, and the part after the dot names
the version you read. A vault path, a KB-relative path and an `exomem://` ref keep working.

A title that several pages share refuses with `AMBIGUOUS_REFERENCE`: `message` says how many
pages carry it, and `candidates` lists at most 5 rows of `title`, `type`, `date` and `id`,
newest first. Retry with the page argument set to the `id` of the page you mean. Copies of one page share an
`id`; then each row also carries its `path`, and the retry is `<path>`: set the page argument to the `path`
of the copy you mean. The `id` of a page, given as `note` to `edit_note` with `action="replace_body"`,
guards the edit against the version that id names; if the page changed since, the refusal is
`STALE_EDIT` and carries the current `id` in its retry. The `id` of a write reply names the
version that write left, so it guards your next edit of that page, even when the reply is a
replay. Every other edit reads the page again under the writer lease, so it needs no id.
A source and a superseded page take no guarded edit, so their `id` names the version
current when the reply was made.

## Mutation results and safe retries

Successful product mutations return a compact decisive terminal by default. Its
first key, `user_summary`, is one plain sentence for the user that names each page by its
title: `Saved -> <title>`,
`Updated -> <title>`, `Moved <from> -> <to>`, `Deleted -> <path>`,
`Replaced -> <new title>, was <old title>`, `Discarded -> <path>`, `Restored -> <path>` or
`Retracted -> <path>`. A Records retraction names the collection page, which stays.
A tool whose subject is a file names the file by its path instead.
A move that repointed links in append-only pages adds
`. Repointed the links to it in <n> append-only pages`, and `pointer_repointed`
names those pages by title; only their link spans changed.
Several files read `Saved 3 files -> first.md and 2 more`. A batch with a file that
failed or was already stored counts each state, and `Saved` covers only the files that
this call stored: `Saved 2 of 3 files -> first.md and 1 more; 1 file could not be saved`,
`Already saved -> <title>`, or `Nothing saved; 2 files could not be saved`. A single file
whose bytes were already stored also reads `Already saved -> <path>`. The `files` rows
carry the codes and reasons. A result whose status is
`replayed` reads `Already saved -> <title>`. Each sentence ends with the name of the store
that took the write, such as `Saved -> <title> (personal Exomem)`; the `read_instructions` reply's `store`
names the same store. A repeat of a request with the same idempotency key returns the
first reply, so it reads as the first call did.
When a `save_source` call passed `options.custody_only` and stored a new Source, the sentence adds
` (kept for custody only)` and the Source's frontmatter records `custody_only: true`.
A refusal carries no `user_summary`; see "Refusals" below. The reply carries only what
your next call can take: the `title` and short `id` of each page written (`pages` when
several), the hashes a guarded write takes, any
`warnings`, and at most one `next_action` sentence (do it) and one `notice` sentence (tell the
user). `notice` appears only when the change may be incomplete: for example a background
update failed, or the call only checked a draft. Everything operational moves under
`diagnostics.operational`, which only `detail="full"` returns: `path` or `paths`,
`ok`, `status`, `mutated`, `request_id`, `receipt_id`, `idempotency_key`, `before_hash`,
`warnings_count`, and every `derived_sync*`, `advisory_sync*`, `graph_sync*` and
`vocabulary_sync` field. A null receipt means that the surface supplied no replay
identity; it does not weaken the committed status.
When the write warned, `warnings` carries the texts — at most 8 entries of at
most 300 characters. `warnings_count` appears beside them only when the list is
shorter than the count: the rest were trimmed, and `detail="full"` shows
them all. An upload receipt reports its warnings as per-file rows under
`files` instead, and a mutation recovered from a portable receipt reports the
count alone because it retains no leaf content. A corpus write advisory includes
its review ref and complete 24-character signal fingerprint in its warning text;
`decide_review_item(review_item=<that ref>, decision="dismiss", why=...)` binds the
fingerprint the server last showed (dismiss requires a reason), and the advisory
re-emits when the counterpart changes or the written page changes the detected
signal class.
A compiled-note write may also carry `structure_suggestion`. Its `kind` is
`note_cohesion` while the written page has reached a size step and nobody has
answered that step. It holds `units`, `step_units` and `next_step_units`, the
review item's `ref` and `fingerprint`, and one `question`: does this page answer
more than one independent question? The runtime counts size and claims nothing
about the page. The field reports nothing about any other page, and it never
affects `status`, `mutated`, or replay. Nothing is reorganised by the runtime;
answering is the agent's decision with the user (see `writing.md`).
Its `kind` is `ownership` while the written page is asked whether it belongs in its
project and nobody has answered. It holds the review item's `ref`, `fingerprint` and
`id`, and one `question` that carries the ownership test and the call that keeps the
page where it is. A response carries one such advisory.
Its `kind` is `rename` after a write that changed a page's title so that its file name no
longer follows it. It holds the page's `ref`, its `current_name`, the `planned_name`,
the spelled-out `call` that renames that page, and an `outcome` sentence. The user's edit
is the approval. It is absent when the title did not change or the name still follows it,
and the rename one leads.
Use `detail="full"` when existing leaf diagnostics are needed under
`diagnostics`. Detail is presentation-only: changing it
does not change mutation identity, execute the leaf again, or alter a replayed
terminal.

A selected media process/retry/reprocess request can finish without changing canonical
bytes. The default reply carries no `state`, `status`, `terminal` or `mutated`; with
`detail="full"` they sit under `diagnostics.operational` and read
`state: settled`, `status: settled`, `terminal: true` and `mutated: false`, beside the
request identity and bounded `media_results`. This means the request has completed, not
that a new content commit or extraction has completed. Inspect each item's state and error/remediation, and use the same
identity for exact replay. A failed-subset retry is a new request containing
only those failed paths.

A committed observation write names the unit it wrote or removed. Pass that unit id as
the `observation` of the next `edit_note` call for it. A committed page write carries the
page `id`, which names the exact committed bytes; the `before_hash` sits under
`diagnostics.operational`. The `id` is a mutation identity, not a promise that nobody has
edited the page since: honor any stale-write refusal. Portable receipt recovery may
omit it; it never invents a missing identity.
Do not reread solely to confirm a successful commit. Use one bounded final
verification of the whole requested outcome, and extra reads only when content,
warnings or missing concurrency guards actually require them.

Only a committed result is a save. A call that could not run, failed in transport,
or returned no committed result saved nothing; say so, never report it saved.

`graph_sync=pending` and `derived_sync=pending` describe asynchronous work, not a
failed canonical write. They appear under `diagnostics.operational` and in no
other part of the reply. Continue independent work and ordinary recall; do not
run maintenance merely to remove the pending label. A graph-dependent query can
still require current edges and return an explicit freshness refusal. When a
background update failed, the reply carries a `notice` that says the change may be
missing from search results.

Artifact preservation reports one terminal `state` per file: `stored` for bytes
this call committed, `already_stored` when the same SHA-256 is already stored in
the vault — that outcome names the existing `path`, `page` and `ref`, and the
batch summary counts it separately — and `failed` with a stable code and
sanitized reason. An `already_stored` hand-over writes nothing, except that bytes
handed over into another Evidence family add that family to the existing page
(`family_added: true`, with `families` listing every family the page holds, the first 64 and `families_total` beyond that); a caller field that the existing page did not take, such as a filename or description, is named in `fields_not_applied` as `{field, provided, kept}`; the
page stays in its first folder, so browse a family by its tag filter. Raw-protected
bytes keep one copy per family. `outcome`
mirrors these for compatibility and reports `already_stored` as `stored` beside
`duplicate_of`. A batch whose response was lost is recovered by repeating the
identical call: the terminal is persisted before derived-state acknowledgement,
so the same identity replays the same per-file paths, hashes, `request_id` and
`receipt_id` without fetching or writing anything again. A retry under a new
identity is a different mutation, and bytes the vault already holds come back as
`already_stored` rather than being stored a second time. A committed file is never
reported as `failed` because its receipt reads badly. A row with no readable
`file_id`, or one that states `failed` itself, is the exception. A field that did
not read is left out of its row and named in `unverified_fields`, and the row keeps
its `outcome`. It keeps its `state` when `state` itself read. Every warning is cut
to 300 characters. A row with more than 8 warnings keeps the first 8, and
`warnings_omitted` counts the rest. A row that lists `hash` in `unverified_fields`
has no digest to check. Tell the person the file is kept and its digest is not
confirmed; never say that it matched. `scope` and
`category` are each one path segment: a separator, reserved character, control
character or surrounding whitespace is refused with `INVALID_PRESERVE` naming the
field, before any handle is fetched, and nothing is silently normalised.

A saved PDF Source carries an `identity` block in its file row. The block says where
DOIs appear. It never says that the document
is the work its title names. `state` is one of:

- `no_claim`: the `url` names no DOI.
- `declared`: the metadata of the PDF holds the DOI of the `url`.
- `printed`: the first page prints that DOI.
- `conflict`: the metadata and first page show only other DOIs.
- `not_shown`: the metadata and first page show no DOI.
- `unavailable`: the PDF could not be read, and `reason` says why. A scanned PDF is checked again after OCR.

`correctable` says whether this connection may record a Source correction. `false`
means it cannot, so the reply asks you to tell the person which value is wrong instead
of offering the correction call; `true` means only that the correction receipt may be
written — the content-hash and page guards can still refuse. `opening` holds the first
600 characters of the pages read. `claimed_dois`,
`declared_dois` and `printed_dois` list the DOIs found. Compare the opening text with
the work the person asked for. On `conflict` or `not_shown`, the sentence in the reply
names the finding, and `next_action` is the correction call when this connection may record
corrections; otherwise it asks you to tell the person which value is wrong. When the document shows
one DOI, the call names it: correct the Source then, unless the opening text shows a work that cites
that DOI, and tell the person. Call `save_source` with no files, the document's own `title`, the right
`url` and
`options.correction = {supersedes: <ref>, expected_hash: <the page's content hash>, reason}`. The
correction keeps the stored original, writes one successor Source and leaves the first
page unchanged. Take the content hash from `read_page` on the Source's ref just before
the call, because processing may have rewritten the page. A search hit for a corrected Source carries
`corrected_by` as `{title, path}`, the page that now stands for it. A successor that the
caller may not read adds nothing.

A note or Evidence write that a Records collection should also hold says so. When the
reply's `records_routing` files a new item, appends an occurrence or asks, the sentence
ends "not filed in <collection title>", and `next_action` is the call or the question.
The note is saved; the Record is not. Do not report the item as filed until the Record
call returns committed.

A file of a multi-file batch can end `failed` with `MUTATION_BUSY` when the
boundary stayed busy past the wait before that file began; files stored before
it keep their outcome. Once any file in a batch commits, the batch's identity is
settled, so repeating the identical call replays the batch and never retries the
failed file. Retry the failed files in a new call that names only them, under a
new identity. This differs from a call-level `MUTATION_BUSY` error, where nothing
committed and the same identity and payload are retried.

A save can instead return `state: "queued"` with `committed: false` and a
`queued_write.ref`. The memory was busy, so the service holds the save durably
and applies it in arrival order. It is not saved yet: tell the user it is
queued, never that it is saved. Do not resend it; a resend with the same
identity returns the same receipt. Read its outcome later with the `route`
the reply gives (`review_memory` mode `queued-write` with that `ref`): it ends
`applied`, or `failed` with its refusal and the original arguments to correct
and send again.

`MUTATION_WARMING`, `MUTATION_BUSY`, `MUTATION_ACKNOWLEDGEMENT_PENDING`, and
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` remain errors, not successful
terminals. Preserve the same mutation identity and unchanged payload when
following their `retry`: wait before retrying a warming or busy call; retry a pending
call only with the same identity; do not submit a new identity after a
committed-uncertain result—reconcile and retry only as instructed.

A collection write that ends `MUTATION_ACKNOWLEDGEMENT_PENDING` was accepted by the store and
may still commit. Recover in one of two ways, and never with a new identity or a changed
payload:

1. Replay the call with the same identity and the same payload. The product returns the
   recorded result and never writes twice.
2. Read the collection and the row by its natural key, then write only what is missing.

A read refused `COLLECTION_STORE_BUSY` committed nothing; retry it later.

## Refusals

A refused call returns `{"error": {"code", "message", "retry"?, "candidates"?}}` and
nothing else. `message` says what was refused. When there is no `retry` it carries the
whole fix as well, and for a write it ends with what happened to your data: `Nothing was
saved.`, `The change was saved.` or `Whether it was saved is not known.` A refusal that kept
something, such as a held candidate, says so instead. `code` is the specific code. `retry`
is the exact next call. It holds the tool name and `arguments`. For the tool you called,
merge `arguments` key by key, nested objects included, into the arguments you already sent;
a `null` clears an argument, and a value in angle brackets is for you to supply. For another
tool, `arguments` is the whole call. `retry.after_s` is a wait in seconds. `retry` is absent
when no repeat of the call can succeed.
`candidates` lists at most five valid values or titles to choose from. With
`detail="full"` a write also returns `diagnostics`: the complete envelope with
the umbrella code, the remediation text, request identity and wait times.

## Relation choices and unlinked names on a write

Name a specific relation on every edge. A write can answer in two ways.

- **A new generic edge is refused.** The error is `GENERIC_RELATION_REFUSED`. It applies
  to every pair of pages, a note to a note as much as a note to an entity. The write
  commits nothing. The compact error lists up to five `candidates`: the specific relations
  that fit the two pages, best first, with the plain link relation last. Repeat the call
  with one of them. With `detail="full"`, `diagnostics` holds the ranked
  `options` (up to eight, with `options_route` for the full list) and the `routes` for each
  way out: `routes.specific`, `routes.propose` (the `schema.propose_relation` and `schema.save_relations`
  operations, for when no option fits) and `routes.plain_link` (the last resort).
  No other answer keeps the generic edge: no argument keeps one. An edge the page already carried is not judged
  again, so a page that holds an older generic edge stays editable.
- **`undeclared_referents` lists links to nothing.** Each of its `names` is a link that
  this write added and that has no page and no entity, with its `connections.create_entity` route.
  Follow the route when the name is a durable referent: stable, recurring and
  useful beyond this source. Otherwise dismiss it with
  `decide_review_item(review_item=<its review ref>, decision="dismiss", why=...)`.

## Relation disposition on compiled writes

Once a vault holds any compiled page, a new compiled page must either carry a
qualifying typed relation or record that relations were reviewed and none
applied. The first page in an empty vault has nothing to relate to and commits
without this; every later page is subject to it. A page that genuinely cites its
counterpart — a provenance link, a supersession, any typed relation — satisfies
the rule as written and needs no extra argument, so connected material passes
for free and only disconnected material needs the explicit disposition.

A page that shares its subject with an existing page is not unrelated, even when no
specific relation holds. Give it a plain link (`- links_to [[...]]`), which satisfies
the rule; a later measurement or a sibling note on the same topic is the usual case.

To commit an unrelated page, send `unlinked_reason` naming why nothing qualified
with the creation itself; that one call commits and records the decision. Committing
without it refuses with `RELATION_DISPOSITION_MISSING`, and its `retry` is the call that
records the decision; fill in the reason placeholder. The umbrella code
`SEMANTIC_CONTRACT_BLOCKED` and its `remediation` text sit under `diagnostics`. A
`preview=true` call reports the obligation first and lists the finding under
`blocking_findings`. To commit a previewed draft, send the same call again without
`preview`: the server binds the draft and the relation review itself. Prefer adding the
real relation over asserting none exists; the reason is for material that genuinely
stands alone.

The decision binds the page, not its bytes. A later edit of a page that holds a
`unlinked_reason` decision commits without one, and records no new
decision, while the page stays unconnected. An edit that removes a page's last
qualifying relation is still refused.

A refused Record append or update is held rather than lost. The error message names
the failing fields (at most three, then "and N more") and says the candidate is kept as a
held candidate. Its `retry` is the `add_record` call that resumes it with the `held` reference;
in `item` (append) or `changes` (update) a `null` clears a field and a value in angle
brackets is yours to supply. With `detail="full"`, `diagnostics.error.issues` lists
every failing field. Fix the named fields and resume, or `discard` the candidate. Never loop,
and never preserve diagnostic breadcrumbs as Evidence.

On MCP these expected refusals arrive as normal tool content with a top-level
`error`; inspect it rather than treating it as a transport failure. A `receipt_id`
is diagnostic and is not a transferable
cross-session replay key. `read_instructions` probes this vault's local OS
mutation boundary: `verified: true` binds the safe holder fields to the current
lock generation, while `verified: false` means the external holder is real but
cannot be safely attributed. Never infer vault content or identity from status.
