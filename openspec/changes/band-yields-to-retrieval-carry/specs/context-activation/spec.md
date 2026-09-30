## ADDED Requirements

### Requirement: A band resolution on an ordinary word yields to a different carried page
A turn's band resolution is yieldable when every anchor the turn resolved carries
`vector_band`, would be at most `partial` without it, and was reached through a name
word the turn wrote in lower case while its casing marks names elsewhere in the turn.
A name word the turn capitalised away from a sentence start, or wrote in a script
with no case, names the anchor, and that resolution SHALL NOT yield. For a yieldable
turn that is not referential and names no agent-chosen anchor, the operation SHALL ask
the retrieval carry for the pages the turn names. When the carry names exactly one
dominant page that is not one of the band-resolved pages, and a carried packet can be
built from that page, the operation SHALL serve that carried packet, the one the same
turn gets with semantic evidence off. In every other case — the carry names one of
the band-resolved pages, several pages or none, cannot run, or its page yields no
carried packet — the band's resolution SHALL stand, so a yield never leaves the turn
with nothing. A turn with any anchor resolved without `vector_band` SHALL NOT ask the
carry for this purpose, and the rule SHALL NOT change which evidence kinds resolve an
anchor.

#### Scenario: A common noun plus the band yields to the page the carry names
- **WHEN** a cased turn about unit conversion writes a rare word from an equipment
  page's name in lower case ("convert the grill's target temperature"), the page
  resolves only on `rare_term` + `vector_band`, and the retrieval carry names a
  temperature-conversions note as the one dominant page
- **THEN** the equipment page is not `resolved`, the packet carries the note at
  `retrieval_carried` with `generation.carried_by = "retrieval"`, and the packet is
  the one the same turn gets with semantic evidence off

#### Scenario: A rare name is not vetoed by an incidental phrase
- **WHEN** a German or Russian turn names an anchor by a capitalised rare name
  ("Quillmere", "Pellimore"), the turn clears the band against it, and an unrelated
  ordinary note shares a phrase with the turn that the retrieval carry names
- **THEN** the anchor resolves on `rare_term` + `vector_band`, the carry is not asked,
  and with semantic evidence off the anchor is not `resolved`

#### Scenario: A name in a script with no case is not vetoed
- **WHEN** a Japanese turn names an anchor inside a compound ("白樺小屋") and the turn
  clears the band against it
- **THEN** the anchor resolves on `rare_term` + `vector_band` whatever other page
  shares words with the turn

#### Scenario: A carried page that yields nothing does not cost the band's page
- **WHEN** a yieldable turn's retrieval carry names one different page, but the carry's
  lanes read nothing off that page
- **THEN** the band-resolved page stays `resolved` and the packet is built from it

#### Scenario: The carry names the band's own page, several pages or none
- **WHEN** a yieldable turn's retrieval carry names the page the band resolved, two or
  more pages, or none
- **THEN** the band's resolution stands

#### Scenario: An anchor resolved on its own words is not yieldable
- **WHEN** a turn resolves one anchor on `exact_alias` and another on `rare_term` +
  `vector_band`
- **THEN** the carry is not asked for this rule and both anchors keep their status
