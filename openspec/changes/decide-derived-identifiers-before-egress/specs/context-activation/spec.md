## MODIFIED Requirements

### Requirement: Bounded role lanes and the working-memory packet
For each resolved anchor the operation SHALL select context roles per the
`context-roles` capability and run one bounded lane per selected role over existing
primitives only: semantic units filtered by category within the anchor neighbourhood,
Records collection items for collections claiming the anchor, active Planning items
linked to the anchor, entity or profile page facets, `graph_context` under a
traversal profile from resolved anchors at depth at most 2 (depth 1 from `partial`
anchors), and evidence pointers. The packet SHALL contain `anchors[]` (ref, title,
kind, status, evidence), `roles[]`, `units[]` (ref, role, text of at most 360
characters cut only at a boundary that leaves no unclosed wikilink, lifecycle,
updated, provenance), `pointers[]` (ref, title, why), `current_state[]` (anchor,
source, as_of, `statement` of at most 200 characters stating the observed status in
the source's own words), `missing[]` (role, reason), `ambiguity[]`,
`budget {limit_chars, used_chars}`, `generation {index_generation, roles_hash}` (and,
for the owner, `freshness_key`, which counts and digests every file in the vault and is
therefore not returned to another audience) and `abstained`. `used_chars` SHALL count every prose field of the
packet (unit text, `current_state[].statement`, pointer title and why) and SHALL
never exceed `max_chars`. Units SHALL be emitted before pages, pages beyond the
budget SHALL become pointers, and a superseded unit SHALL be marked `superseded`
with its active successor named rather than presented as current. A lane that
reaches its read limit before exhausting the anchor neighbourhood SHALL report
`missing[] {role, reason: "lane_truncated"}`, and a lane whose items fit neither as
units nor as pointers within `max_chars` SHALL report `missing[] {role, reason:
"budget"}`; an item the egress guard removes because it names material released to
the caller at a notice level SHALL be reported as `missing[] {role, reason:
"withheld"}` once per affected section without naming the item, where `role` holds the
packet section name (`anchors`, `units`, `pointers`, `current_state`) rather than a
context role. Material withheld at L0 SHALL be omitted exactly as absent material is:
no marker, and a packet whose every anchor it removed abstains as `unresolved`. The packet SHALL NOT carry the
`due_state` block and SHALL NOT read or advance the due-state emission ledger; recall
remains the only `due_state` carrier.

#### Scenario: Budget holds under a large neighbourhood
- **WHEN** the selected lanes yield more candidate text than `max_chars`
- **THEN** the packet's `used_chars` is at most `max_chars`, units are kept in role
  priority order, and the overflow is represented only as pointers

#### Scenario: Superseded knowledge is marked
- **WHEN** a role lane yields a unit whose page carries `status: superseded` and a
  `superseded_by` target
- **THEN** the unit either is omitted in favour of the successor's unit or appears with
  `lifecycle: superseded` and `provenance.superseded_by` set; it is never emitted as
  active

#### Scenario: Current state comes from Records first
- **WHEN** a resolved resource anchor is claimed by a Records collection whose latest
  item states the resource's status
- **THEN** `current_state[]` carries that status as `statement` with `source: records`
  and the item's `observed_on`, and the `current_state` role lane does not substitute
  an older prose note for it

#### Scenario: A truncated lane is reported, not hidden
- **WHEN** a role lane's read limit is reached while in-neighbourhood units remain
- **THEN** `missing[]` carries `{role, reason: "lane_truncated"}` for that lane

#### Scenario: Activation never consumes the due-state emission
- **WHEN** `activate_context` is called and `ask_memory` is then called on the same
  vault in the same process with a due-state block pending
- **THEN** the `ask_memory` response carries the same `due_state` block it would have
  carried had `activate_context` not been called

#### Scenario: Abstained packet injects nothing
- **WHEN** the turn is `unresolved`
- **THEN** `units`, `pointers` and `current_state` are empty and `budget.used_chars`
  is 0

#### Scenario: L0 material is omitted as absent material is
- **WHEN** the egress guard removes an item that names a page withheld from the
  caller at L0
- **THEN** `missing[]` carries no `withheld` marker for it, and a packet whose every
  anchor was removed abstains with `abstention.reason = "unresolved"`

## ADDED Requirements

### Requirement: Activation resolves a turn over the caller's view

For a caller other than the owner under a governed policy, anchor resolution SHALL use
the catalogue a vault without the pages withheld from that caller would hold. The
anchors a turn's words can reach (by name, alias, derived short name or name term)
SHALL be decided before evidence is assembled; name-term counts SHALL count visible
owners only, stopping once a term is common among them; a derived short name retired
only by a withheld anchor SHALL be restored; and an anchor reached only by other
evidence SHALL be decided before resolution. Only those anchors SHALL be decided, so
the request stays within the activation path's filesystem ceilings. Such a caller's
packet SHALL be neither served from nor stored in the packet cache. The owner's
resolution, packets and cache SHALL be unchanged.

#### Scenario: A withheld anchor shares a visible anchor's name
- **WHEN** a withheld anchor shares a visible anchor's title or alias, retires its
  derived short name, makes a turn word common, or is the only anchor a turn names
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault
  without the withheld anchor, and never abstains as `withheld`
