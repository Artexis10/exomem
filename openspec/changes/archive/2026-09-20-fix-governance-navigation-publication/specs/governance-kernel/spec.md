## ADDED Requirements

### Requirement: Navigation participates in canonical catalog publication

Governance migration SHALL include otherwise eligible navigation and log Markdown in
the initial immutable catalog. Ordinary retrieval's navigation filtering SHALL NOT
determine canonical catalog membership. A governed capture that updates navigation
SHALL publish those updates and its primary content in the same successor catalog.

Projected retrieval SHALL exclude navigation before candidate scoring, graph
admission and reranking, preserving ordinary recall's navigation exclusion. Complete
catalog membership SHALL NOT make navigation eligible as a result, graph seed or
target, scoring passage or continuation candidate. Structural exclusion SHALL NOT
be reported as an authorization denial.

#### Scenario: First capture after hosted enrollment

- **WHEN** a fresh scaffold with navigation pages completes governance enrollment and becomes serving
- **THEN** a valid reviewed capture commits its note and navigation updates in one complete successor catalog
- **AND** the initial catalog and its digest remain unchanged

#### Scenario: Public navigation names a restricted page

- **WHEN** an authorized navigation page contains a restricted page's name or link
- **THEN** keyword, hybrid and vector recall, including CLIP, graph expansion, reranking and continuation, exclude that navigation before candidate acquisition
- **AND** navigation presence does not change the public result sequence or expose its restricted references

### Requirement: Omitted legacy navigation rows require guarded adoption

When an existing navigation or log page has no row because earlier migration omitted
navigation, a canonical planned-write batch SHALL be able to add its successor row
only while retaining an exact content guard for the existing canonical file. The
guard SHALL bind the same path and predecessor hash as the planned mutation. This
compatibility path SHALL retain the normal current policy, activation tuple, writer
and measurement-closure checks. It SHALL NOT rewrite a historical catalog or bypass
the canonical file guard. Raw catalog mutations SHALL NOT acquire this permission.

An existing catalog row with a different predecessor, a non-navigation file without
a predecessor row, or navigation without the required exact guard SHALL remain a
refused mutation. An observed canonical file change before commit SHALL refuse the
batch without publishing a new active catalog or overwriting that file.

#### Scenario: Existing migrated vault repairs omitted navigation during capture

- **WHEN** a valid capture updates an exactly guarded navigation page absent from an older catalog
- **THEN** the normal successor includes that page and the new note with complete required projection lanes
- **AND** canonical predecessor checks remain active throughout commit

#### Scenario: Compatibility cannot adopt arbitrary missing content

- **WHEN** a non-navigation predecessor is absent, an existing catalog predecessor differs, or a raw catalog mutation requests navigation adoption
- **THEN** publication refuses before committing canonical bytes or changing the active tuple

#### Scenario: Navigation changes after preparation

- **WHEN** a navigation file changes after its exact guarded write was prepared
- **THEN** commit refuses without overwriting the changed file or activating the prepared catalog
