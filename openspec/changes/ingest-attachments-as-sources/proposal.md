# Proposal: ingest-attachments-as-sources

## Why

An attached file cannot be captured as a Source. `capture_source` accepts
`content: str` and nothing else, so the only way to route an attachment through
it is to retype the text through the model — lossy for a transcript, impossible
for an image. The one lossless file-handle path, `preserve_artifacts`, is
Evidence-only by specification: `client-artifact-preservation` requires each
staged file be committed through the `preserve_stream` Evidence path.

So the lane an artifact lands in is decided by what the transport can carry, not
by what the artifact is. The scaffold guidance is already correct — `SKILL.md`
routes Sources to `capture_source` and Evidence to `preserve_artifacts` — and
the tool set cannot honour it for anything with a file handle. This is a product
defect rather than an agent-prompt defect: the epistemically wrong route is the
convenient one.

Two live reproductions, both measured against the current implementation.

**A transcript captured as raw material for later compilation.** It belongs in
Sources; the convenient lossless path put it in Evidence.

**A screenshot attached later.** Same asymmetry on the routing question.

Alongside the routing defect, and independent of it, whether a stored artifact
receives a page at all is decided by an extraction-capability test. `preserve()`
writes one only when a description, extracted text, or a *media stub* applies,
and the stub condition asks whether the extension is extractable and whether the
bytes arrived as a stream rather than as text. Measured, that means a `.txt`
streamed through `preserve_artifacts` receives a page, the same `.txt` supplied
as text through `preserve_evidence` does not, and a `.csv`, `.json`, or `.eml`
receives one on neither path. An artifact with no page has no `exomem_id`, no
`ingested_into`, and no corpus presence, because only `.md` is indexed — so it is
invisible to find, to the graph, and to the unprocessed-source backlog. None of
that follows from whether the artifact's text can be extracted.

Both reproductions failed the same way at the next step, and this is the part
that produced the reported warning. `remember`
resolves a cited source by replacing the extension with `.md`, while preserve
writes the page as `<filename>.md` — so `session.txt` resolves to `session.md`
and `shot.png` to `shot.md`, neither of which exists:

```
TEXT sidecar: None                                (supplied as text, not streamed)
TEXT resolve -> …/2026-05-25-session.md           exists: False
IMG  resolve -> …/2026-05-25-shot.md              exists: False
IMG  sidecar resolve -> …/2026-05-25-shot.png.md  exists: True
```

That is the reported `source not found, ingested_into back-ref skipped`, and it
breaks the Source-to-compiled-note provenance loop for every preserved artifact,
in either lane.

Severity is raised by irreversibility. `move_file` refuses to move anything out
of `Evidence/` — a case scope must stay complete — and `reclassify_source` only
corrects `source_type` and `domain` within Sources. A misrouted artifact is
therefore permanently misfiled. The interface makes the wrong route both the
convenient one and the one-way one.

## What Changes

- Add lossless file-handle ingestion into Sources: `capture_source` accepts the
  same client file handles as `preserve_artifacts`, staged through the same
  bounded safe-fetch, persisted under `Sources/` with the source taxonomy
  applied. No bytes pass through model-visible arguments, and nothing is
  duplicated.
- Give every ingested artifact exactly one addressable page, in both lanes,
  regardless of whether its type is extractable, which input mode delivered the
  bytes, or whether a description was supplied. The page carries `type: source`,
  a stable `exomem_id`, `ingested_into`, and a pointer to the bytes. A pending
  media stub keeps its current shape, because reconciliation owns it.
- Resolve a cited artifact path to its page, so `sources:` accepts either the
  artifact or its page and the `ingested_into` back-reference lands.
- Give the out-of-band transports the same lane choice, so `/upload` and
  `transfer_artifact` cannot decide epistemics on a client's behalf.

Deliberately unchanged, and asserted rather than assumed:

- `preserve_artifacts` and `preserve_evidence` keep their Evidence meaning. The
  lane is stated by the tool's name, not by a mode flag whose default is wrong.
- Sources-to-Evidence promotion stays the only reclassification, one-way, with a
  required reason. Evidence remains non-exportable.
- Artifacts already stored under Evidence stay there. This change makes them
  citable; it does not move them and does not add a reversal path.

## Capabilities

### New Capabilities

- `attachment-source-ingestion` — lossless attachment capture into Sources, the
  page contract every ingested artifact satisfies, citation resolution from an
  artifact path, and transport-neutral lane selection.

### Modified Capabilities

- `client-artifact-preservation` — routing guidance becomes intent-first and
  capability-second, because a capable client now has two destinations rather
  than one.

## Impact

- `commands.py` (`op_capture_source`), `client_artifacts.py` (staging reused for
  a second destination), `preserve.py` (unconditional page), `add.py` (a source
  whose body describes bytes it does not inline), `note.py`
  (`_resolve_source_path`), and the out-of-band upload routes.
- The MCP tool surface moves: `tests/fixtures/mcp_tool_schemas.json` and
  `src/exomem/tool_surface_contract.json` are regenerated by
  `scripts/dump-tool-schemas.py`. The ChatGPT Personal Plugin attestation is not
  refreshed here and a changed fingerprint stays release-blocking until that
  consumer is verified separately.
- Scaffold guidance in `src/exomem/_scaffold/_Schema/` updates to route by intent
  first. It must stay generic; `tests/test_scaffold_no_leak.py` is the gate.
