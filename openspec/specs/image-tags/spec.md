# image-tags Specification

## Purpose
TBD - created by archiving change image-zero-shot-tags. Update Purpose after archive.
## Requirements
### Requirement: Zero-Shot CLIP Image Tags

The system SHALL, when image tagging is enabled (`EXOMEM_IMAGE_TAGS`), enrich an image's
extracted text with zero-shot tags computed from the already-loaded CLIP model: it SHALL embed a
fixed generic tag vocabulary with CLIP's text encoder (once, cached), cosine the image's CLIP
embedding against that vocabulary, and select the top-K tags whose cosine score clears a
configured threshold. The selected tags SHALL be appended to the image's extracted text as a
single `Tags: <a>, <b>, …` line so they are indexed by BM25 and the bge text index.

#### Scenario: Depicted concept becomes findable text

- **WHEN** an image is extracted with `EXOMEM_IMAGE_TAGS` set and its CLIP embedding scores the
  vocabulary terms `invoice` and `table` above the threshold
- **THEN** the extracted text gains a `Tags: invoice, table` line (after any OCR/caption text)
- **AND** that line is stored in the image's sidecar and indexed like any other extracted text

#### Scenario: Tags are ordered and capped

- **WHEN** more vocabulary terms clear the threshold than the configured top-K
- **THEN** only the top-K highest-cosine tags are emitted, in descending-score order

#### Scenario: Reuse the loaded CLIP model and device logic

- **WHEN** tags are computed
- **THEN** the existing CLIP model singleton and its device selection (CPU when ASR is active, per
  `EXOMEM_CLIP_DEVICE`) are reused with no additional model load and no new dependency

### Requirement: Configurable Top-K and Threshold

The number of tags and the cosine threshold SHALL be configurable via `EXOMEM_IMAGE_TAGS_TOPK`
and `EXOMEM_IMAGE_TAGS_THRESHOLD`, with sane defaults when unset or unparseable. Only tags whose
score is greater than or equal to the threshold SHALL be emitted.

#### Scenario: Threshold filters weak matches

- **WHEN** a vocabulary term's cosine score is below `EXOMEM_IMAGE_TAGS_THRESHOLD`
- **THEN** that term is not emitted as a tag

#### Scenario: Top-K override limits output

- **WHEN** `EXOMEM_IMAGE_TAGS_TOPK` is set to 1 and several terms clear the threshold
- **THEN** exactly one tag — the highest-scoring — is emitted

#### Scenario: Unparseable override falls back to the default

- **WHEN** `EXOMEM_IMAGE_TAGS_TOPK` or `EXOMEM_IMAGE_TAGS_THRESHOLD` is set to a non-numeric value
- **THEN** the corresponding built-in default is used and tagging still proceeds

### Requirement: Generic Tag Vocabulary

The tag vocabulary SHALL be a fixed, generic set of common visual concepts (objects, scenes,
document and screen kinds) shipped in the package. It SHALL contain no personal, brand, tenant, or
vault-structure tokens, so it passes the source leak guard that scans all of `src/exomem/`.

#### Scenario: Vocabulary is generic and leak-safe

- **WHEN** the package source is scanned by the leak guard (`tests/test_scaffold_no_leak.py`)
- **THEN** the vocabulary introduces no personal or brand token and the scan passes

#### Scenario: Vocabulary terms are clean and unique

- **WHEN** the vocabulary is loaded
- **THEN** every term is a non-empty lowercase concept and there are no duplicate terms

### Requirement: Default-Off and Byte-Identical When Unset

Image tagging SHALL change no behavior unless `EXOMEM_IMAGE_TAGS` is set. With the flag unset, the
image extraction output SHALL be byte-identical to the current OCR (plus optional caption) output,
and no tagging or CLIP-text code path SHALL run.

#### Scenario: Flag unset leaves extraction unchanged

- **WHEN** an image is extracted with `EXOMEM_IMAGE_TAGS` unset
- **THEN** the extracted text and engine are exactly today's OCR/caption result with no `Tags:` line
- **AND** no tag computation is performed

### Requirement: Soft-Fail Degradation

The tagging path SHALL soft-fail. Any failure — a missing CLIP/Pillow dependency, an unloadable
model, a GPU/cuDNN error, an unreadable image, or any encoding error — SHALL be logged and degrade
to producing no tags. Image extraction MUST still complete successfully; the path MUST NOT raise.

#### Scenario: CLIP dependency absent

- **WHEN** the CLIP model or Pillow is not importable while tagging is enabled
- **THEN** no tags are appended and the image's OCR/caption text is extracted and stored unchanged
- **AND** the failure is logged and extraction does not raise

#### Scenario: No vocabulary term clears the threshold

- **WHEN** tagging runs but no vocabulary term's score clears the threshold
- **THEN** no `Tags:` line is appended and the extraction output is the unchanged OCR/caption text

