# Spec Delta

## ADDED Requirements

### Requirement: A spaced closed compound reads as its closed word
The resolver SHALL join each pair of adjacent non-stopword tokens in one clause into a turn term, unless exactly one of the two tokens is capitalised inside a sentence. A join SHALL never be a turn phrase, so it SHALL never earn `exact_alias` on its own. When a join equals a name term of any indexed anchor the caller can see, whether or not the turn reached that anchor, the resolver SHALL treat the two tokens as that word: neither token SHALL, as a single word, earn `exact_alias`, `lexical_overlap` or `rare_term` for any anchor through that occurrence. A longer phrase that contains the tokens SHALL keep its evidence. A token that the turn also writes outside such a join SHALL keep its own evidence. The same reading SHALL apply to earlier conversation entries. A carried page's title SHALL consume halves against the title's own words. The audience decision for a non-owner reader SHALL read the same turn terms, joins included, as candidate matching, so its name-term counts leave out withheld owners of a join.

#### Scenario: A compound written with a space does not name an anchor keyed on its half
- **WHEN** a turn writes "home work tracker" and the vault holds a project named "Home" and an anchor named "Homework Tracker"
- **THEN** the project is not a candidate and the tracker resolves

#### Scenario: A half written on its own keeps its anchor
- **WHEN** a turn writes "home" on its own and also writes "home work"
- **THEN** the project named "Home" keeps `exact_alias`

#### Scenario: A capitalised name beside a compound half keeps its anchor
- **WHEN** a turn writes "the book Mark lent me" and the vault holds a person named "Mark" and a project named "Bookmark Sync"
- **THEN** the person keeps `exact_alias`

#### Scenario: An earlier entry's compound is read against the whole catalogue
- **WHEN** an earlier user entry writes "home work tracker" and the current turn reaches only the project named "Home"
- **THEN** the entry does not name the project

#### Scenario: A withheld anchor reached only through a join is decided
- **WHEN** a non-owner reader's turn reaches a withheld anchor only through a join
- **THEN** the audience decision withholds that anchor and leaves it out of the join's name-term count
