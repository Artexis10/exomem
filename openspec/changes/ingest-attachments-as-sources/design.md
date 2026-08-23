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

Today the page is conditional, and the condition is an extraction-capability
test wearing the wrong hat. `preserve()` writes one when a description, extracted
text, or a media *stub* applies, and the stub condition asks two questions that
have nothing to do with addressability: is this extension extractable, and did
the bytes arrive as a stream rather than as text. Measured, the fallout is:

| Artifact | Delivered as | Page today |
|---|---|---|
| `.txt` | stream (`preserve_artifacts`) | yes — text is extractable |
| `.txt` | text (`preserve_evidence`) | no — the stub wants absent bytes |
| `.png` | either | yes — image is extractable |
| `.csv`, `.json`, `.eml` | either | no |

An artifact with no page has no `exomem_id`, no `ingested_into`, and no corpus
presence at all, because only `.md` is indexed. That is independent of which lane
the artifact went to, and it is why an initial reading of the reproduction
blamed the lane for a failure the extension had already caused.

The page becomes unconditional in both lanes. Where no description and no
extracted text were supplied, it carries the artifact's identity — the original
filename, the SHA-256, and the byte count — alongside `type: source`, a stable
`exomem_id`, `ingested_into: []`, and the pointer. `schema.validate_source`
requires non-empty `content`, so the body is synthesized from that metadata
rather than left empty; a source page whose body describes bytes it does not
inline is the shape `adopt` already ships.

Those three fields are named `original_filename`, `binary_sha256`, and
`binary_size` because the media pipeline already writes and checks them under
exactly those names. One vocabulary for describing an artifact, whether the page
was written at capture or re-rendered by reconciliation.

The one page this path does not describe is a *pending media stub*.
Reconciliation owns those: it re-renders them with the same provenance computed
from the binary itself, and decides by comparing those fields whether a stub is
already current. Writing them at capture would move that decision, so the gate is
`not want_stub` rather than "not media" — a `.txt` supplied as text is media by
type but is not a pending stub, and it does get its identity fields.

A shipped test pins the current absence (`tests/test_preserve.py` asserts
`sidecar_path is None` for a text artifact with no description). It is a
deliberate re-aim, not a deletion: the property worth pinning is that no *empty*
page is written and that the page never inlines the artifact's bytes, so the
replacement asserts the addressing contract and the absence of the content.

## Media reconciliation owns any pending media page, and rewrites it as Evidence

This is the largest thing the change has to deal with, and the first design
pass missed it. `reconcile_media` does not merely fill a page's extracted text —
when a media page is not already in the canonical pending shape it **re-renders
the whole page** from `preserve._render_sidecar`, keeping only `exomem_id` and
demoting the previous body into `## Preserved notes`. The canonical shape it
compares against requires `source_type: other`, which no real Source has.

Measured on a Source-shaped page for an image under `Sources/Sessions/`, with a
kind, a domain, and projects on it:

```
title:       Riverside council walkthrough  ->  "Evidence: 2026-08-23-walkthrough.png"
source_type: session                        ->  other
domain:      urban-planning                 ->  dropped
projects:    [riverside]                    ->  dropped
tags:        [session, riverside]           ->  [evidence, evidence, uncategorized]
body                                        ->  demoted under "## Preserved notes"
```

The locator line reads `Preserved under Evidence/evidence/uncategorized/`, a path
that does not exist, because scope and category are derived by locating a literal
`evidence` segment in the path and defaulting when there is none. That is the
same positional assumption `ensure_media_sidecar` makes, except here it destroys
classification rather than producing poor tags.

So an image captured as a Source would lose its classification the first time
media processing touched it, silently, and the loss would look like ordinary
convergence. Three responses were considered:

- **Leave Sources media unmarked**, so reconciliation never claims it. Rejected:
  a captured screenshot would never be OCR'd, which removes most of the reason to
  capture it losslessly.
- **Two pages per artifact** — a pipeline-owned stub plus a Source page citing
  it. Rejected: it contradicts the one-page contract this change just
  established, and doubles the addressing problem it set out to fix.
- **Make the pending shape and its re-render preserve what they do not own.**
  Taken. The pipeline's business is `media_type`, `evidence_file`,
  `extracted_by`, `processing_state`, and the binary provenance fields; a page's
  identity, classification, and body belong to whoever captured it. The canonical
  shape check becomes a check over the fields the pipeline owns rather than over
  the whole frontmatter, and the re-render edits those fields in place instead of
  rebuilding the page.

That is a genuine widening of this change, and it is why the layout decision
above — reusing the Evidence file convention so the pipeline serves both lanes
unchanged — bought less than it appeared to. The addressing is shared; the
*ownership* was not, and had to be separated anyway.

It also explains, after the fact, why writing artifact provenance onto a pending
stub was worth avoiding at capture: not because it would flip the convergence
check, which it provably cannot without `processing_state` and the timestamp
fields, but because those pages are not the capture path's to describe.

## Citation resolves from the artifact, not only from the page

`note._resolve_source_path` replaces the extension with `.md`, so it can only
find a page named `<stem>.md`. Under the convention above the page is
`<filename>.md`, which is why both reproductions failed even though one of them
had a perfectly good page.

Resolution becomes ordered and additive, with the candidate list chosen by
whether the citation already ends `.md`. If it does not, the candidates are
`<path>.md` — the artifact's own page — then today's `.with_suffix(".md")`, so
every citation that resolves now keeps resolving. If it does, the ambiguity runs
the other way: the citation is either a full page path or an `.md` artifact whose
page `preserve` names `<stem>-notes.md` to avoid a doubled extension, so that
form is tried first and the path itself second. The first candidate that exists
wins; when none does, the historical candidate is returned so a genuinely missing
source still reports the path a caller would expect.

The order matters — `<path>.md`
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

## Promotion has to move a pair, and does not yet

Making an artifact a two-file unit — bytes plus the page that addresses them —
breaks an assumption `move_file` was entitled to make. Measured on a captured
Source promoted into Evidence with a stated reason:

```
binary at destination:   True
page at destination:     False
page still in Sources:   True, with evidence_file pointing at the old path
```

So the promotion half-lands. The bytes reach Evidence with no page, which is the
unaddressable state this change exists to remove, and the page left behind in
Sources describes an artifact that is no longer there. Nothing refuses, and the
result reads as success.

The fix belongs in the move rather than in a caller: an artifact and its page
are one unit, so relocating either has to relocate both and rewrite the pointer,
in one atomic batch, with the existing promotion-reason requirement and the
one-way refusal unchanged. `move_file` already carries `extra_writes` and
`content_transform` for exactly this class of caller-owned, declared change, so
the seam exists; what does not exist is a notion of a two-file unit.

Implemented that way. Whichever half a caller names, the operation is
normalized onto the page before any guard runs, and the bytes are renamed
alongside it inside `mutate` — after the page's own rename, so a failure of
either rolls back both. The append-only guards, the promotion-reason
requirement, and the one-way refusal all still see the page path and are
unchanged. An ordinary source page whose stem happens to have no sibling file
is not a pair, and still moves alone; that control is asserted rather than
assumed.

## Two schema generators disagreed, and nothing had made them prove it

Adding a structured parameter to `capture_source` failed a shipped test that
compares the hosted agent contract against the personal MCP surface. Measured,
the same command rendered `files` two ways: the hosted contract emitted
`items: {$ref: "#/$defs/ClientArtifactFile"}`, the personal surface inlined the
model. The server-side registration path compresses schemas; building a
`FunctionTool` directly, which is what the hosted contract does, does not.

No hosted-alpha command had ever carried a structured type, so the divergence
had never been exercised — `preserve_artifacts` uses the same model but is not
in the hosted alpha set. The test that catches it exists precisely because the
two must agree, so this is a latent defect surfaced rather than a cost of this
change, and the hosted contract now inlines to match.

One shape decision follows from it. `files` on `capture_source` is
`list[ClientArtifactFile]` defaulting to empty, not `... | None`. A union
renders as an `anyOf`, which pushed the item model into `$defs` on both
surfaces and made the two generators disagree in a second, subtler way. An
empty list already means "no files supplied", so the union bought nothing.

## Routing guidance is not doctrine, and the bootstrap says so

The obvious home for "the lane is what the artifact is for" looked like the
bootstrap's epistemic commitments. It is the wrong one, and two shipped tests
say why. Commitments must name no tool — they are doctrine that holds whatever
the surface looks like — and this rule is only actionable once it names
`capture_source` against `preserve_artifacts`. The compact bootstrap also has a
pinned byte ceiling, currently with 197 bytes of headroom, so a commitment-sized
addition trips it.

So the guidance lives in the tool descriptions, the scaffold `SKILL.md`, the
operations reference, and the workflow skills — where an agent reads it at the
moment it is choosing. That is also the more honest placement: this is a fact
about the surface, not a claim about knowledge.

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
