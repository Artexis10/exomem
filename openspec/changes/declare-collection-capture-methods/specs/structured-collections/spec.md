## ADDED Requirements

### Requirement: A collection manifest may declare one capture method

A collection manifest MAY carry an optional `capture` block with one key, `method`: a non-empty string of at most 512 characters naming one vault page by wiki link or Exomem reference. The page MAY be of any kind, such as a procedure, a pattern, a skill page or a note. The system SHALL validate the block with the manifest and round-trip it unchanged through inspection and `describe`. An unknown key, a block that is not a mapping, and a missing, empty, non-string or oversized `method` SHALL refuse with `INVALID_COLLECTION_CAPTURE` naming the key. Validation SHALL NOT require the page to exist.

The system SHALL resolve the reference only when a route or an inspection needs it, for the caller asking, through the existing link resolver confined to the vault. A reference that names no page, that leaves the vault or that names a reserved path SHALL resolve to nothing. A page the caller may not read SHALL resolve to nothing as well, and nothing in an answer SHALL tell the two cases apart. A resolved method SHALL carry the page's stable reference where it holds an identity, its path otherwise, and its title without link markup, bounded to 160 characters. Inspection of the collection SHALL report one `CAPTURE_METHOD_UNAVAILABLE` finding when a declared method resolves to nothing for the caller, with one reason for both cases. Routing, validation and writes SHALL NOT depend on the resolution, and a failure to resolve SHALL cost only the method.

#### Scenario: A declared capture method round-trips
- **WHEN** a manifest declares `capture: {method: "[[Dock capture method]]"}`
- **THEN** validation accepts it, inspection returns the block unchanged, and `describe` documents its key and bound

#### Scenario: A malformed capture block refuses
- **WHEN** `capture` is a list, names an unknown key, omits `method`, or gives `method` as an empty string, a number or a string over 512 characters
- **THEN** manifest validation refuses with `INVALID_COLLECTION_CAPTURE` naming the offending key

#### Scenario: A reference resolves to the page's stable identity
- **WHEN** the declared reference is a wiki link by title, with an alias or an anchor, an Exomem reference or a vault path, naming a page that holds an identity
- **THEN** each form resolves to the same method, carrying that page's stable reference and its title

#### Scenario: A missing and a withheld page are indistinguishable
- **WHEN** one manifest declares a page that does not exist and another declares a page that governance withholds from the caller
- **THEN** both resolve to nothing, inspection reports the same `CAPTURE_METHOD_UNAVAILABLE` finding for both, and no answer names the withheld page

#### Scenario: A resolved method leaves inspection quiet
- **WHEN** the declared page exists and the caller may read it
- **THEN** inspection reports no capture finding

#### Scenario: A title carries no link and stays bounded
- **WHEN** the declared page's title contains wiki-link markup or exceeds 160 characters
- **THEN** the resolved title carries the link's text without markup and at most 160 characters
