## ADDED Requirements

### Requirement: A routed collection serves its declared capture method

When a `task_context` lead is a collection manifest that declares `capture.method`, the system SHALL project the declared page as a method of the same packet, under that page's own release decision for the caller, exactly as it releases a lead. It SHALL use the existing method kinds: a skill page as a skill item, a page that lists methods as those methods, and any other page as one page-level item with applicability `read_required` whose `read_ref` is the page's vault path, as for any served method until its approval verdict is `approved`, which replaces the path with the stable reference. The item SHALL carry the declared page's own source version, so that a revision, a retirement or a withholding of that page resets the method a client holds at the next activation through the existing generic reset, whether or not the collection still leads. The declaration SHALL be read from the manifest as released to the caller, so a caller who cannot read the manifest receives no declared method. A declared page the caller may not read SHALL be omitted exactly as a declaration naming no page, and the guarded release of the packet SHALL recheck it. Each declaring lead SHALL cost at most one source projection, and a lead that declares nothing SHALL cost nothing. A path or an Exomem reference SHALL need no link resolver. A wiki-link title, stem or short page id SHALL resolve only through the broad resolver when it is already resident and SHALL NOT build it on the turn: on a cold resolver the method is omitted for that turn, the block reports the missing reason `lane_failed`, and the build is scheduled in the background when a watcher owner can keep it current. A failure on the declared page SHALL cost the method and never the task context, and the deadline SHALL be checked before each lookup. A page that is both a lead and a declared method SHALL be served once. The page's approval verdict SHALL apply as it does to any method. Reading the method SHALL NOT grant authority to act on it.

#### Scenario: A declared plain page is served as a method to read
- **WHEN** a turn routes to a collection whose manifest declares a page with no method units
- **THEN** the packet carries one method item for that page with applicability `read_required` and the page's vault path as its `read_ref`

#### Scenario: A declared skill page is served as a skill
- **WHEN** the declared page is a skill page
- **THEN** the item has the skill kind, the page's name and its description

#### Scenario: A revised method page resets the held method
- **WHEN** a client holds the served method and the declared page is then edited
- **THEN** the next activation resets the held methods and serves the current page

#### Scenario: A withheld method page reaches no one who may not read it
- **WHEN** the declared page is withheld from the caller, and when it simply does not exist
- **THEN** the packet carries no method item for it and the two packets are identical

#### Scenario: A lead and its declared page are served once
- **WHEN** the declared page is also a lead of the same turn
- **THEN** the packet carries its method item once

#### Scenario: A turn never builds the link resolver
- **WHEN** a turn routes to a collection that declares its method by title or by short page id while the broad resolver is cold, and a collection that declares none
- **THEN** the turn builds no resolver, omits the declared method, serves the rest with the block `partial` and the missing reason `lane_failed`, and serves the method on a later turn once the resolver is resident

#### Scenario: A path or reference declaration needs no resolver
- **WHEN** the declaration is a vault path or an Exomem reference and the resolver is cold
- **THEN** the packet carries the method and the turn builds no resolver

#### Scenario: A failure on the method page costs only the method
- **WHEN** projecting the declared page fails transiently
- **THEN** the packet is served with its leads and without the method
