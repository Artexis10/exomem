# Mutation results and recovery

## Mutation results and safe retries

Successful product mutations return a compact decisive terminal by default. Its
first key, `user_summary`, is one plain sentence for the user: `Saved -> <path>`,
`Updated -> <path>`, `Moved <from> -> <to>`, `Deleted -> <path>`,
`Replaced -> <new path>, was <old path>`, `Discarded -> <path>` or `Restored -> <path>`.
Several files read `Saved 3 files -> first.md and 2 more`. A batch with a file that
failed or was already stored counts each state, and `Saved` covers only the files that
this call stored: `Saved 2 of 3 files -> first.md and 1 more; 1 file could not be saved`,
`Already saved -> <path>`, or `Nothing saved; 2 files could not be saved`. A single file
whose bytes were already stored also reads `Already saved -> <path>`. The `files` rows
carry the codes and reasons. A result whose status is
`replayed` reads `Already saved -> <path>`. Each sentence ends with the name of the store
that took the write, such as `Saved -> <path> (personal Exomem)`; the bootstrap's `store`
names the same store. A repeat of a request with the same idempotency key returns the
first reply, so it reads as the first call did.
A refusal carries no `user_summary`; see "Refusals" below. The reply carries only what
your next call can take: `path` or `paths`, the hashes a guarded write takes, any
`warnings`, and at most one `next_action` sentence (do it) and one `notice` sentence (tell the
user). `notice` appears only when the change may be incomplete: for example a background
update failed, or the call only checked a draft. Everything operational moves under
`diagnostics.operational`, which only `response_detail="full"` returns: `ok`, `status`,
`mutated`, `request_id`, `receipt_id`, `idempotency_key`, `before_hash`,
`warnings_count`, and every `derived_sync*`, `advisory_sync*`, `graph_sync*` and
`vocabulary_sync` field. A null receipt means that the surface supplied no replay
identity; it does not weaken the committed status.
When the write warned, `warnings` carries the texts — at most 8 entries of at
most 300 characters. `warnings_count` appears beside them only when the list is
shorter than the count: the rest were trimmed, and `response_detail="full"` shows
them all. An upload receipt reports its warnings as per-file rows under
`files` instead, and a mutation recovered from a portable receipt reports the
count alone because it retains no leaf content. A corpus write advisory includes
its review ref and complete 24-character signal fingerprint in its warning text;
triage binds to that exact fingerprint (dismiss requires a reason), and a stale
fingerprint simply lets the advisory re-emit when the counterpart changes or the
written page changes the detected signal class.
A compiled-note write may also carry `structure_suggestion`. Its `kind` is
`note_cohesion` while the written page has reached a size step and nobody has
answered that step. It holds `units`, `step_units` and `next_step_units`, the
review item's `ref` and `fingerprint`, and one `question`: does this page answer
more than one independent question? The runtime counts size and claims nothing
about the page. The field reports nothing about any other page, and it never
affects `status`, `mutated`, or replay. Nothing is reorganised by the runtime;
answering is the agent's decision with the user (see `writing.md`).
Use `response_detail="full"` when existing leaf diagnostics are needed under
`diagnostics`. Use `response_detail="legacy"` only for temporary compatibility
with the former raw result. Response detail is presentation-only: changing it
does not change mutation identity, execute the leaf again, or alter a replayed
terminal.

A selected media process/retry/reprocess request can finish without changing canonical
bytes. The default reply carries no `state`, `status`, `terminal` or `mutated`; with
`response_detail="full"` they sit under `diagnostics.operational` and read
`state: settled`, `status: settled`, `terminal: true` and `mutated: false`, beside the
request identity and bounded `media_results`. This means the request has completed, not
that a new content commit or extraction has completed. Inspect each item's state and error/remediation, and use the same
identity for exact replay. A failed-subset retry is a new request containing
only those failed paths.

Committed observation responses carry `unit_ref` and `removed_unit_ref` when their
producer supplies them. A committed page write carries `after_hash`, the exact hash of
the committed bytes; the `before_hash` sits under `diagnostics.operational`. `after_hash`
is a mutation identity, not a promise that nobody has edited the page since: pass it as
the next `expected_hash` and honor any stale-write refusal. Portable receipt recovery may
omit it; it never invents a missing identity.
Do not reread solely to confirm a successful commit. Use one bounded final
verification of the whole requested outcome, and extra reads only when content,
warnings or missing concurrency guards actually require them.

`graph_sync=pending` and `derived_sync=pending` describe asynchronous work, not a
failed canonical write. They appear under `diagnostics.operational` and in no
other part of the reply. Continue independent work and ordinary recall; do not
run maintenance merely to remove the pending label. A graph-dependent query can
still require current edges and return an explicit freshness refusal. When a
background update failed, the reply carries a `notice` that says the change may be
missing from search results.

Artifact preservation reports one terminal `state` per file: `stored` for bytes
this call committed, `already_stored` when the same SHA-256 already exists under
the same `Evidence/<scope>/<category>/` destination — that outcome names the
existing `path` and `ref`, writes nothing, and the batch summary counts it
separately — and `failed` with a stable code and sanitized reason. `outcome`
mirrors these for compatibility and reports `already_stored` as `stored` beside
`duplicate_of`. A batch whose response was lost is recovered by repeating the
identical call: the terminal is persisted before derived-state acknowledgement,
so the same identity replays the same per-file paths, hashes, `request_id` and
`receipt_id` without fetching or writing anything again. A retry under a new
identity is a different mutation, and bytes already under that destination come
back as `already_stored` rather than being stored a second time. `scope` and
`category` are each one path segment: a separator, reserved character, control
character or surrounding whitespace is refused with `INVALID_PRESERVE` naming the
field, before any handle is fetched, and nothing is silently normalised.

A file of a multi-file batch can end `failed` with `MUTATION_BUSY` when the
boundary stayed busy past the wait before that file began; files stored before
it keep their outcome. Once any file in a batch commits, the batch's identity is
settled, so repeating the identical call replays the batch and never retries the
failed file. Retry the failed files in a new call that names only them, under a
new identity. This differs from a call-level `MUTATION_BUSY` error, where nothing
committed and the same identity and payload are retried.

`MUTATION_WARMING`, `MUTATION_BUSY`, `MUTATION_ACKNOWLEDGEMENT_PENDING`, and
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` remain errors, not successful
terminals. Preserve the same mutation identity and unchanged payload when
following their `retry`: wait before retrying a warming or busy call; retry a pending
call only with the same identity; do not submit a new identity after a
committed-uncertain result—reconcile and retry only as instructed.

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
`response_detail="full"` a write also returns `diagnostics`: the complete envelope with
the umbrella code, the remediation text, request identity and wait times.

## Relation choices and unlinked names on a write

Name a specific relation on every edge. A write can answer in two ways.

- **A new generic edge is refused.** The error is `GENERIC_RELATION_REFUSED`. It applies
  to every pair of pages, a note to a note as much as a note to an entity. The write
  commits nothing. The compact error lists up to five `candidates`: the specific relations
  that fit the two pages, best first, with the plain link relation last. Repeat the call
  with one of them. With `response_detail="full"`, `diagnostics` holds the ranked
  `options` (up to eight, with `options_route` for the full list) and the `routes` for each
  way out: `routes.specific`, `routes.propose` (the `propose-relation` and `save-relations`
  calls, for when no option fits) and `routes.plain_link` (the last resort).
  No other answer keeps the generic edge: neither `relation_disposition="generic"` nor a
  `reason` on a `relations` item exists. An edge the page already carried is not judged
  again, so a page that holds an older generic edge stays editable.
- **`undeclared_referents` lists links to nothing.** Each of its `names` is a link that
  this write added and that has no page and no entity, with its `create-entity` route.
  Follow the route when the name is a durable referent: stable, recurring and
  useful beyond this source. Otherwise dismiss it the same way.

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

To commit an unrelated page, send `relation_disposition="reviewed_none"` and a
`relation_review_reason` naming why nothing qualified with the creation itself;
that one call commits and records the decision. Committing without them refuses
with `RELATION_DISPOSITION_MISSING`, and its `retry` is the call that records the
decision; fill in the reason placeholder. The umbrella code
`SEMANTIC_CONTRACT_BLOCKED` and its `remediation` text sit under `diagnostics`. A
`validate_only=true` call reports the
obligation first: it returns `relation_review_hash` alongside the draft fields
and lists the finding under `blocking_findings`. Committing that validated
draft sends its `draft_id`, `draft_hash` and `draft_token` and the
`relation_review_hash` too. Prefer adding the real relation over asserting none
exists; the disposition is for material that genuinely stands alone.

The decision binds the page, not its bytes. A later edit of a page that holds a
`reviewed_none` decision commits without any review field, and records no new
decision, while the page stays unconnected. An edit that removes a page's last
qualifying relation is still refused.

A refused Record append or update is held rather than lost. The error message names
the failing fields (at most three, then "and N more") and says the candidate is kept as a
held candidate. Its `retry` is the `change_records` call that resumes it with the `held` reference;
in `item` (append) or `changes` (update) a `null` clears a field and a value in angle
brackets is yours to supply. With `response_detail="full"`, `diagnostics.error.issues` lists
every failing field. Fix the named fields and resume, or `discard` the candidate. Never loop,
and never preserve diagnostic breadcrumbs as Evidence.

On MCP these expected refusals arrive as normal tool content with a top-level
`error`; inspect it rather than treating it as a transport failure. A `receipt_id`
is diagnostic and is not a transferable
cross-session replay key. `bootstrap` probes this vault's local OS
mutation boundary: `verified: true` binds the safe holder fields to the current
lock generation, while `verified: false` means the external holder is real but
cannot be safely attributed. Never infer vault content or identity from status.
