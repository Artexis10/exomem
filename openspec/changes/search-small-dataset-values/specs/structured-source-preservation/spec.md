## MODIFIED Requirements

### Requirement: Dataset preservation creates a bound discovery card

New CSV, TSV and JSON evidence captures SHALL publish their original bytes and an addressable dataset card in the same create-only batch. The card MUST identify the original path, format, hash and byte size and use the explicit dataset source-card shape (`type: source`, `source_type: dataset-export`, `data_file`, `format`). It SHALL remain a citable source governed by its existing source type and tags. Automatic capture MUST NOT assert reviewed artifact semantics: a missing descriptor remains unresolved wherever semantic classification is required. The system MUST NOT inline dataset rows or caller-supplied extracted text into automatic dataset cards.

#### Scenario: Discover and query an uploaded table
- **WHEN** a valid table is preserved through a supported evidence capture entrypoint
- **THEN** ordinary source discovery can find its dataset card and authorized exact queries can read the original table
- **AND** downloading the original returns the uploaded bytes unchanged
- **AND** raw table rows become search content only as the bounded value rendering that the small-dataset requirement allows

#### Scenario: Ambiguous or changed dataset identity
- **WHEN** a dataset has duplicate cards or the bound original bytes change
- **THEN** existing governed raw-content classification fails closed
- **AND** automatic preservation does not weaken that refusal

## ADDED Requirements

### Requirement: Small dataset values are searchable by readers who may read them

The derived search indexes SHALL hold a bounded text rendering of a preserved dataset's values when its card's inspection status is ready or limited, its original is at most 64 KiB, and it parses within the existing row limit. The rendering MUST be indexed under the dataset original's own path, never inside the card or any vault file. Search MUST release a value hit only to a reader whom the release plane allows to read the original at the full release level, the same decision that authorizes `query_dataset`; for every other reader the hit MUST be absent, not degraded to an excerpt. A released hit SHALL name the original and carry a bounded passage around the match. Datasets over the bounds, malformed datasets, and Records and Planning descendants SHALL stay excluded.

#### Scenario: Find a fact held only in a small dataset
- **WHEN** a small JSON dataset holding a distinctive value is preserved and the reader may read it at full level
- **THEN** a search for that value returns the dataset original with a passage that contains the value
- **AND** the card and the original bytes are unchanged

#### Scenario: A reader who may not read the rows finds no value hit
- **WHEN** governance leaves the dataset's membership unresolved or below full level for the reader
- **THEN** a search for a value in it returns no hit for the original
- **AND** the card remains discoverable by its own title and metadata as before

#### Scenario: A large dataset keeps card-only discovery
- **WHEN** a preserved dataset exceeds 64 KiB or the parse limits
- **THEN** none of its values are indexed
- **AND** its card is indexed exactly as before
