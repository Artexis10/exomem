## ADDED Requirements

### Requirement: Error Payloads Cross The Same Terminal Boundary

The terminal filter at the shared dispatcher is the last thing between a command result and
the wire, and an error is a result. Every payload leaving a content-returning command at the
shared dispatcher SHALL pass the same terminal filtering as a successful return value,
whether the command returned or raised. An error raised inside a governed command MUST NOT
reach a caller carrying a vault path, title, or reference that the caller is not permitted
to see.

Errors that name colliding or ambiguous stored items SHALL carry a content-free code and
counts. The identifying detail an owner needs to repair the collision SHALL be reachable
only through a surface that resolves an audience and applies a disclosure decision.

#### Scenario: an identity collision does not name the colliding pages

- **WHEN** a caller resolves an identifier that matches more than one stored item
- **THEN** the error carries the ambiguity code and the number of matches
- **AND** the error carries no vault path, title, or reference of any match

#### Scenario: a raised error is filtered like a returned result

- **WHEN** a governed content-returning command raises an error whose payload names a vault
  item withheld from the caller
- **THEN** the reference is removed before the error crosses the dispatcher boundary
- **AND** the caller cannot distinguish the withheld item from one that does not exist

#### Scenario: an ungoverned vault keeps its error text

- **WHEN** a vault has no governance configured and a command raises
- **THEN** the error text is unchanged apart from the always-on secret scrubbing that
  already applies

### Requirement: Reverse Provenance Is Stripped Below Full Release

Provenance runs in both directions: a compiled item records what it cites, and a cited item
records what compiled from it. Both directions name items that may be withheld. The
frontmatter provenance fields stripped below full release SHALL include the reverse
citation field that records which compiled items ingested a source, alongside the forward
citation, evidence, supersession and parent-media fields already covered.

This requirement is scoped to the PROJECTED representation of a page — its parsed
frontmatter and the fields the projector emits. It deliberately does not claim coverage of
a surface that returns the raw stored bytes on request: such a surface re-serialises the
original file, so it reproduces every provenance field and is bounded by no disclosure
level. That surface is a separate, pre-existing gap and is not closed by this change;
stating the guarantee narrowly keeps the archived spec true rather than recording a
promise the code does not keep.

#### Scenario: a released source does not enumerate the compiled items that cited it

- **WHEN** a source item is released to an audience below full level and a compiled item
  withheld from that audience cites it
- **THEN** the reverse citation field is absent from the released representation
- **AND** the withheld compiled item's path, title and reference do not appear anywhere in
  the response

#### Scenario: full release to a permitted audience is unchanged

- **WHEN** the same source item is released at full level to a permitted audience
- **THEN** the reverse citation field is present and complete

#### Scenario: the guarantee is stated over the projected representation

- **WHEN** a caller requests the raw stored bytes of a page alongside its projection
- **THEN** the projected representation still omits the reverse citation field
- **AND** the raw-bytes surface is out of scope for this requirement and is governed
  separately

### Requirement: Operational Run State Is Not Released As Knowledge

Durable run state for governed multi-step operations records the paths, targets and content
hashes of items whose individual disclosure decisions may be restrictive. It is operational
state, not knowledge, and it SHALL NOT be indexed into the content corpus or surfaced by
recall. It SHALL remain reachable to the owner through the governed command that owns the
run.

Text released from any surface MUST NOT embed a machine-readable enumeration of item paths,
targets, or content hashes. Where a run records a summary in released text, that summary
SHALL carry counts and the run reference only, with per-item detail confined to the run
object behind the governed command.

#### Scenario: run state does not appear in recall

- **WHEN** a run has recorded state naming items in the vault and a caller issues a recall
  query whose terms match that state
- **THEN** no run-state item is returned
- **AND** the result counts are the same as for a vault with no run present

#### Scenario: a released run summary carries no per-item detail

- **WHEN** a run records a summary into a released page
- **THEN** the summary carries counts and the run reference
- **AND** it carries no source path, target path, or content hash of any individual item

#### Scenario: the owner still reads full run detail through the governed command

- **WHEN** the owner requests the status of a run through the command that owns it
- **THEN** the full per-item detail is returned
- **AND** the response is subject to the same disclosure decision as any other governed
  content-returning result

### Requirement: An Error Names Back Only What The Caller Supplied

Filtering an error works by replacing a withheld reference with a marker. That is correct
where the marker sits inside content the caller was already permitted to receive, and
wrong where the error's payload IS a reference the caller just sent: the replacement then
distinguishes an item that exists and is restricted from one that does not exist, which is
the existence oracle the boundary exists to prevent.

A reference the caller supplied in the request SHALL therefore be echoed back unchanged,
whether or not it names an existing item and whether or not that item is withheld. A
reference the caller did not supply SHALL be redacted. An error's text SHALL remain a pure
function of the caller's own input.

#### Scenario: the same requested path reads identically whether or not it exists

- **WHEN** a caller requests a path that exists but is withheld from them, and later
  requests the same path after it has been removed
- **THEN** both errors are byte-identical
- **AND** neither reveals whether the item existed

#### Scenario: a reference the caller never named is redacted

- **WHEN** an error names a stored item that the caller did not supply in the request, such
  as a second candidate for an ambiguous identifier
- **THEN** that reference is replaced before the error crosses the boundary
- **AND** two different unsupplied references redact to the same text
