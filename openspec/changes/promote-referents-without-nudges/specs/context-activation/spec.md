## ADDED Requirements

### Requirement: An anchor expands through the profile that declares its kind

For each resolved anchor, activation's graph lane SHALL use the traversal profile whose `activation_anchor_kinds` declares that anchor's kind.
An entity anchor SHALL use the `entity` profile, and every other anchor kind SHALL use the profile that declares it, which is `epistemic` in the shipped pack.
Activation code SHALL NOT name a profile.
The existing depth, node and edge bounds, the resolved-only rule and the egress guard SHALL apply unchanged.

#### Scenario: An owned entity is reached from its owner

- **WHEN** a fresh-session turn resolves a person entity that `owns` a dog entity, a supplement entity is `used_for` that dog, and no wikilink joins the person and the supplement
- **THEN** the packet's neighbourhood reaches the supplement through the `entity` profile at depth 2
- **AND** the same corpus under the former `epistemic` selection does not reach it

#### Scenario: A non-entity anchor is unchanged

- **WHEN** a turn resolves a hub anchor
- **THEN** the graph lane uses `epistemic` and the packet equals the packet before this change

#### Scenario: A vault extension joins without code

- **WHEN** a vault saves an extension whose parent belongs to a family in the `entity` profile
- **THEN** activation traverses edges of that extension from entity anchors with no code change
