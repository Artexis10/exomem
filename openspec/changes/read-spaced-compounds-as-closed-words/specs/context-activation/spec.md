# Spec Delta

## ADDED Requirements

### Requirement: A spaced closed compound reads as its closed word
The resolver SHALL join each pair of adjacent non-stopword tokens in one clause into a turn term. A join SHALL never be a turn phrase, so it SHALL never earn `exact_alias` on its own. When a join equals an authored name term of an indexed anchor, the resolver SHALL treat the two tokens as that word: neither token SHALL earn `exact_alias`, `lexical_overlap` or `rare_term` for any anchor through that occurrence. A token that the turn also writes outside such a join SHALL keep its own evidence. The audience decision for a non-owner reader SHALL read the same turn terms, joins included, as candidate matching.

#### Scenario: A compound written with a space does not name an anchor keyed on its half
- **WHEN** a turn writes "home work tracker" and the vault holds a project named "Home" and an anchor named "Homework Tracker"
- **THEN** the project is not a candidate and the tracker resolves

#### Scenario: A half written on its own keeps its anchor
- **WHEN** a turn writes "home" on its own and also writes "home work"
- **THEN** the project named "Home" keeps `exact_alias`

#### Scenario: A withheld anchor reached only through a join is decided
- **WHEN** a non-owner reader's turn reaches a withheld anchor only through a join
- **THEN** the audience decision withholds that anchor
