## ADDED Requirements

### Requirement: Working-set injection renders stance and central moves

In working-set mode, an admitted ready stance block SHALL render under the existing data header with whole lines and refs. Mark a passed review date as `review due` and show nonzero overflow. Render a stance generation once per session, then one pointer line until its generation changes. All output SHALL obey the existing render ceiling and failure fallback.

When `next_action` exists, render its `say` once after the data block. Otherwise preserve supported older-service recovery through the generated fallback from read-next-action. The hook SHALL keep no independently authored per-state text. A supplied move and its fallback SHALL never both render.

#### Scenario: A changed stance generation renders again

- **WHEN** two prompts have the same stance generation and a third has a new generation
- **THEN** the hook renders the block, then its pointer, then the changed block

#### Scenario: The supplied move replaces its fallback

- **WHEN** an ambiguous anchors packet carries `next_action`
- **THEN** the hook renders the supplied move once and adds no fallback anchor line

#### Scenario: An older related-pages response remains useful

- **WHEN** a supported older service returns the related-pages condition without `next_action`
- **THEN** the generated fallback retains the existing related-pages read instruction
