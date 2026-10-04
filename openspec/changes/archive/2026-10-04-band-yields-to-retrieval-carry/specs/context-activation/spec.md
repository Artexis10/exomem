## ADDED Requirements

### Requirement: A band resolution on an ordinary word yields to a different carried page
A turn's band resolution is yieldable when every anchor the turn resolved carries
`vector_band`, would be at most `partial` without it, and was reached through name
words that are all in a cased script and that the turn never writes with an initial
capital, in a turn that carries a casing signal. A turn carries a casing signal when
it writes at least one cased word with an initial capital and at least one cased word
without one; a sentence-initial capital counts. A name word the turn writes with an
initial capital anywhere, sentence start included, or writes in a script with no
case, names the anchor, and that resolution SHALL NOT yield. A turn with no casing
signal (all lower case, all capitals, or every word capitalised) SHALL NOT yield. For a yieldable
turn that is not referential and names no agent-chosen anchor, the operation SHALL ask
the retrieval carry for the pages the turn names. When the carry names exactly one
dominant page that is not one of the band-resolved pages, and a carried packet can be
built from that page, the operation SHALL serve that carried packet, the one the same
turn gets with semantic evidence off. In every other case — the carry names one of
the band-resolved pages, several pages or none, cannot run, or its page yields no
carried packet — the band's resolution SHALL stand outside the explicitly scoped
subject-request exception below. A turn with any anchor resolved without `vector_band` SHALL NOT ask the
carry for this purpose, and the rule SHALL NOT change which evidence kinds resolve an
anchor outside that exception.

For a bounded possessive subject/property request recognized under the existing
context-activation scope contract, activation SHALL evaluate the affected ordinary-word
band candidate locally. If it lacks independent subject contact and meets the existing
name/casing protections, it SHALL remain at most partial before packet branching,
whether or not rival material survives. An independently resolved anchor elsewhere
SHALL retain its status and SHALL NOT restore that weak candidate. Independently
named pages retain their ordinary admission; property-clause-only body contact must
pass the subject-scoped admission predicate. This exception SHALL NOT widen
retrieval-only identity resolution or veto protected multilingual names.

#### Scenario: A scoped conversion request does not substitute another object's facts
- **WHEN** a turn with a casing signal asks to convert one object's target temperature,
  that object's page has only the yieldable ordinary-word band evidence, and another
  object's settings page shares the conversion words without subject admission
- **THEN** the weak equipment candidate is not `resolved`, the unrelated settings and
  their prose pointers are not served, and both evidence arms preserve honest
  partial/unresolved subject status rather than substituting saved facts

#### Scenario: An independent anchor does not restore a weak property candidate
- **WHEN** that scoped request also independently names another project or page
- **THEN** the independently named context survives while the affected ordinary-word
  band candidate remains at most partial

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

#### Scenario: An unscoped carried page that yields nothing does not cost the band's page
- **WHEN** a yieldable turn's retrieval carry names one different page, but the carry's
  lanes read nothing off that page and no recognized subject/property request applies
- **THEN** the band-resolved page stays `resolved` and the packet is built from it

#### Scenario: The carry names the band's own page, several pages or none
- **WHEN** a yieldable turn's retrieval carry names the page the band resolved, two or
  more pages, or none, outside the recognized subject/property exception
- **THEN** the band's resolution stands

#### Scenario: An anchor resolved on its own words is not yieldable
- **WHEN** a turn resolves one anchor on `exact_alias` and another on `rare_term` +
  `vector_band`
- **THEN** the carry is not asked for this rule and both anchors keep their status

#### Scenario: Known limit: a turn with no casing signal never yields
- **WHEN** the same unit-conversion turn is typed entirely in lower case ("can you
  convert the grill's target temperature from fahrenheit to celsius?")
- **THEN** the turn carries no casing signal, the band's resolution is not yieldable,
  and the equipment page resolves on `rare_term` + `vector_band` exactly as it did
  before this requirement
