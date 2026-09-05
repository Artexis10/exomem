<!-- authority:non-specification -->

# Workflow contracts

Workflow contracts are optional, user-authored Markdown files stored under
`<kb_dirname>/_Schema/contracts/workflow/`. Their YAML frontmatter is the
canonical policy; the derived English block is safe to refresh, while any
Markdown outside that block remains yours.

Use `schema_memory(subject="workflow-contracts")` to inventory, inspect,
validate, resolve, preview, save, or refresh contracts. Resolve with the exact
known project, domain, and activity values. Omit an unknown value; use `null`
when it is known absent. A tie or incomplete context refuses rather than
guessing. A reviewed proposal can be resolved for the current session without
being saved; preview and save it with a reason to make the choice durable.

Planning always owns intended future state and Records always owns observed
events. A companion contract only declares opaque artifact ownership; it does
not call a tool, synchronize data, establish authority, or complete Planning.
Use `review_memory(mode="plan-progress")` and `unreflected_outcomes` to review
the feedback loop, then make an explicit Planning transition when appropriate.

Older vaults may report `WORKFLOW_CONTRACT_MIGRATION_REQUIRED`. Resolve the
reserved `@standalone` selection for the current session, or save a reviewed
standalone or companion contract. Existing Planning and Records files are not
rewritten. Rolling back simply leaves the Markdown contracts in place for a
later version to use again.

Future integrations and a hosted portal must use this same guarded
resolver/preview/save surface. Neither is supplied by this feature.
