## ADDED Requirements

### Requirement: A band-decided resolution yields to a different carried page
A turn is band-decided when every anchor it resolved carries `vector_band` and would
be at most `partial` without it. For a band-decided turn that is not referential and
names no agent-chosen anchor, the operation SHALL ask the retrieval carry for the
pages the turn names, as it would for an `unresolved` turn. When the carry names
exactly one dominant page and that page is not one of the band-resolved pages, the
band-resolved anchors SHALL be held at `partial`, the turn SHALL be `unresolved`, and
the turn SHALL then be carried as it would be with semantic evidence off. When the
carry names one of the band-resolved pages, several pages or none, or cannot run, the
band's resolution SHALL stand. A turn with any anchor resolved without `vector_band`
SHALL NOT ask the carry for this purpose, and the rule SHALL NOT change which evidence
kinds resolve an anchor.

#### Scenario: A rare word plus the band yields to the page the carry names
- **WHEN** a turn about unit conversion names a rare word from an equipment page's
  name, the page resolves only on `rare_term` + `vector_band`, and the retrieval carry
  names a different page, a temperature-conversions note, as the one dominant page
- **THEN** the equipment page is not `resolved`, the packet carries the note at
  `retrieval_carried` with `generation.carried_by = "retrieval"`, and the packet is
  the one the same turn gets with semantic evidence off

#### Scenario: The carry names the band's own page
- **WHEN** a band-decided turn's retrieval carry names the same page the band resolved
- **THEN** the page stays `resolved` and the packet is built from it as before

#### Scenario: The carry names several pages or none
- **WHEN** a band-decided turn's retrieval carry names two or more pages, or none
- **THEN** the band's resolution stands

#### Scenario: A rare name plus the band still resolves in another language
- **WHEN** a German, Russian or Japanese turn names an anchor by a rare name and the
  turn clears the band against it, and no carry names a different page
- **THEN** the anchor resolves on `rare_term` + `vector_band`, and with semantic
  evidence off it stays `partial`

#### Scenario: An anchor resolved on its own words is not band-decided
- **WHEN** a turn resolves one anchor on `exact_alias` and another on `rare_term` +
  `vector_band`
- **THEN** the carry is not asked for this rule and both anchors keep their status
