# command-surface

## ADDED Requirements

### Requirement: Relation Filters Are Exposed Consistently Across Surfaces

`find` and `ask_memory` SHALL expose `relations`, `relation_of`, and
`relation_direction` with docstring-derived schemas projected identically into
MCP, REST, CLI, and OpenAPI surfaces, and the generated tool-surface fixtures
SHALL be regenerated in the same change. Bootstrap search guidance SHALL
mention relation filtering in one bounded sentence. The relation vocabulary
lock (`tests/golden/relation_compatibility.yaml`) MUST NOT change. Error
envelopes SHALL reuse the existing `RETRIEVAL_INDEX_WARMING` mapping and a
typed `INVALID_RELATION_FILTER` validation error.

#### Scenario: Tool schemas carry the new parameters

- **WHEN** tool schemas are regenerated after the change
- **THEN** `find` and `ask_memory` schemas include the three relation parameters with descriptions, and schema-fidelity gates pass

#### Scenario: Vocabulary lock untouched

- **WHEN** the change lands
- **THEN** `tests/golden/relation_compatibility.yaml` is byte-identical to before
