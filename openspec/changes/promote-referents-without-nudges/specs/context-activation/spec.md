## ADDED Requirements

### Requirement: An anchor expands through the profile that declares its kind

For each resolved anchor, activation's graph lane SHALL use the traversal profile whose `activation_anchor_kinds` declares that anchor's kind.
An entity anchor SHALL use the `entity` profile, and every other anchor kind SHALL use the profile that declares it, which is `epistemic` in the shipped pack.
Activation code SHALL NOT name a profile.
The graph lane SHALL pass the reader's release decision to the traversal, so a restricted reader's expansion never routes through a page withheld from it.
A typed neighbour that the `entity` profile reaches SHALL be served as a `pointers[]` entry whose `why` names the relation labels on its path.
The existing depth, node and edge bounds, the resolved-only rule and the egress guard SHALL apply unchanged, and warm activation SHALL stay under 1 second at p95.

#### Scenario: An owned entity is reached from its owner

- **WHEN** a fresh-session turn resolves a person entity that `owns` a dog entity, a supplement entity is `used_for` that dog, and no wikilink joins the person and the supplement
- **THEN** the packet's `pointers[]` holds the supplement with a `why` that names `owns` and `used_for`
- **AND** the same corpus under the former `epistemic` selection holds no such pointer

#### Scenario: A non-entity anchor is unchanged

- **WHEN** a turn resolves a hub anchor
- **THEN** the graph lane uses `epistemic` and the packet equals the packet before this change

#### Scenario: A withheld page is not a stepping stone

- **WHEN** a restricted reader's turn resolves a person whose only path to a visible entity runs through a page withheld from that reader
- **THEN** the packet holds no pointer to that entity
