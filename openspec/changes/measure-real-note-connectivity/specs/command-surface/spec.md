## ADDED Requirements

### Requirement: Remember Documents Provenance And Its Graph Effect
The generated `remember` tool description SHALL document the `sources:` parameter with its
accepted wikilink form, at least one concrete example, and the back-reference mechanic it
drives: for each cited source, the new page's wikilink is appended to that source's
`ingested_into:` frontmatter, maintaining the source-to-note graph and removing the source
from the unprocessed backlog. It SHALL state that provenance is expected for the compiled
types whose frontmatter specification requires it, and that omitting it returns a warning
rather than failing the write.

Because every generated surface derives from one registry entry, this documentation SHALL
appear identically on the MCP tool schema, the REST facade, and the CLI without
duplication, and any change to it SHALL advance the pinned tool-surface fingerprint through
the existing dump-and-verify path rather than being edited into generated artifacts by
hand.

#### Scenario: Provenance parameter is self-describing
- **WHEN** a client reads the generated `remember` input schema
- **THEN** the `sources` parameter description names the wikilink form, gives an example, and states the `ingested_into:` back-reference effect

#### Scenario: Omitted provenance warns without failing
- **WHEN** `remember` writes a compiled type that requires provenance and no sources are supplied
- **THEN** the page is written and the result carries a warning naming the omission
- **AND** no error is raised and no source value is invented

#### Scenario: Description change advances the pinned surface
- **WHEN** the `remember` description or its parameter help changes
- **THEN** the regenerated tool-surface fingerprint differs from the registered fingerprint
- **AND** the connector contract records the new fingerprint as pending refresh rather than claiming registration
