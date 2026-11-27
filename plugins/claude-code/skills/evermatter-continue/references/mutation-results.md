# Mutation results and recovery

Read this when a write reply is anything other than a plain save, or when you must name a page that
several pages could match.

## Naming pages

Name a page by its title or by its short `id` wherever a tool takes a page. A title resolves at
once, even for a page created a second ago. The first part of an `id` names the page and survives
edits and moves; the part after the dot names the version you read.

- When several pages share a title, the refusal lists candidates. Retry with the `id` of the page
  you mean. When the candidates are copies of one page, retry with the `path` of the copy you mean.
- The `id` that a write reply returns names the version that write left. Pass it to guard your next
  `replace_body` on that page. If the page changed since, the refusal carries the current `id`:
  read the page again before you retry. Other edits need no `id`.
- A committed observation write names the unit it wrote or removed. Pass that unit id as the
  `observation` of the next `edit_note` call on it.

## Report only what committed

Only a committed result is a save. A call that could not run, failed in transport or returned no
committed result saved nothing: say so, and never report it saved.

- Repeat the reply's `user_summary` to the user. It names each page by its title and names the
  store that took the write.
- Do the reply's `next_action` when it has one. Tell the user its `notice`: the change may be
  incomplete, for example because a background update failed.
- Ask for `detail="full"` only when you need the diagnostics. Detail changes the presentation, not
  the write, and a replay with another detail writes nothing.
- Do not read the page again only to confirm a commit. Make one bounded check of the whole outcome,
  and read more only when content, warnings or a missing guard requires it.
- A pending graph or derived update is background work, not a failed write. Continue with
  independent work and recall, and run no maintenance only to clear the label. A query that needs
  current edges may still refuse until they are fresh.
- When a note or Evidence write says that a Records collection should also hold the item, the note
  is saved and the Record is not. Do not report the item filed until the Record call commits.
- A warning that carries a review ref is a corpus advisory. When it does not apply, dismiss it with
  `decide_review_item(review_item="<review ref>", decision="dismiss", why="<why>")`. It returns
  only when its counterpart or the written page's signal changes.
- A compiled-note write may carry a `structure_suggestion`. Answer it as
  [writing](writing.md#answering-a-structure-suggestion) describes.

## Files

A file save reports each file as stored, already stored or failed. Tell the person a file is kept
only when its row says stored or already stored. Bytes the vault already holds are stored once, and
the row names the earlier copy. When a row cannot confirm the file's hash, say the file is kept and
its digest is unconfirmed; never say it matched.

A saved PDF Source carries an `identity` finding about the DOIs it shows. It never proves that the
document is the work its title names. Compare its opening text with the work the person asked for.
On a conflict or no DOI, follow `next_action`: it is the correction call when this connection may
record one, otherwise it asks you to tell the person which value is wrong. Read the Source's content
hash with `read_page` just before a correction, because processing may have rewritten the page. A
correction keeps the stored original and writes one successor Source.

A media process, retry or reprocess request can finish without changing any content. A settled
request is complete; it does not mean a new extraction exists. Read each item's state and remedy.
Retry only the failed items, in a new request.

## Retries and identity

Every write has an identity. The same identity with the same payload replays the first reply with a
top-level `replayed: true`, and never writes twice. A reply that defines its own `replayed` field keeps
that field as it was. A new identity is a new write.

1. When a call-level warming or busy error says nothing committed, wait for its `retry` hint. Then
   retry with the same identity and the same payload.
2. When a batch stored some files and failed others, the batch is settled. Repeating it replays the
   same outcome. Retry only the failed files in a new call, under a new identity.
3. When the acknowledgement is pending or the commit is uncertain, never send a new identity or a
   changed payload. Replay the same call, or read the collection row by its natural key and write
   only what is missing.
4. When a lost reply leaves a batch's outcome unknown, repeat the identical call. It returns the
   recorded result without writing again.

A read refused because the collection store is busy committed nothing; retry it later.

### Queued writes

A save can come back queued and not committed, when memory was busy. The service holds it and
applies it in arrival order. Tell the user it is queued, never that it is saved, and do not resend
it. Read its outcome later with
`inspect_memory(operation="review.queued_write", arguments={"ref": "<queued write ref>"})`. It ends
applied, or failed with its refusal and the original arguments to correct and send again.

## Refusals

On MCP, an expected refusal arrives as normal tool content with an `error`, not as a transport
failure. Its `message` says what was refused and, for a write, whether anything was saved. Report
that outcome as the message states it.

When the refusal has a `retry`, it is the exact next call. For the tool you called, merge its
`arguments` key by key into the arguments you sent: `null` clears one, and a value in angle brackets
is yours to supply. For another tool, `retry` is the whole call. Wait `after_s` seconds when it
gives one. With no `retry`, no repeat of the call can succeed; the `message` holds the fix.

### Relations on a write

Name a specific relation on every new edge. A new generic edge is refused for every pair of pages,
and the write commits nothing. Choose one of the listed candidates, best first. When none fits,
propose one with `schema.propose_relation` and save it with `schema.save_relations`
([vocabulary](vocabulary.md)). The plain link is the last resort. An edge a page already carried is
not judged again.

Once the vault holds a compiled page, each new compiled page carries a typed relation or records
that none applied. A page that cites its counterpart already satisfies the rule. A page that shares
its subject with an existing page is related: give it a plain link, such as `- links_to [[Page]]`.
For material that truly stands alone, send `unlinked_reason` with the creation call. Prefer the real
relation over that reason. A later edit needs no new reason, but an edit that removes a page's last
qualifying relation is refused.

When a write lists `undeclared_referents`, each name is a link to nothing. Create the entity when
the name is a durable referent: stable, recurring and useful beyond this source. Otherwise dismiss
it with `decide_review_item` and a reason.

### Held Record candidates

A refused Record append or update is held rather than lost. Fix the fields the message names and
resume with its `retry`, which carries the `held` reference, or discard it with `records.discard`.
Never loop on the same refusal, and never preserve diagnostic breadcrumbs as Evidence.
