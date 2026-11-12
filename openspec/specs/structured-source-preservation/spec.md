# structured-source-preservation Specification

## Purpose
Make newly preserved structured files discoverable and useful while retaining exact original bytes and bounded, governed access to their content.

## Requirements

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

### Requirement: Automatic dataset inspection is bounded and truthful

Automatic cards SHALL expose only bounded structural metadata: parsed row count, column count, and bounded JSON field names. CSV/TSV header labels MUST NOT be copied into automatic cards because a headerless first data record cannot be distinguished reliably from a header. The first-record-as-header assumption used by exact tabular queries SHALL be explicit. Automatic inspection MUST use at most 1 MiB of original input, at most the existing 10,000 parsed rows, a CSV/TSV padded table shape of at most 100,000 cells, at most 64 displayed fields and at most 256 characters per field. Cards MUST distinguish available, limited and unavailable inspection without inventing counts or claiming exact query support for malformed content. Exact query limits remain independent.

#### Scenario: Valid nested JSON
- **WHEN** the existing JSON parser can locate records within the automatic inspection budget
- **THEN** the card identifies the observed fields and parsed row count
- **AND** existing nested-field query behavior remains available

#### Scenario: Inspection cannot complete
- **WHEN** a capture exceeds inspection limits or has malformed, non-UTF-8 or excessively nested data
- **THEN** preservation succeeds within the upload limit with a truthful inspection status
- **AND** the original is retained without raw parser errors or fabricated schema metadata in the card

### Requirement: Common text exports receive literal previews

New YAML, YML, JSONL, NDJSON, XML and TOML evidence uploads SHALL receive a literal UTF-8 preview bounded to 64 KiB of original input when no caller extraction is supplied. Truncation and invalid encoding MUST be explicit. Inspection MUST NOT execute document instructions, expand entities, resolve local references or access the network.

#### Scenario: Search a text export
- **WHEN** a UTF-8 text export is preserved
- **THEN** its bounded preview is available through the governed source companion
- **AND** the original remains byte-exact

#### Scenario: Large or invalid text
- **WHEN** an export is too large for the preview or contains invalid UTF-8 within the inspected prefix
- **THEN** the companion records truncation or unavailable encoding status
- **AND** preservation retains the original without falsely claiming full-content indexing

### Requirement: Existing privacy and compatibility boundaries remain intact

Inspection SHALL run locally without additional packages, models or network services. Newly supported literal formats MUST retain their existing binary companion class. Existing source companions MUST NOT be migrated or overwritten implicitly. Ordinary Records and Planning recall exclusions and authorization before raw dataset parsing MUST remain effective.

#### Scenario: Historical companion remains valid
- **WHEN** an existing literal-format artifact has a valid binary companion
- **THEN** introducing automatic previews for new captures does not invalidate or rewrite that companion

#### Scenario: Dataset is excluded from a reader
- **WHEN** governance excludes the uploaded dataset from the requesting reader
- **THEN** its rows and aggregates remain unavailable before parsing
- **AND** its generated card does not bypass governed retrieval

### Requirement: Sparse tabular inputs cannot amplify inspection memory

Automatic CSV/TSV inspection SHALL refuse table shapes exceeding 100,000 cells before allocating padded row dictionaries. This inspection limit MUST preserve the original with a stable `shape_limit` status and leave exact query limits unchanged.

#### Scenario: Wide header with many short records
- **WHEN** a small uploaded CSV declares many fields followed by enough short records to exceed the padded-cell budget
- **THEN** automatic inspection reports `shape_limit` without allocating the amplified table
- **AND** the original upload remains preserved

### Requirement: Ambiguous tabular headers stay behind exact queries

Automatic CSV/TSV source cards MUST NOT publish the first record as field labels. Discovery by the card SHALL use the source filename, caption, format and structural counts. Exact header and record content remains available through authorized structured reads, and through search only as the bounded value rendering that the small-dataset requirement allows, released at full level.

#### Scenario: Headerless CSV first record contains sensitive values
- **WHEN** a headerless CSV is uploaded without explicit format metadata
- **THEN** no first-record values are copied into its source card
- **AND** the card states the header assumption and records structural counts
- **AND** a reader who may not read the original at full level finds no first-record value through search

### Requirement: Small dataset values are searchable by readers who may read them

The derived search indexes SHALL hold a bounded text rendering of a preserved dataset's values when its card's inspection status is ready or limited, its original is at most 64 KiB, and it parses within the existing row limit. The rendering MUST be indexed under the dataset original's own path, never inside the card or any vault file. Search MUST release a value hit only to a reader whom the release plane allows to read the original at the full release level, the same decision that authorizes `query_dataset`; for every other reader the hit MUST be absent, not degraded to an excerpt. A record SHALL match only through a value: a query of field names alone finds no hit. A record SHALL match when one part of it with at most 40 fields holds query words that carry at least half of the query's weight, at least one of them in a value; function words and one-letter fragments of a query word SHALL NOT be required, every query word that holds a digit SHALL be required and all such words together SHALL sit in one field's value, and a word SHALL weigh more the fewer datasets hold it. When the reader's scope holds at least 40 indexed datasets, a word match SHALL be dropped when its matched words together occur in more than 2% of those datasets, and in more than 3. At most 3 value hits SHALL enter a search result, and among equally good value hits the dataset with the newest card SHALL come first. A released hit SHALL name the original, carry a passage of about 600 characters that starts at the match, and name `query_dataset` as the way to read exact rows. A value hit that is not released MUST leave the reader's other results exactly as they would be without value search. Datasets over the bounds, malformed datasets, and Records and Planning descendants SHALL stay excluded.

#### Scenario: Find a fact held only in a small dataset
- **WHEN** a small JSON dataset holding a distinctive value is preserved and the reader may read it at full level
- **THEN** a search for that value returns the dataset original with a passage that contains the value
- **AND** the card and the original bytes are unchanged

#### Scenario: A natural question finds the value
- **WHEN** a small dataset holds a supplier's name and, in the same object, its `iban`
- **THEN** searches for "Acme's IBAN", "IBAN of Acme" and "what is the IBAN for Acme" each return the dataset original
- **AND** the passage starts at the matching field and holds the IBAN

#### Scenario: A name that no dataset holds finds no value hit
- **WHEN** no dataset holds the word "Zorblex" and a dataset holds an `iban` field
- **THEN** a search for "Zorblex IBAN" returns no dataset original

#### Scenario: A number that differs in one group finds no value hit
- **WHEN** a dataset holds the account "EE81 7700 1234 5678 9099" and another field of the same record holds "9012"
- **THEN** a search for "EE81 7700 1234 5678 9012" does not return it

#### Scenario: Common words across many datasets find no value hit
- **WHEN** the scope holds at least 40 small datasets and more than 2% of them hold both "test" and "results" in their values
- **THEN** a search for "test results" returns no dataset original
- **AND** a search for a distinctive value in the same scope still returns its dataset

#### Scenario: Words far apart in a large record do not match
- **WHEN** a record holds one query word in an object and the other in a different object, and the smallest part that holds both has more than 40 fields
- **THEN** that record does not match

#### Scenario: Value hits take a bounded share of results
- **WHEN** more than 3 datasets match a query
- **THEN** the result holds at most 3 dataset originals

#### Scenario: The newest version comes first
- **WHEN** two datasets hold the same account number and match equally well
- **THEN** the dataset whose card is newer comes first, for the spaced and the unspaced number

#### Scenario: A reader who may not read the rows finds no value hit
- **WHEN** governance leaves the dataset's membership unresolved or below full level for the reader
- **THEN** a search for a value in it returns no hit for the original
- **AND** the card remains discoverable by its own title and metadata as before

#### Scenario: A withheld value hit displaces nothing
- **WHEN** a reader may not read a dataset at full level and a value in it matches the query
- **THEN** the reader's results, their order and their count equal the results without value search

#### Scenario: Field names alone do not find datasets
- **WHEN** a query names only columns that small datasets share
- **THEN** no dataset original is returned for that query

#### Scenario: A large dataset keeps card-only discovery
- **WHEN** a preserved dataset exceeds 64 KiB or the parse limits
- **THEN** none of its values are indexed
- **AND** its card is indexed exactly as before

#### Scenario: A raw-protected original stays searchable
- **WHEN** a small dataset is preserved under the release floor for explicitly protected immutable artifacts
- **THEN** its values are indexed under that original's own path like any other admitted dataset
- **AND** a search releases the hit only to a reader whom the release plane allows to read the original at full level
