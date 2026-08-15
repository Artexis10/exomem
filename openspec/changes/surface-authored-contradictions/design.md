## Context

Three mechanisms touch contradiction today and none of them reads an authored
`contradicts` edge.

`audit._check_corpus_contradictions` sweeps every active read-write compiled
conclusion against the embedding sidecar, keeps pairs whose max chunk cosine lands
in `[CONTRADICTION_FLOOR, DUP_THRESHOLD)`, orders them by
`cosine + w * pair_dormancy`, demotes same-family pairs, caps at
`EXOMEM_CONTRADICTION_TOP_N`, and returns `AuditFinding` rows. It short-circuits to
`[]` whenever `EXOMEM_DISABLE_EMBEDDINGS` is set, which is the whole fast test
suite.

`attention._rank` fuses each queue's emission order through weighted RRF and dedups
by anchor path, then `attention._apply_review_state` binds each item to a stable
`review_id` plus a `signal_fingerprint` and folds in a `dismiss` / `snooze` decision
from `.review-state.json`. Emission order is rank, so whatever the contradiction
check emits first ranks first.

`corpus_aware.detect_duplicates` and `corpus_aware.detect_contradictions` run at
write time against a draft. They are the only near-duplicate mechanism in the
product; there is no standing duplicate queue. `edit.commit` calls
`detect_contradictions` after the write lands; `add` calls both before the page
exists.

The typed graph already stores authored relations. `contradicts` is a core,
symmetric relation, and `EpistemicGraphIndex.relation_participants` answers "which
pages participate in a typed edge" with an explicit availability status that never
false-empties. Nothing consumes it for review.

`context_pack._tension_pairs` measures the same cosine band among the packed notes
and labels every pair "proximity, not polarity — reader decides".

The MCP tool surface is frozen byte-for-byte by `tests/test_mcp_schema_fidelity.py`
against `tests/fixtures/mcp_tool_schemas.json` and
`src/exomem/tool_surface_contract.json`, both of which are guarded files. Tool
docstrings become tool descriptions, so no operation docstring may change.

## Goals / Non-Goals

**Goals:**

- Feed authored `contradicts` edges into the contradiction queue as first-class,
  triageable entries that outrank every proximity pair.
- Make asserted entries independent of the embedding sidecar, so the strongest
  signal survives a torch-less deploy and is testable in the fast suite.
- Make provenance explicit on every contradiction entry and every deep-pack tension
  pair, so a reader never confuses "you said these conflict" with "these sit close
  in vector space".
- Give a reader one durable way to say "these are rivals; keep both" that silences
  the write-time nag while resurfacing honestly the moment either note is edited.
- Exempt pairs whose authored structure already declares them as rivals from the
  redundant write-time proximity warning.

**Non-Goals:**

- Judging which rival is right, ranking rivals against each other, auto-merging,
  auto-superseding, or auto-dismissing anything.
- Adding a relation type. `contradicts` and `answers` already exist;
  `tests/golden/relation_compatibility.yaml` stays untouched.
- Building a standing duplicate queue. The write-time warning remains the only
  duplicate mechanism.
- Changing any MCP tool signature, docstring, parameter, or description.
- Inferring polarity from text. The claim-level polarity lane is unchanged.

## Decisions

### Asserted entries live inside `corpus_contradictions`, emitted first

An authored `contradicts` edge produces an `AuditFinding` in the existing
`corpus_contradictions` category rather than a new category. A new category would
have to be registered in `ALL_CATEGORIES`, `ATTENTION_CATEGORIES`, the attention
tiebreak order, and `review_memory`'s mode list, and every existing consumer that
asks for "the contradiction queue" would silently miss the strongest half of it.
Keeping one category also means the deduplication and multi-signal additivity rules
in `attention-queue` apply unchanged.

Ordering falls out of the existing contract: `attention._rank` treats emission order
as intra-queue rank, so emitting every asserted finding before the first proximity
pair is exactly "ranked above proximity". No ranking code changes.

The asserted lane runs before the `EXOMEM_DISABLE_EMBEDDINGS` short-circuit, because
an authored edge is read from the graph sidecar and needs no vectors. The proximity
sweep keeps its short-circuit verbatim.

### Endpoint eligibility mirrors the proximity lane

An asserted pair surfaces only when both endpoints pass `_is_active_compiled_rw`:
an active, read-write, compiled conclusion that is not an index or log hub. That is
the same gate the proximity sweep uses, and for the same reason — those are the only
pages a contradiction can actually be reconciled against through
edit / replace / supersede. An authored edge into a raw source, an archived note, or
a read-only tree is left alone rather than surfaced as unactionable work.

### Asserted pairs are not capped by `EXOMEM_CONTRADICTION_TOP_N`

That cap exists because the proximity sweep is combinatorial: every eligible page
against every other, bounded only by the band. Authored edges are finite, deliberate,
and written one at a time by a human. Capping them would mean silently hiding
something the user typed. The cap, the summary finding, and its `meta.truncated` /
`meta.shown` / `meta.total` counts therefore keep describing the proximity lane
exactly as before, which also keeps existing cap regressions byte-stable.

### An asserted pair suppresses its own proximity duplicate

Two notes can be both authored-as-contradicting and close in vector space; the
polarity case ("X works" / "X fails") is precisely where cosine is highest. Surfacing
the same pair twice under the same anchor would double its RRF vote in attention and
present the reader with two rows for one decision. The asserted entry is strictly
stronger, so the proximity duplicate is dropped before scoring — it is not counted
toward the cap and does not appear in the omitted count.

### Provenance is `meta`, not `detail`

`review_state.fingerprint` reads `meta.signal_version` verbatim and ignores every
other `meta` key, so adding `meta.provenance` cannot churn a stored dismissal.
`attention._reason` copies `finding.meta` wholesale into each reason, so provenance
reaches the ranked surface with no attention-side change. Existing proximity
findings gain `provenance: "proximity"` and keep their `signal_version` byte-for-byte
so no stored decision resurfaces spuriously.

Asserted findings compute their `signal_version` from the same two page signal
versions but through a distinct `asserted` discriminator, so an asserted entry and a
proximity entry over the same pair can never collide in the review-state store.

### The competing-alternatives stance is keyed on the pair, not the queue item

The obvious implementation — reuse the attention item's `review_id` — is wrong. An
attention item's identity and fingerprint fold in every category that flagged its
anchor, so the same pair has a different item fingerprint depending on whether the
anchor also happens to be stale or relation-poor that day, and the write-time check
in `corpus_aware` has no way to reconstruct it from two paths.

The stance is therefore recorded under a pair-derived `review_id`:
`item_id("competing:" + "|".join(sorted([ref_a, ref_b])))`, with a pair fingerprint
built from the two endpoints' `refs_for_paths` refs and their on-disk page signal
versions. It is written into the same `.review-state.json` with the same
`review_id:fingerprint` record key and the same atomic writer as dismiss and snooze —
"recorded like dismiss/snooze in review state" is literal.

Because the fingerprint folds in both endpoints' content, editing either rival
changes the fingerprint, the stored record no longer matches, and the pair resurfaces
as open. That is the required honest-resurfacing property, and it is the same
mechanism dismiss already relies on. `edit.commit` runs the write-time check after
the write lands, so an edit to a rival correctly re-warns once, and the reader can
re-affirm the stance.

### Queues honor the stance by consulting the pair record

`attention._apply_review_state` checks the item-level record first; if that yields a
non-open state, it wins, because it is the more specific decision about this exact
signal composite. Otherwise the pair stances for every contradiction the item carries
are consulted, and the item becomes `competing` only when ALL of them are stanced.
Since every queue filters by `item.state == state` and `competing` is not `open`,
"honored by all queues" falls out of the existing filter rather than needing
per-queue code.

An item can carry more than one pair, and that is the ordinary shape, not an edge
case: both lanes anchor a pair on `min(a, b)`, so a note that is the
alphabetically-first endpoint of two conflicts is one item with two reasons. Treating
that as "no pair" would make such an item unstanceable, and — worse — would strand an
existing stance the moment a second pair drifted onto the same anchor: the record
would stay live and keep muting the write-time warning while being impossible to
re-affirm or clear. So `pairs_from_reasons` returns every pair, `record_stance`
records each, and `clear_stance` clears each. `reopen` clears by review id rather
than by fingerprint, so it also releases a stance recorded against older content.

`competing` joins `VALID_ACTIONS` and `VALID_VIEWS` so the store, the state summary,
and the `state=` view parameter all accept it. `reopen` on a contradiction-pair item
clears both the item record and the pair record, so the inverse is complete.

### Discoverability without touching a tool docstring

`triage_memory`'s description is frozen by the schema-fidelity gate, so the new
action cannot be documented in its `Args`. Three response-side channels carry it
instead: the `INVALID_REVIEW_ACTION` error already renders `sorted(VALID_ACTIONS)`
and so names `competing` automatically; each asserted finding's `proposed_fix` names
the stance explicitly; and the CLI gains `exomem review competing <ref>` plus a
`--state competing` view, neither of which is part of the MCP schema. This is a real
limitation of the frozen surface, recorded below as a trade-off.

### The structural-pair exemption is graph-derived, not stance-derived

If the author already wrote `- contradicts [[B]]` on A, or both A and B `answers` the
same question page, they have declared the relationship structurally. The write-time
proximity warning then tells them nothing they did not type themselves. Both the
duplicate band and the overlap band consult the same predicate, because a genuine
rival pair often sits above the duplicate threshold, not below it.

The exemption applies only to the write-time warnings. The asserted queue entry is
deliberately not exempt: surfacing it is the entire point of this change, and the
reader dismisses or marks it competing through triage like any other item.

### An orphaned stance stays addressable by its own pair ref

A stance survives its pair leaving every queue — the proximity pair drifts out of
band, or the authored edge is deleted. The record is then on no item's `reasons`,
so the item-walking `clear_stance` cannot see it, while `DeclaredPairFilter` still
consults it and suppresses the write-time warning. `dismiss` orphans identically,
but a dismissal has no write-time side effect, so this one is the one that bites.

Rather than leave it un-clearable, `triage_memory(ref=<pair_ref>, action="reopen")`
now clears a stance addressed directly by its own pair ref — the ref the stance
write returns under `pairs[].ref`, and which every annotated contradiction reason
carries. That needs no new tool parameter, which matters because the tool schema is
frozen. An unknown ref still raises `REVIEW_ITEM_NOT_FOUND`; only a ref that
actually carries a stance record is treated this way.

Two bounded residuals remain, deliberately. Discovery still depends on holding the
pair ref: there is no "list every orphaned stance" surface, because the record
stores refs and hashes rather than paths and adding paths would change the persisted
record shape. And the orphan self-heals on the next edit to either endpoint, since
the write-time check reads both pages' current content — an edit changes the pair
signal version, the record stops matching, and the warning returns. The exposure is
therefore at most one suppressed warning on one write, on a pair the reader
themselves declared, and only while neither note changes.

### A stanced pair is annotated on the item, not hidden

An item that is only partially stanced — the drift case, where one of two pairs
carries a stance — would otherwise serialize as an ordinary open item with two
reasons and no hint that one of them is dispositioned and muting a warning. Each
contradiction reason therefore carries `pair_ref`, and a stanced one also carries
`stance`. The annotation is applied AFTER the item fingerprint is computed:
`review_state.fingerprint` reads only `category`, `meta.signal_version`, `detail`,
and `related_paths`, so these keys cannot feed back into review identity, but
ordering the write after the hash keeps that true independently of that field list.

### Deep packs label, they do not suppress

`context_pack._tension_pairs` gains asserted pairs (from authored `contradicts` edges
among the packed notes, emitted first and needing no sidecar) and a `provenance` field
on every pair. It does not consult review state. A pack is reasoning context, not a
work queue: knowing two packed notes are declared rivals is useful signal, and reading
`.review-state.json` during assembly would add coupling and cost to a read path that
deliberately touches only content, frontmatter, wikilinks, and precomputed vectors.

`embeddings_available` keeps meaning exactly what it means today — whether the
proximity pass ran — so an embeddings-off pack now reports
`embeddings_available: false` while still carrying asserted tension pairs.

## Risks / Trade-offs

- [`competing` is invisible to an MCP client because the frozen tool description
  cannot list it] -> Accept and route discovery through the `INVALID_REVIEW_ACTION`
  error, asserted findings' `proposed_fix`, and the CLI; revisit whenever the tool
  contract is next intentionally revised.
- [A pair-keyed stance is a second identity scheme in one state file] -> Reuse
  `review_state.item_id` and `review_state.fingerprint` verbatim under a `competing:`
  discriminator so the record shape, the record key, and the atomic writer stay
  single-sourced, and namespace it so it can never collide with an attention,
  activation, relation, or adoption id.
- [Editing either rival silences nothing and re-nags] -> That is the specified
  behaviour, not a defect: a stance that survived arbitrary edits would be a standing
  mute rather than a fingerprint-bound decision. Re-affirming is one triage call.
- [Suppressing a warning is a form of hiding] -> The suppression is only ever the
  reader's own explicit stance or their own authored edge, it is fingerprint-bound,
  and the pair still appears in the contradiction queue and in deep packs.
- [The asserted lane adds a graph read to an audit category that previously touched
  only the vector sidecar] -> It is ONE indexed `relation_edges` query for the whole
  vault, and the lane soft-fails to empty when the graph is disabled or warming
  rather than fabricating or blocking. The obvious implementation — a participant
  lookup plus an anchored lookup per participating page — is a trap:
  `relation_participants` narrows by anchor in Python AFTER running the same
  unnarrowed `relation_type IN (...)` query, so that shape costs O(pages x edges)
  with one read snapshot per page (measured ~1 s at 250 authored edges) and it
  reaches the retrieve/inject path through `find(pack=true)` and
  `memory_context.assemble_context`. `relation_edges` exists because
  `RelationFilterResult.provenance` keeps only one counterpart per page and so
  cannot answer "which page is joined to which" in a single pass.
- [The write-time exemption scales with vault edge count, not candidate count] ->
  `DeclaredPairFilter` builds one `DeclaredEdges` snapshot (two indexed queries) and
  one review-state read LAZILY on the first candidate and reuses both, so the cost is
  flat in the number of candidates and is paid only on a write that would have warned
  at all. The residual is real and worth stating plainly: that snapshot is
  proportional to the vault's TOTAL `contradicts` + `answers` edge count, not to the
  handful of candidates being checked, so a vault with an order of magnitude more
  such edges pays proportionally more on every warned write (measured 7.3 ms at 5
  edges, 69.6 ms at 250, with six candidates). Narrowing it would mean an
  anchor-narrowed query, which the graph layer does not currently support without
  the O(P x E) fan-out this change removed.
- [A warming graph silently yields no asserted entries] -> Documented in the spec as
  an explicit absence contract rather than a fabricated result; the proximity lane is
  unaffected, and the next audit after the rebuild surfaces them.
