## MODIFIED Requirements

### Requirement: Honest generic and abstaining outcomes remain first-class

Relation guidance SHALL state that the caller must choose the most specific registered relation that is truthful.
When none fits, the caller SHALL propose a new relation type through the registry route, write a plain link as a last resort, or author no edge when no durable relationship is established.
Guidance SHALL NOT suggest the generic relation for new authoring.
Resolution SHALL NOT fabricate directional semantics from topical proximity, require an extension proposal for every connection, or use a vocabulary-diversity quota.

#### Scenario: Legitimately generic connection stays generic
- **WHEN** two notes have a durable connection but the available evidence supports no more specific semantic claim
- **THEN** the resolver presents the plain link as the honest last resort
- **AND** it emits no requirement to create a new relation and does not present the generic relation

#### Scenario: A relation that no registered one expresses is proposed
- **WHEN** an agent can state a truthful relation that no registered relation expresses
- **THEN** guidance names the relation proposal route before the plain link

#### Scenario: Topical proximity does not become a directional claim
- **WHEN** two notes discuss the same topic without evidence of support, causation, implementation, or another directional relation
- **THEN** deterministic similarity evidence does not convert the connection into `supports`, `causes`, `implements`, or another specific predicate

## ADDED Requirements

### Requirement: A write that adds a generic relation is refused with specific options

A governed live write that adds a relation whose registry definition is generic SHALL refuse before commit, for every pair of pages, including note to note.
The barrier SHALL judge only the edges that this call adds: an edge already on the page before an edit, and an edge that a successor carries from its predecessor, SHALL NOT trigger it.
It SHALL cover create, replacement, edit, observe, overwrite and append operations, and SHALL NOT apply to a page that is not live or to the `adoption_compile` bulk operation.
The barrier SHALL read genericity from the registry attribute and SHALL NOT compare a relation key.
No retry argument, recorded reason or other answer SHALL keep the generic relation.

The refusal SHALL use the code `GENERIC_RELATION_REFUSED` and SHALL carry:
- up to eight ranked specific relations, each with its key, label, description and parent, and a route to the full list;
- the exact call for a retry with a specific relation, spelled for the writer that was called;
- the exact `propose-relation` and `save-relations` calls that propose a new relation type through the existing registry route;
- the exact edge that writes a plain link, as the last resort, with the plain link's key taken from the registry.

Identical bullets in one write SHALL be refused once.
An unresolved or withheld target SHALL NOT change the refusal, so both states SHALL produce the same observable result.

#### Scenario: A note-to-note generic edge is refused with the menu and three routes
- **WHEN** a create or an edit adds a generic relation between two notes
- **THEN** the write is refused with `GENERIC_RELATION_REFUSED`, the ranked options and the three routes
- **AND** the page is unchanged

#### Scenario: A retry with the top option commits
- **WHEN** the agent repeats the write with the first listed relation in place of the generic relation
- **THEN** the write commits and the page holds that relation

#### Scenario: A plain link commits
- **WHEN** the agent repeats the write with the plain link in place of the generic relation
- **THEN** the write commits

#### Scenario: An entity edge is refused the same way
- **WHEN** a write adds a generic relation that touches an entity
- **THEN** it is refused with the same code and routes

#### Scenario: An edit that leaves a legacy generic edge alone commits
- **WHEN** a page already holds a generic relation and an edit adds no generic relation
- **THEN** the edit commits and the legacy edge is unchanged

#### Scenario: A draft and a bulk import stay writable
- **WHEN** a draft or pending page, or an `adoption_compile` write, adds a generic relation
- **THEN** the barrier does not refuse the write

#### Scenario: A carried edge is not newly authored
- **WHEN** a superseding page retains its predecessor's generic relation
- **THEN** the barrier does not treat it as an addition

#### Scenario: A withheld target reads as an absent one
- **WHEN** a restricted writer adds a generic relation to a target it cannot see
- **THEN** the refusal equals the refusal for a target that does not exist, and no option depends on the target's type

### Requirement: The refusal menu ranks admissible relations from registry data and usage

One producer SHALL build the menu of specific relations for every surface that offers one in place of the generic relation.
A relation SHALL be admissible when it is active, is not generic, is allowed in the page's scope, and fits the pages' declared endpoints where it declares any.
A relation that declares no endpoints and inherits none SHALL be admissible for every pair.
Admissible relations SHALL rank by these keys in order:
1. a relation whose own endpoints name both pages;
2. a relation the vault already authors between pages of the same note types, most used first, counting authored specific relations that are neither generic nor the plain link;
3. an extension before a core relation;
4. the registry's order.
The menu SHALL hold at most eight relations and SHALL give the same menu for the same edge.
The usage count SHALL be computed in memory on the refusal path from the corpus the write already holds, and SHALL count only facts whose pages are visible to the writer.

#### Scenario: A relation the vault already uses for the pair ranks first
- **WHEN** the vault authors one admissible relation between notes of the two note types more often than another
- **THEN** the refusal lists that relation before the other

#### Scenario: Endpoints that name both pages outrank usage
- **WHEN** an admissible relation declares endpoints that name both pages
- **THEN** it ranks before a relation that declares none, however often the vault uses the other

#### Scenario: An extension ranks before a core relation
- **WHEN** an extension and a core relation tie on the earlier keys
- **THEN** the extension is listed first

### Requirement: Generators leave relation choice to the agent

A relation candidate that observes only similarity or co-participation SHALL name no relation and no bullet.
Its public item SHALL carry the ranked specific relations and the plain link.
Queue acceptance of such a candidate SHALL require a `requested_relation` that names one of the offered relations or the plain link, and SHALL refuse the generic relation and a missing choice with `RELATION_CHOICE_REQUIRED`.
Compile proposals and question-pair suggestions SHALL carry options rather than default to the generic relation.
An upkeep link proposal for such a candidate SHALL stay offered, and its accept call SHALL require a `requested_relation` that the agent takes from the ranked options or the plain link, which the proposal's route carries.

#### Scenario: A generated candidate does not choose a relation implicitly
- **WHEN** a generator finds two pages that share a source, a question or a resolution target
- **THEN** its output carries the ranked options and the plain link and names no relation

#### Scenario: Accepting without a choice is refused
- **WHEN** an agent accepts such a candidate without naming a relation
- **THEN** the call is refused with `RELATION_CHOICE_REQUIRED` and the page is unchanged

#### Scenario: An upkeep proposal still offers the pair and needs a choice
- **WHEN** two pages with no authored relation share a source
- **THEN** the upkeep link proposal for the pair is offered
- **AND** its route carries `choose` with the ranked options and the plain link, and leaves `requested_relation` for the agent to fill

#### Scenario: Accepting with a chosen relation commits
- **WHEN** an agent accepts the candidate with an offered relation or with the plain link
- **THEN** the page holds one bullet with that relation to the target
