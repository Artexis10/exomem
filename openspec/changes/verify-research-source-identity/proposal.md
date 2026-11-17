# Proposal

## Why

An agent saved a repository PDF as a journal article. The PDF was a later thesis summary that only cites the article. The same agent put a DOI from memory into another Source's `url`, and that PDF's first page prints a different DOI. Exomem kept both originals byte for byte. The capture reply never showed the agent what the document says about itself, so the wrong metadata became immutable Source metadata unchallenged.

Three gaps let this happen:

- A capture writes `extracted_by: pending` and returns. Nothing compares the claimed title or DOI with the document, and nothing reads the PDF's own metadata.
- A Source has no field that says whether it holds the full text, an abstract, a citation record, an excerpt or a different work that discusses the target.
- A wrong binary Source cannot be corrected. `options.correction` needs a new original, and the duplicate check returns `already_stored` when the agent resends the same bytes. `files.reclassify` changes only kind and domain. The owner ruled on 2026-11-16 that Sources keep their content and provenance unchanged.

A corrected Source is also invisible in search. Its hit keeps the wrong title, with no marker.

## What Changes

- **Identity evidence at capture.** A captured original that is a PDF is read once at capture: its first two pages without OCR, its document information and its XMP packet. The capture compares the DOIs it finds with the DOI in the claimed `url`. The Source records the result as `identity_check`, and the reply returns it with the document's opening text. A conflict, or a claimed DOI that the document does not show, puts the correction call in `next_action`.
- **Document form.** `save_source` takes an optional `document_form` from a new open registry, `document-forms`. The shipped pack holds `full-text`, `abstract`, `citation-record`, `excerpt` and `secondary-work`.
- **Metadata-only correction.** A correction without `files` or `content` reuses the predecessor's stored original after a fixity check. It writes a successor with the caller's metadata, the predecessor's `ingested_into` and a fresh identity check. The predecessor page and original stay byte-identical. The receipt stays the record.
- **Corrected Sources in search.** A search hit for a corrected Source carries `corrected_by`, naming the current successor the caller may read.
- **Missed conflicts surface later.** A new audit family, `source_identity_conflict`, reports an uncorrected Source whose recorded check is `conflict`.
- **Guidance.** The served contract and the scaffold tell agents five things:
  - Keep user-supplied files and the documents an answer relies on.
  - Take bibliographic identity from the document.
  - Label the document form.
  - Decide compilation.
  - Correct a wrong Source through the correction route.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `attachment-source-ingestion`: a captured document reports its own identity evidence.
- `durable-source-capture`: a metadata-only correction reuses the stored original; search marks a corrected Source.
- `command-surface`: Source document form, and the `source_identity_conflict` audit family.
- `agent-bootstrap-contract`: research-source custody, identity, form and compilation guidance.

## Impact

The changed code is in these files:

- `client_artifacts.py`, `add.py`, `commands.py` (capture) and `media_processing.py` (the OCR rerun)
- a new `document_identity.py`
- `source_corrections.py` and the search hit projection
- `audit.py`
- the vocabulary packs, `prominence.py` and the scaffold

The interface changes are additive. `save_source` gains one optional field. File rows gain an optional `identity` block, and search hits gain an optional `corrected_by`. `defusedxml` becomes a declared dependency of the `media-cpu` extra; it is already installed through `markitdown`.

The behaviour depends on a model's judgement, so the real-model incident cases decide each guidance line before release.
