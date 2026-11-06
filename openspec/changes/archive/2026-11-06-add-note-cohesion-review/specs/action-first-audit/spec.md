## REMOVED Requirements

### Requirement: The audit detects semantic scope divergence from existing unit vectors

**Reason**: The `scope_divergence_semantic` category produced no finding on a labelled set of 40 large notes, with every unit embedded and routing switched off. Of those notes, 16 were overloaded, and the owner later approved splitting 13 of them. A vector grouping does not decide what a page is about.

**Migration**: Overloaded pages are now found through the agent-judged `note_cohesion` family in `attention-queue`. Review decisions recorded against `scope_divergence_semantic` remain as inert records. A request that selects the removed category is refused as an unknown category.
