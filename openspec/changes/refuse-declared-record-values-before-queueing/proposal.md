## Why

The busy-write intake returned a `queued` receipt for a Records save whose value lies outside a field's declared enum. The replay refused the value later, after the caller had moved on. The argument-only checks that run before queueing cannot see this refusal, because it reads the collection's declaration from the vault.

## What Changes

- Before a Records append or update is queued, the serving process reads its collection's declared fields as a request thread reads them. A value that those fields refuse gets that refusal at once, and the save is not queued.
- The refusal leaves through the terminal egress filter, as the same refusal from the save itself does.
- The save stays queued while an open queued save may change the declared fields first. Such a save is one of a command that can rewrite a page's frontmatter, whichever page it names, or a Records save that carries a manifest's text.
- The check runs only for a save that is about to queue, and stops within a fixed time. A declaration or queue that it cannot read leaves the save queued.
- A direct revise that commits after the check, before the replay would have run, is not seen. The caller then gets a refusal that the revised collection would not give, and sends the save again.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: the requirement that a save its arguments condemn is refused now also refuses, before queueing, a Records value that its collection's declared fields certainly refuse.

## Impact

`commands.declared_refusal_before_queueing`, `record_memory.value_refusal`, `write_intake.may_change_declaration` and `Command.rewrites_frontmatter`. No change to the queued receipt, the intake store or the replay.
