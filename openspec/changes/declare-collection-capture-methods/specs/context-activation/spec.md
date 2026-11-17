## ADDED Requirements

### Requirement: A routed collection serves its declared capture method

When a `task_context` lead is a collection manifest that declares `capture.method`, the system SHALL project the declared page as a method of the same packet, under that page's own release decision for the caller, exactly as it releases a lead. It SHALL use the existing method kinds: a skill page as a skill item, a page that lists methods as those methods, and any other page as one page-level item with applicability `read_required` whose read route is the page's stable reference. The item SHALL carry the declared page's own source version, so that a revision, a retirement or a withholding of that page resets the method a client holds at the next activation through the existing generic reset, whether or not the collection still leads. The declaration SHALL be read from the manifest as released to the caller, so a caller who cannot read the manifest receives no declared method. A declared page the caller may not read SHALL be omitted exactly as a declaration naming no page, and the guarded release of the packet SHALL recheck it. Each declaring lead SHALL cost one reference lookup and one source projection and no vault scan; a lead that declares nothing SHALL cost nothing. A page that is both a lead and a declared method SHALL be served once. The page's approval verdict SHALL apply as it does to any method. Reading the method SHALL NOT grant authority to act on it.

#### Scenario: A declared plain page is served as a method to read
- **WHEN** a turn routes to a collection whose manifest declares a page with no method units
- **THEN** the packet carries one method item for that page with applicability `read_required` and the page's stable reference as its read route

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

#### Scenario: Resolution costs one lookup per declaring lead
- **WHEN** a turn routes to one collection that declares a method, and to another that declares none
- **THEN** the declared reference is resolved once and the undeclaring collection costs no resolution
