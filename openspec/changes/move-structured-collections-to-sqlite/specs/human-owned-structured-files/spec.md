## MODIFIED Requirements

### Requirement: New items materialize their readable representation in one write

When a collection declares `item_filename`, the view of a new item SHALL use the rendered human path. When it declares `item_presentation`, the view rendered after every successful create, add, append, update, triage, or applied view edit SHALL contain the item's canonical frontmatter values and current managed presentation in one atomically published file. The substrate SHALL validate that the committed values can be rendered under the active filename and presentation recipes before commit, and SHALL refuse a mutation whose values cannot be rendered, so that no committed item can lack a renderable view.

#### Scenario: New Planning item is readable immediately
- **WHEN** an agent adds a valid item to a Planning collection with both recipes
- **THEN** once the post-commit projection has run, the item's view has a human title-based filename and a current readable managed block

#### Scenario: Presentation failure cannot leave frontmatter-only state
- **WHEN** a changed value cannot be rendered under the active presentation recipe
- **THEN** the whole mutation refuses, and neither the store nor any view changes

### Requirement: Managed Markdown is meaningful without becoming canonical

`item_presentation` version `1` SHALL render a bounded managed Markdown block containing a human heading, labelled selected canonical values, configured long text, and a Related section. Its marker SHALL bind the recipe digest and the item's row version. The item's authored body SHALL be canonical in the collection store and rendered exactly outside the managed block. Direct changes inside the managed block SHALL NOT be adopted as canonical state: edit-back SHALL treat a managed-block-only change as formatting and re-render it, and SHALL apply only changes to declared values and the authored body.

#### Scenario: Frontmatter-only Record gains a readable document
- **WHEN** a Records item contains selected summary and note fields with an active presentation recipe
- **THEN** opening its view in an ordinary Markdown reader exposes labelled values and readable note text without requiring frontmatter inspection

#### Scenario: Authored prose survives refresh
- **WHEN** an item has an authored body and a canonical field changes
- **THEN** the re-rendered view replaces the managed block and emits the authored body exactly

#### Scenario: Editing the managed block is not adopted
- **WHEN** a user edits only text inside the managed block of a view
- **THEN** no transition is recorded and the view is re-rendered from the store
