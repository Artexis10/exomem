## ADDED Requirements

### Requirement: Structural creations may commit as one batch

The semantic writer SHALL commit several structural creations as one canonical batch when each creation carries its own draft token and needs no relation review. A structural creation is one whose status class makes the shared semantic-unit predicate false. The batch SHALL refuse a creation that requires the full relation review, that names a predecessor, or that binds a vocabulary entry, and SHALL write nothing when it refuses.

Before the mutation guard, the batch SHALL plan each creation's Source back-references, SHALL refuse two creations that plan the same file, and SHALL judge each creation's contract against the corpus that its preflight read together with every candidate of the batch. Inside one mutation guard, the batch SHALL re-check each creation's Source closure and SHALL admit those verdicts only while every validity stamp is current; otherwise it SHALL judge every candidate again against one fresh corpus. It SHALL write every page, back-reference and shared index or log update in one atomic batch, and a failure that the write catches SHALL leave none of them. The vocabulary writer gate SHALL validate one sealed derived-write manifest per page against the complete batch, and SHALL refuse a derived write that two manifests claim.

The batch SHALL raise only while nothing of it is on disk. Once the batch is written it SHALL NOT raise: each creation SHALL receive its own result, or the error of a step after the write that failed for that creation. A failure after the write that belongs to the whole batch SHALL be the outcome of every creation. A write that fails after its commit point, while the batch's pages hold the bytes that the batch planned, SHALL count as written.

#### Scenario: A chunk of draft notes commits once

- **WHEN** the semantic writer commits three structural creations as one batch
- **THEN** each creation reports its own written paths and the index and log hold one entry for each note

#### Scenario: Two creations plan one file

- **WHEN** a batch holds two creations that plan the same file
- **THEN** the batch refuses before it writes and the caller commits each creation alone

#### Scenario: A commit lands between validation and the batch

- **WHEN** another governed commit lands after the creations were validated and before the batch commits
- **THEN** the batch judges every creation again against one fresh corpus and commits when no verdict blocks

#### Scenario: A step fails after the batch is written

- **WHEN** a step after the write fails for one creation of the batch
- **THEN** every other creation receives its result and that creation receives the error, and the batch does not raise

#### Scenario: A failure while installing the batch

- **WHEN** the atomic write fails after it installed some pages of the batch
- **THEN** no page of the batch remains

#### Scenario: Two manifests claim one derived write

- **WHEN** two sealed manifests of one batch claim the same derived path
- **THEN** the vocabulary gate refuses the batch
