## Context

A binary Source is its original plus a Markdown page that holds frontmatter and the extracted text. Capture stages the bytes, checks for an identical earlier Source (`client_artifacts._earlier_source`), renders the page (`add._render_source`) and returns. Media processing extracts the text later.

`options.correction` (`source_corrections.py`) writes a successor and a create-only receipt under `_Sources/corrections/`. Page reads, compiled-source admission, compilation proposals and custody downloads follow it through `resolve_current`. The predecessor is never rewritten, by requirement.

## Goals / Non-Goals

**Goals**
- The agent that saves a document sees, in the same reply, what the document says about itself. A DOI mismatch is stated, not inferred.
- One call corrects a wrong Source, with no re-upload and no loss of history.
- Readers can tell which Sources hold full text and which hold a record of a work.

**Non-goals**
- Exomem does not decide whether a document is the work its title names. A citing work can print the cited title and DOI. That judgement stays with the calling agent, which reads the opening text the reply returns. No server model is added.
- No capture is refused for identity reasons. Custody comes first.
- Office documents and images get no identity check at capture; their state is `unavailable`.
- Retrieval ranking and answer attribution are out of scope. The context compiler programme owns them.

## Decisions

### One identity function, two callers

`document_identity.check(pages, metadata_values, claimed_url)` is pure. Capture calls it with the first two pages of a PDF, read with PyMuPDF without OCR, plus the document information values and the XMP text values. Media processing calls it again after OCR, and only for a Source whose recorded state is `unavailable` with reason `no_text_layer`.

- **DOI syntax.** One expression matches the DOI syntax of the DOI Handbook (ISO 26324): `10.`, a registrant code, `/`, then a suffix. It is a format check over a fixed syntax, so it is sound under the repository's C6 rule, and the code says so in one line. Matching is case-insensitive, and trailing sentence punctuation is dropped. A break right after `10.` or right after `/` is joined first.
- **Claimed DOIs** are the DOI-syntax matches in the `url`. This covers `doi.org` links and publisher paths alike, without a host list.
- **Declared DOIs** are the matches in the XMP text values and the document information values. XMP is parsed with `defusedxml`. A citing work does not declare a cited DOI as its own, so this is the strongest signal.
- **Printed DOIs** are the matches in the first two pages.
- **States**, decided in this order:
  1. `unavailable`: not a PDF, no extractor, encrypted, no text layer or a parse error. The reason is named.
  2. `no_claim`: the `url` holds no DOI.
  3. `declared`: the metadata declares a claimed DOI.
  4. `conflict`: the metadata declares only other DOIs.
  5. `printed`: the pages print a claimed DOI.
  6. `conflict`: the pages print only other DOIs.
  7. `not_shown`: the document shows no DOI anywhere.
- **No state means "verified identity".** `printed` and `declared` say only where a DOI appears.

The Source page records `identity_check: {state, reason?, claimed_dois, declared_dois, printed_dois, pdf_title, checked_at}`. Media processing owns the field after capture, as it owns `extracted_by`.

The reply row gains `identity: {state, reason?, opening, pdf_title, declared_dois, printed_dois}`. `opening` is the first 600 characters of the two pages, with whitespace collapsed. On `conflict` or `not_shown`, the outcome sentence names the finding. `next_action` then becomes the metadata-only correction call, with the Source's ref and page hash filled in. That instruction replaces the capture-sweep sentence for that reply.

The read costs one PDF open and two page reads per captured PDF, independent of vault size. PyMuPDF already parses these files in process in media processing, so untrusted parsing does not move to a weaker boundary. Any exception yields `unavailable` and the capture continues.

### Document form is registry data

`vocabulary/packs/core/document-forms.yaml` follows the source-kind pattern: label, description, aliases, a vault overlay and auto-registration of a valid new key. No code branches on an entry key. The capture validates the key and writes `document_form`. The field is optional. A Source without it says nothing about its form, and readers show it as unstated.

### Metadata-only correction reuses the stored original

A correction with no `files`, `content` or `passages` is metadata-only. It applies to a predecessor that has a stored original.

1. The capture reads the original at the predecessor's `evidence_file`. It refuses with `SOURCE_ORIGINAL_CHANGED` when the SHA-256 differs from `binary_sha256`.
2. The bytes are staged as the successor's original, under the original filename and content type.
3. The duplicate check is skipped for every correction, because a correction is an explicit successor of named bytes.
4. The successor takes title, `url`, kind, domain, projects, tags and `document_form` from the call. It takes `ingested_into` from the predecessor and gets a fresh identity check.

The existing guards apply unchanged: the historical page hash, one successor per predecessor, canonical identity, authority and egress. A text Source still corrects with `content`.

An undo is a further correction that restores the earlier metadata. History only grows.

### Search marks a corrected Source

The search reply projection checks each returned Source hit with `source_corrections.has_correction`, which costs one receipt lookup per Source hit and is bounded by the page size. A corrected hit gains `corrected_by: {title, path}` from `resolve_current` when the caller may read the successor. A withheld or invalid chain adds nothing, as page reads already behave. Ranking is unchanged.

### A conflict the agent ignored surfaces later

The audit's Source pass already reads each Source's frontmatter. A Source whose `identity_check.state` is `conflict` and that has no correction receipt yields a `source_identity_conflict` finding, and the finding's `proposed_fix` is the correction call. `not_shown` is not reported, because many correct documents print no DOI on their first pages.

### Guidance earns its place

Each added contract or scaffold line is ablated in the real-model incident cases. A line that does not beat the arm without it is removed before release.

## Risks / Trade-offs

- **A false `conflict`** can come from a DOI split in an unhandled way. It costs the agent one comparison, and the agent sees every DOI found.
- **A citing work can print the target's DOI on page 1** and read as `printed`. The opening text and the guidance carry that case. The real-model case covers it.
- **The successor stores a second copy of the original.** Shared original storage, a separate change, removes the copy where it is installed.
- **Erasure of a corrected Source** must account for both pages of the chain. The erasure change owns that rule. This change adds no erasure path.
- **Media processing rewrites Source frontmatter.** It must keep `identity_check` and `document_form`. A regression test covers this.

## Load target

The product load target is 20,000 pages per vault (from the `make-vault-store-canonical` design).

- Capture adds at most 150 ms p95 per PDF. Proof: before-and-after timings on 1 MB, 20 MB and 300-page generated PDFs.
- Search adds one receipt lookup per returned Source hit.
- The audit adds one field read per Source that its existing pass visits.
- No added path scales with the vault.
