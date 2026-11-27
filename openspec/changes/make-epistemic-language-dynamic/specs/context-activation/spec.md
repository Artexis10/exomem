## ADDED Requirements

### Requirement: Current-turn subject precedence
Recall SHALL retain a subject whose exact name appears in the current turn when unrelated unnamed weak alternatives would cause ambiguity. It SHALL retain those alternatives as partial leads. It SHALL check competing interpretations within each group of overlapping current-turn names independently. Focus-only names SHALL NOT activate this precedence. Categorical anchor resolution SHALL apply its `ambiguous` rule after this precedence.

#### Scenario: One explicit subject with an unrelated weak alternative
- **WHEN** the current turn names one resolved subject exactly and an unrelated weak match would compete with it
- **THEN** recall serves the named subject and retains the weak match as a partial lead

#### Scenario: Two separately named subjects remain available
- **WHEN** the current turn names two subjects with disjoint names
- **THEN** recall carries both subjects without treating their distinct names as competing senses

#### Scenario: A second topic does not hide same-name ambiguity
- **WHEN** one current-turn name resolves disconnected alternatives and the turn also names another topic
- **THEN** recall reports the first name's ambiguity even if cross-topic links connect the combined candidates

#### Scenario: Weak interpretations of a spoken name still compete
- **WHEN** a weaker candidate's current-turn name contact overlaps the exact subject name and represents another disconnected interpretation
- **THEN** recall preserves that ambiguity instead of treating the weaker interpretation as an unrelated task-word match

#### Scenario: Complementary weak context remains resolved
- **WHEN** a weak candidate is structurally complementary to the explicitly named subject
- **THEN** subject precedence does not demote that context solely because it lacks an exact name

#### Scenario: Focus does not impersonate the current turn
- **WHEN** only conversation focus supplies an exact name or adds an unspoken alias
- **THEN** recall does not treat that name or alias as current-turn spelling

#### Scenario: Weak-only and identity-selection behavior remains
- **WHEN** the current turn names no exact subject or an explicit identity choice selects the subject
- **THEN** recall preserves the existing weak-only ambiguity and identity-selection behavior
