## MODIFIED Requirements

### Requirement: Templates are ordinary independent scaffolds
A collection MAY recommend ordinary Markdown templates, default properties, validation guidance, entry examples, and future form descriptors. The binding schema SHALL live independently in the collection contract. Changing a template SHALL NOT rewrite history; using a template SHALL NOT require an agent, Obsidian, or any plugin; and returning a template SHALL pass the same governance boundary as other files. `edit_memory` SHALL edit a frontmatter-less template through whole-body, surgical string, batch-string, and section operations without adding frontmatter or an `updated:` field. Operations that require frontmatter SHALL refuse such a page without claiming that its supplied path is missing.

#### Scenario: Optional Obsidian template works without Exomem
- **WHEN** a vault is configured to use `Knowledge Base/Templates/` and a user invokes `Templates → Insert template`
- **THEN** an X3 Push or Pull template can be inserted and manually completed without Exomem runtime involvement

#### Scenario: Template change does not change schema or history
- **WHEN** a user edits a collection template
- **THEN** the template bytes change independently and existing record items, collection schemas, audit history, and query results remain unchanged

#### Scenario: Frontmatter-less template is edited through the normal editor
- **WHEN** a caller performs a whole-body, surgical string, batch-string, or section edit on an ordinary Markdown template with no YAML delimiters
- **THEN** the requested body change commits through the normal editor and the result still has no YAML delimiters

#### Scenario: Metadata edit still requires frontmatter
- **WHEN** a caller requests tag or frontmatter mutation on a frontmatter-less template
- **THEN** the operation fails with a stable frontmatter-required error and does not report the resolved `path` argument as missing

#### Scenario: Obsidian insertion stays ordinary
- **WHEN** a vault is configured to use `Knowledge Base/Templates/` and a user invokes `Templates → Insert template`
- **THEN** an X3 Push or Pull template can be inserted and manually completed without Exomem runtime involvement
