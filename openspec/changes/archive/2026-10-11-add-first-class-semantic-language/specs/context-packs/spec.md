## ADDED Requirements

### Requirement: Context Packs Include Bounded Cited Semantic Units
Context pack assembly SHALL include a `semantic_units` map for packed pages containing normalized compact and rich units with unit reference, category, kind, content excerpt, source anchor/span, parent identity, and authored relations. The map SHALL be bounded, SHALL report truncation, and SHALL avoid duplicating the same parent content as both structural claim text and unbounded units.

#### Scenario: Packed compact and rich units are visible
- **WHEN** a packed page contains compact observations and rich semantic blocks
- **THEN** its semantic-units entry contains both forms with their distinct category/kind metadata and citable anchors

#### Scenario: Unit cap is explicit
- **WHEN** a packed page has more units than the per-page or pack-wide cap
- **THEN** only the cap is returned and `truncation` states how many units were omitted

#### Scenario: Legacy semantic-block field is a projection
- **WHEN** a pack includes a rich semantic unit and also emits the compatibility `semantic_blocks` field
- **THEN** the legacy field is derived from that same bounded normalized unit and does not consume a second identity, parse, graph node, or ranking slot

### Requirement: Unit-Seeded Context Preserves Graph Semantics
When recall is unit-level or mixed, context assembly SHALL seed from the selected unit and parent, preserve authored unit relation direction/origin/anchor, and add bounded page/provenance/lifecycle context. It MUST NOT infer typed relations from category labels or semantic proximity.

#### Scenario: Unit relation anchors context
- **WHEN** a selected rich unit has an authored `evidenced_by` relation
- **THEN** graph-enriched context reports the unit anchor, typed relation, target, and provenance without converting unrelated page links into the same edge

#### Scenario: Category similarity does not create an edge
- **WHEN** two units share category `config` but have no authored relation
- **THEN** context may retrieve both through filtering/ranking but does not claim a typed graph relation between them

### Requirement: Semantic Unit Packing Is Measurement-Only
Semantic-unit context assembly SHALL use parsed Markdown, registries, derived indexes, and deterministic bounds only. It MUST NOT invoke a generative/reasoning model and SHALL remain useful when optional embeddings are unavailable.

#### Scenario: Embeddings-off unit pack remains useful
- **WHEN** embeddings are disabled and a category-filtered deep recall runs
- **THEN** the pack includes lexical/category-matched units, parent metadata, authored relations, and provenance while reporting embeddings unavailable
