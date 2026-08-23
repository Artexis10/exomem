# Design: ingest-attachments-as-sources

## Context

Almost everything this change needs already exists, on both sides of the line it
is trying to fix. `move_file` already implements Sources-to-Evidence promotion:
one-way, `promotion_reason` required, bytes verbatim, with the asymmetry argued
in a comment rather than assumed. The Evidence sidecar is already a `type:
source` page carrying `ingested_into: []`. `adopt` already renders a source page
that *points at* an artifact it does not inline, with `imported_from`,
`original_sha256`, and `original_bytes`. `ClientArtifactFile` is transport-generic.
Media reconciliation is already lane-neutral — it excludes Records and reserved
paths, never non-Evidence trees.

What is missing is a way to reach Sources with a file handle, and an addressing
contract that survives the trip. So this is assembly, and the design work is
mostly in choosing which of two existing conventions to extend and refusing to
invent a third.

## Two tools, not a lane flag

`capture_source` gains `files`; `preserve_artifacts` keeps its Evidence meaning
unchanged. The alternative — one ingestion command with `lane="source"|"evidence"` —
was rejected because a flag has a default, and every default here is wrong for
half the calls. A tool whose name states the lane makes the epistemically correct
route exactly as cheap as the incorrect one, which is the actual requirement:
both become a single call carrying a file handle.

It also keeps the blast radius honest. `preserve_artifacts` and its spec, its
safe-fetch guarantees, its per-file outcome contract, and its replay-window
caching are untouched; the new path reuses the staging function beneath them.

## Sources adopt the Evidence file convention, because the pipeline is wired to it

Two layouts were available for an artifact and its page:

- one stem — `2026-08-23-transcript.txt` beside `2026-08-23-transcript.md`
- the Evidence convention — `2026-08-23-transcript.txt` beside
  `2026-08-23-transcript.txt.md`

The one-stem layout reads better, matches how `Sources/` already names pages, and
would work with today's citation resolver unchanged. It was still rejected, and
the reason is measured rather than aesthetic: the sidecar's location is hard-wired
as `<binary.name>.md` in seven places across `media_processing.py` and
`media_worker.py`, and `media_worker.py:82` *asserts* that relationship as a
guard. A one-stem Sources layout would mean either media captured as a Source
never receives its extracted text, or rewriting the media pipeline's addressing —
which is a larger and riskier change than the defect being fixed.

So both lanes use `<filename>` plus `<filename>.md`. One convention vault-wide,
the media pipeline serves both lanes with no change, and the citation resolver is
fixed once for both rather than once per layout.

Naming authority stays with `add`: the page stem resolves through
`resolve_filename_slug` and `unique_path` as it does for every other source, and
the artifact filename is derived from that same resolved stem plus the supplied
extension. The two cannot diverge, and collision behaviour stays `add`'s
uniquify rather than `preserve`'s `ARTIFACT_EXISTS` refusal — one command should
not have two collision semantics. The attachment's original filename is recorded
in frontmatter rather than used as the path, the same way `adopt` records
`imported_from`.

## The pointer field keeps its misleading name

The artifact pointer is `evidence_file:`, which is wrong once a Source uses it.
It is read in roughly fifteen places — the media worker's regex, media
reconciliation's convergence checks, `find_types.media_file`, context packs,
review context, and scene frames. Renaming it buys a better word and pays for it
across the entire media pipeline, so this change keeps the field and records the
misnomer.

One consequence is not merely cosmetic and is fixed here: `review_context.py:40`
groups `evidence_file` under `_EVIDENCE_FIELDS`, so a Source page carrying the
pointer would be presented under an evidence heading — reproducing, in the
review surface, exactly the Source/Evidence confusion this change exists to end.
The label is decided from the page's tree rather than from the field name.
`find_types.py:119` already reads the field as a generic `media_file`, so
presentation there needs nothing.

## Every ingested artifact gets exactly one page

Today the page is conditional: `preserve()` writes a sidecar only when a
description, extracted text, or a media stub applies, and `preserve_artifacts`
supplies none of the three. A text attachment therefore lands as bytes with no
`exomem_id`, no `ingested_into`, and no corpus presence at all — the corpus
indexes only `.md`. That is what broke the provenance loop, and it is independent
of which lane the artifact went to.

The page becomes unconditional in both lanes. When there is no description, no
extracted text, and no media type, the page still carries `type: source`, a
stable `exomem_id`, `ingested_into: []`, the pointer, the original filename, the
SHA-256, and the byte count — enough to cite it, find it by title, and see what
it is. `schema.validate_source` requires non-empty `content`, so the body is
synthesized from that metadata rather than left empty; a source page whose body
describes bytes it does not inline is the shape `adopt` already ships.

For media the page keeps its current stub form — `media_type`, `extracted_by:
pending`, and the `## Extracted text` anchor the worker fills — so the existing
extraction path converges on it unchanged.

A shipped test pins the current absence (`tests/test_preserve.py` asserts
`sidecar_path is None` for a text artifact with no description). It is a
deliberate re-aim, not a deletion: the property worth pinning is that no
*empty* sidecar is written, and the replacement asserts the page exists and
carries the addressing contract.

## Citation resolves from the artifact, not only from the page

`note._resolve_source_path` replaces the extension with `.md`, so it can only
find a page named `<stem>.md`. Under the convention above the page is
`<filename>.md`, which is why both reproductions failed even though one of them
had a perfectly good page.

Resolution becomes ordered and additive: a path already ending `.md` is used as
given; otherwise `<path>.md` is tried, then `<stem>-notes.md` for the `.md`
artifact case `preserve` handles, and finally today's `.with_suffix(".md")` so
every citation that resolves now keeps resolving. The order matters — `<path>.md`
must precede the suffix replacement, or `shot.png` keeps resolving to a
non-existent `shot.md` while `shot.png.md` sits beside it.

This is where the "source not found" warning actually comes from, so it is
fixed once, in the resolver, rather than by teaching each caller a naming rule.

## Transport does not decide the lane

`transfer_artifact` mints a token and `/upload` receives the bytes, and both
land in Evidence because that is the only destination `preserve_stream` has.
Clients that cannot expose file handles would otherwise keep the original defect
in a second place. Both take the lane as an explicit parameter, carried on the
minted capability so the destination is fixed when the token is issued rather
than chosen by whoever posts the bytes.

## Fix forward, and what that leaves

Artifacts already in Evidence stay there. `move_file` refuses to move anything
out of `Evidence/`, and that refusal protects a real property: a case scope
claims to hold everything preserved for it, so removing an item changes what the
folder claims. This change does not weaken it and does not add a reversal.

What already-stored artifacts do get is addressability. The unconditional page
and the resolver fix are retroactive in effect — an existing preserved artifact
becomes citable as soon as it has a page, and `ensure_media_sidecar` already
exists as the back-fill for media. A back-fill for non-media artifacts follows
the same shape.

The residue is honest and worth stating: an artifact that entered Evidence only
because no Sources route existed remains classified as proof-bearing. Nothing in
the vault records that its lane was chosen by the interface rather than by
intent, so nothing can distinguish it later. Correcting those two items is
manual.

## Non-goals

**No new epistemic layer.** Sources, Evidence, compiled notes, and the one-way
promotion between the first two are unchanged. This change only makes the
existing model reachable with a file in hand.

**No byte duplication.** Staging streams to private temporary storage and the
commit moves bytes once; nothing is base64-encoded through model-visible
arguments, and no artifact is written to two trees.

**No lane inference.** The tool called states the lane. Guessing from MIME type,
filename, or content would put the decision back where the defect came from —
somewhere other than the caller's intent.

## Risks

1. **Tool surface movement.** Adding `files` to `capture_source` changes its
   MCP schema, so `tests/fixtures/mcp_tool_schemas.json` and
   `tool_surface_contract.json` are regenerated. `scripts/dump-tool-schemas.py`
   deliberately does not refresh the ChatGPT Personal Plugin attestation, and a
   changed fingerprint stays release-blocking until that consumer is verified.
2. **Scope and category derived positionally.** `ensure_media_sidecar` derives
   its tags from `Knowledge Base/Evidence/<scope>/<category>/…` by index. Applied
   to `Sources/<Folder>/…` it produces nonsense tags. It must become
   lane-aware, and a test should assert the Sources case rather than only the
   Evidence one.
3. **Two collision semantics in one command.** `add` uniquifies, `preserve`
   refuses. The design resolves this by giving `add` naming authority for the
   pair, but any future path that writes a Sources artifact through
   `preserve_stream` directly would reintroduce the split.
4. **Replay window.** `preserve_artifacts` returns a cached terminal result for
   a byte-identical retry. `capture_source` with `files` must behave the same, or
   a retried attachment capture writes twice under a uniquified name — which is
   worse than a refusal, because it duplicates silently.
5. **Guidance drift.** The scaffold currently routes by capability
   ("file handles → `preserve_artifacts`"). It becomes intent-first, and the
   leak guard means the wording must stay generic.
