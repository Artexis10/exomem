## ADDED Requirements

### Requirement: Ownership review is a review family judged by the connected agent

The audit registry SHALL add the category `ownership_review`. Its review items SHALL have the standard reference, fingerprint, dismiss, snooze and reopen semantics.

A page SHALL be eligible when its note type has role `compiled`, its lifecycle status is live, it lies in a read-write tree, it is recall-eligible, it carries a project key, and it does not carry a breadth tag (exactly `hub` or `snapshot`). Sources, Evidence, superseded, archived, dropped and draft pages SHALL NOT be asked.

The item's detail SHALL ask whether the page belongs in its current project. It SHALL state the ownership test and the deferral rule from the guidance routes, and it SHALL spell out the call that keeps the page where it is. It SHALL NOT claim that the page is misfiled, and SHALL carry no score.

The item's identity SHALL be the page and the family. Its signal version SHALL be the page's project key, home folder and title, so that a decision binds to them. A dismissal SHALL hold while all three stay the same, and the item SHALL reopen with a new fingerprint when any of them changes. A change to the page's body alone SHALL NOT reopen it.

#### Scenario: A page created in a project opens an item

- **WHEN** an agent creates a compiled page with the project key `project-alpha`
- **THEN** one open `ownership_review` item exists for that page
- **AND** its detail asks whether the page belongs in `project-alpha` and states the ownership test

#### Scenario: A dismissal holds while the page grows

- **WHEN** the item of a page in `project-alpha` is dismissed with an `intentional:` reason, and the page later gains three sections
- **THEN** the item stays dismissed

#### Scenario: A project change reopens the item

- **WHEN** the item of a page is dismissed, and the page's project key later changes from `project-alpha` to `project-beta`
- **THEN** the item is open again with a new fingerprint

#### Scenario: Hubs and Sources are never asked

- **WHEN** a compiled page carries the `hub` tag, or a Source page carries a project key
- **THEN** no `ownership_review` item exists for that page

### Requirement: A bounded rotation asks every eligible page in time

The due-state projection SHALL open `ownership_review` items for at most `rotation_daily` never-asked eligible pages each day, with a default of 3. Within each project it SHALL order pages by the cosine distance between the page's mean semantic-unit vector and the mean of the project's other pages, farthest first, and it SHALL take projects in turn. A project with fewer than three eligible pages SHALL order its pages by authored date. The distance SHALL order the rotation and SHALL NOT exclude any eligible page from it.

The distances SHALL be computed off the write path, in a bulk refresh that reads vectors with the paginated sweep read and refreshes only projects whose membership changed. The projection SHALL report the number of eligible pages never asked, beside the count of open items.

#### Scenario: The farthest page in a project is asked first

- **WHEN** a project holds 40 eligible pages that were never asked, and 6 of them form a group far from the project's centre
- **THEN** the first rotation item for that project is one of those 6 pages

#### Scenario: One large project cannot take the whole daily slice

- **WHEN** one project holds 800 never-asked pages and two other projects hold 30 each
- **THEN** one day's rotation opens items in more than one project

#### Scenario: The write path computes no distance

- **WHEN** a page is created in a project with 500 pages
- **THEN** the write reads no semantic-unit vectors of other pages

### Requirement: Ownership items join the due-state projection as a backlog

The due-state projection SHALL maintain `ownership_review`, and a page write SHALL settle the written page's item in full. Open items SHALL sort after every dated obligation and before `note_cohesion`. Their counts SHALL be exact and never capped.

#### Scenario: A backlog never displaces a due obligation

- **WHEN** 50 `ownership_review` items are open and one prediction window is due
- **THEN** the served top rows list the due prediction before any `ownership_review` row

#### Scenario: Ownership sorts ahead of note cohesion

- **WHEN** both `ownership_review` and `note_cohesion` items are open, and no dated obligation is due
- **THEN** the served top rows list `ownership_review` rows before `note_cohesion` rows

### Requirement: The write response carries an open ownership question

A write to a page whose `ownership_review` item is open SHALL carry a `structure_suggestion` of kind `ownership` with the page's ref, the item's fingerprint and id, and the question with its spelled-out call. The question block SHALL stay within 400 tokens. A page whose item is dismissed or absent SHALL carry no ownership suggestion. The existing `structural_suggestions` envelope SHALL turn this delivery off.

#### Scenario: An answered page writes quietly

- **WHEN** a page's ownership item is dismissed and the page is edited
- **THEN** the write response carries no `ownership` suggestion

### Requirement: Ownership answers use the existing review and curation surfaces

The served guidance SHALL direct the agent to apply the ownership test to the page. A page that serves its project's goal SHALL be kept where it is by one dismissal with an `intentional:` reason that names that goal. A page with its own goal, institution or schedule SHALL change home only through a curation plan that the user confirms by its exact plan id and fingerprint; a new project SHALL be registered through the existing project-key route, and the related projects SHALL be linked by a typed relation between their notes. When the user already decided the page's home and nothing that bears on ownership has changed, the agent SHALL leave the decision standing.

#### Scenario: Keeping a page where it is needs one call

- **WHEN** an agent judges that a page serves its project's goal
- **THEN** one dismissal with the item's ref, fingerprint and an `intentional:` reason settles the item

#### Scenario: A rehome never happens without the user's confirmation

- **WHEN** an agent judges that a page belongs to a new project
- **THEN** the page changes home only through a curation plan that the user confirms by its exact plan id and fingerprint
