## ADDED Requirements

### Requirement: A residual scope selects what no other scope selects

A policy MAY declare at most one residual scope: a scope document with `residual: true` and no selector or exclude fields. Membership SHALL assign the residual scope to a page or file exactly when no other scope selects that item and no vocabulary registry binding classifies it. The public instance's registry storage SHALL count as classified with no scope. The residual scope SHALL never select a directory. Otherwise it SHALL be an ordinary scope that rules, grants, release grants, `default_deny`, organisation caps, connector denied sets and private vocabulary instances name by id. Membership SHALL remain a function of the item, its path and the compiled policy. A policy without a residual scope SHALL produce exactly its previous membership. The compile SHALL refuse a residual document with selector or exclude fields, a second residual scope, or a non-boolean `residual`. When the connector boundary protects the residual scope, its pinned document SHALL include `residual`.

#### Scenario: An unclassified page is withheld from an unnamed audience
- **WHEN** a residual scope declares `default_deny` and an audience that no rule names reads a page that no other scope selects
- **THEN** the page is answered as a missing page, and the owner receives it in full

#### Scenario: Classification moves a page out of the residual scope
- **WHEN** the owner adds a scope that selects a residual page
- **THEN** the next decision for that page uses the new scope and not the residual scope

#### Scenario: A fenced connector and an opted-in connector
- **WHEN** one client's denied set lists the residual scope and another client's does not
- **THEN** the first receives no unclassified page from any read, search, recall or recent-context door, and the second receives them

#### Scenario: Vocabulary and folders stay available
- **WHEN** a fenced connector interprets pages or browses a folder that holds pages it may see
- **THEN** public vocabulary overlays and history are admitted, and the folder is listed

#### Scenario: A vault without a residual scope
- **WHEN** a policy declares no residual scope
- **THEN** scope membership is unchanged
