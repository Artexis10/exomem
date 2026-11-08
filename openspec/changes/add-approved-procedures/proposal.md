## Why

Exomem can already project a user's methods into the activation packet. A packet item says only that a method exists and that the agent should read its source. The agent cannot tell a procedure that the owner endorses from a method that arrived inside a captured or imported page.

Text that an agent reads can say "follow these steps" or "approve me", so an endorsement must not live in the text. A static skill package does not fill the gap either. It installs once per host, goes stale when the owner edits the procedure, and carries no endorsement.

The owner needs procedures that behave like skills but stay live knowledge. The owner creates, edits and retires a procedure the ordinary way. Any agent in any client then finds the procedure by meaning and follows it, for work inside or outside Exomem. An explicit approval by the owner is the only thing that makes a page followable, so imported or captured text never becomes instructions.

## What Changes

- Add a `procedure` note type to the shipped vocabulary pack. It is a compiled type in the folder `Notes/Procedures`, with optional sources. Its guidance asks for a `summary` that says when to use the procedure, a "not for" section and procedure blocks. Code never branches on the `procedure` key.
- Treat any compiled page that holds method units as a candidate procedure. Method units are the units whose category the context-roles registry maps to `projection: method`. The type adds authoring guidance only.
- Add an approvals ledger. It is a reserved, product-owned, append-only record in the vault, registered in the reserved-paths registry like source corrections. Each entry records the stable ref, the version, the action, the principal, the observed client label and the time.
- Add one operation, `approve_procedure`, with the actions `approve` and `revoke`. It takes `expected_version` and is owner-only, so a host can permission it apart from the write tools. The generated CLI gives a human the same route. Generic, import and capture routes cannot write the ledger.
- Compute one verdict per page and caller: `approved`, `stale` (owner only) or `unapproved`. A page is eligible when it is compiled, has a live status class, holds at least one method unit and has a unique ref. Sources and Evidence pages are never eligible. An unreadable ledger makes every verdict `unapproved` and reports a finding.
- Report the verdict where agents look. Method items in `task_context.methods` gain an `approval` field. An approved item adds one short line, "Approved procedure: <title>. Read it, then follow it where it fits.", charged to the shared packet budget. `read_memory` and an owner listing in `read_review` report the same verdict.
- Carry `approval` in the continuity `method_versions` hints. Revalidation resets held methods when the verdict differs, because approve and revoke change no page bytes. Approval adds no ranking.
- Teach the safety contract in the shared skill. Approval is a label, not a command. The owner endorsed this version as their way of working, the agent decides whether it fits, and the user's current instruction wins.
- Ship no inline steps in the packet and no generated `SKILL.md` files.

## Impact

- New capability `procedure-approval`. Modified capabilities: `governance-authoring` (the approvals tree joins the reserved trees), `context-activation` (method items carry the verdict) and `skill-loading` (approved procedures inform, never command).
- The `context-activation` delta only adds a requirement, because the open change `recognize-situations-and-project-methods` also modifies that capability. This change builds on that change's method projection and `method_versions` hints, so implement the serving work on top of it.
- The tool surface gains `approve_procedure`. The packaged tool-surface digest and the schema budget change, so the ChatGPT Personal Plugin needs its ordinary post-deploy schema refresh, and `docs/capabilities.md` needs regeneration. The plugin tree and the skill copies need regeneration for the scaffold text and the hook.
- A server-made rewrite of an approved page, such as a link rewrite or an `updated:` stamp, changes its bytes. The approval then reads `stale` until the owner approves the new version. This is accepted and visible.
- No vault migration. Existing pages stay `unapproved`. Existing vaults gain the ledger on the first approval.
- The proof runs on real models, because the behaviour depends on what agents do with the label and with hostile text. Tasks section 4 lists the cases.
