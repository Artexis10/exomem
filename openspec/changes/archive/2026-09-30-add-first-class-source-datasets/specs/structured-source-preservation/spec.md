## Purpose

Make newly preserved structured files discoverable and useful while retaining exact original bytes and bounded, governed access to their content.

## ADDED Requirements

### Requirement: Dataset preservation creates a bound discovery card

New CSV, TSV and JSON evidence captures SHALL publish their original bytes and an addressable dataset card in the same create-only batch. The card MUST identify the original path, format, hash and byte size and use the explicit dataset source-card shape (`type: source`, `source_type: dataset-export`, `data_file`, `format`). It SHALL remain a citable source governed by its existing source type and tags. Automatic capture MUST NOT assert reviewed artifact semantics: a missing descriptor remains unresolved wherever semantic classification is required. The system MUST NOT inline dataset rows or caller-supplied extracted text into automatic dataset cards.

#### Scenario: Discover and query an uploaded table
- **WHEN** a valid table is preserved through a supported evidence capture entrypoint
- **THEN** ordinary source discovery can find its dataset card and authorized exact queries can read the original table
- **AND** downloading the original returns the uploaded bytes unchanged
- **AND** raw table rows do not become semantic search documents

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

Automatic CSV/TSV source cards MUST NOT publish the first record as field labels. Discovery SHALL use the source filename, caption, format and structural counts; exact header and record content remains available through authorized structured reads.

#### Scenario: Headerless CSV first record contains sensitive values
- **WHEN** a headerless CSV is uploaded without explicit format metadata
- **THEN** no first-record values are copied into its source card
- **AND** the card states the header assumption and records structural counts
