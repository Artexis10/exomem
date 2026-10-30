## ADDED Requirements

### Requirement: Structural creations may commit as one batch

The semantic writer SHALL commit several structural creations as one canonical batch when each creation carries its own draft token and needs no relation review. A structural creation is one whose status class makes the shared semantic-unit predicate false. The batch SHALL refuse a creation that requires the full relation review, that names a predecessor, or that binds a vocabulary entry, and SHALL write nothing when it refuses.

Inside one mutation guard, the batch SHALL check each note's draft token and Source closure and plan its Source back-references. It SHALL refuse two creations that plan the same file. It SHALL evaluate each note's contract against the base corpus together with every candidate of the batch. It SHALL write every page, back-reference and shared index or log update in one atomic batch, and a failure SHALL leave none of them. The vocabulary writer gate SHALL validate one sealed derived-write manifest per page against the complete batch, and SHALL refuse a derived write that two manifests claim.

#### Scenario: A chunk of draft notes commits once

- **WHEN** the semantic writer commits three structural creations as one batch
- **THEN** each creation reports its own written paths and the index and log hold one entry for each note

#### Scenario: Two creations plan one file

- **WHEN** a batch holds two creations that plan the same file
- **THEN** the batch refuses before it writes and the caller commits each creation alone

#### Scenario: A failure while installing the batch

- **WHEN** the atomic write fails after it installed some pages of the batch
- **THEN** no page of the batch remains

#### Scenario: Two manifests claim one derived write

- **WHEN** two sealed manifests of one batch claim the same derived path
- **THEN** the vocabulary gate refuses the batch
