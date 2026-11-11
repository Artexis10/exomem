## MODIFIED Requirements

### Requirement: Small dataset values are searchable by readers who may read them

The derived search indexes SHALL hold a bounded text rendering of a preserved dataset's values when its card's inspection status is ready or limited, its original is at most 64 KiB, and it parses within the existing row limit. The rendering MUST be indexed under the dataset original's own path, never inside the card or any vault file. Search MUST release a value hit only to a reader whom the release plane allows to read the original at the full release level, the same decision that authorizes `query_dataset`; for every other reader the hit MUST be absent, not degraded to an excerpt. A record SHALL match only through a value: a query of field names alone finds no hit. A record SHALL match when one part of it with at most 40 fields holds query words that carry at least half of the query's weight, at least one of them in a value; function words and one-letter fragments of a query word SHALL NOT be required, and a word SHALL weigh more the fewer datasets hold it. When the reader's scope holds at least 40 indexed datasets, a word match SHALL be dropped when its matched words together occur in more than 2% of those datasets, and in more than 3. At most 3 value hits SHALL enter a search result, and among equally good value hits the dataset with the newest card SHALL come first. A released hit SHALL name the original, carry a passage of about 600 characters that starts at the match, and name `query_dataset` as the way to read exact rows. A value hit that is not released MUST leave the reader's other results exactly as they would be without value search. Datasets over the bounds, malformed datasets, and Records and Planning descendants SHALL stay excluded.

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
