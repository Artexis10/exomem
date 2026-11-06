## MODIFIED Requirements

### Requirement: The envelope teaching closes the hookless quiet loop

The served contract SHALL teach a hookless client how to route a
plain-language request to stop a kind of suggestion: the registered-family
vocabulary is discoverable from the served surfaces, the mapping from the
user's words to a registered family is the agent's judgment, and the resulting
decision lands through the existing family-disposition surface. The
server-side half of the loop — the quiet persisting with reason and origin
across sessions and engine restarts, inspection, and reset — is the
family-disposition requirement of the attention queue and SHALL hold unchanged
with the envelope present; the envelope's class dispositions SHALL be
unaffected by any family decision.

#### Scenario: A taught mapping quiets a real family and the envelope stands

- **WHEN** a hookless session, following the served contract, quiets a
  registered review family (for example `note_cohesion`) with a reason, a new
  session starts, and the dispositions view is read
- **THEN** that family is `quiet` with the recorded reason and origin, other
  families are untouched, every envelope class disposition is unchanged, and a
  reset restores `normal`

#### Scenario: The vocabulary is discoverable without hooks

- **WHEN** the hookless custom-instructions block and compact bootstrap are
  generated
- **THEN** each names the family-disposition surface and how to list the
  registered families, rather than hardcoding a family table
