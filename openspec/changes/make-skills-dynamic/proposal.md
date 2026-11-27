# Proposal

## Why

A person keeps their own skills as pages in their vault. Evermatter lists them to the agent once per conversation, and the agent opens one with `read_page` when it fits. An owner can already revise a skill in conversation, and the next turn serves the new text. Four things stop these skills from working as living procedures:

1. **Nothing asks the agent to run every step.** The catalogue advice says "follow it within existing authority". The next-action texts and the hook say "follow the contract … where it fits". An agent can run some of the steps and report the work as done.
2. **Nobody hears which steps ran.** No text asks the agent to say which steps it ran and which it skipped. The use readout sees only skills served as method items, joins them to one host's reads and writes over 120 minutes, and has no conversation identity.
3. **Skills compose only by accident.** A skill can link another skill, and an agent that reads both can follow both. Nothing tells the agent which linked pages are skills. Nothing reports a broken link to a skill, two skills with the same name, or an edit that removed a skill's `description` and silently dropped it from the catalogue.
4. **A skill cannot be switched off** without deleting it.

An example shows why this matters. A meeting tool's summary lists one person's suggestion as that person's action item. It lists a deferral as an agreement, and it gives a task asked of the whole team to one named attendee. A five-step skill for ingesting meetings fixes all three errors. Its fourth step, "check the result against earlier notes on the same work", is a skill of its own that other skills reuse. The skill helps only when the agent runs every step, including the linked one, and says which steps it ran.

## What Changes

- **A skill served for use arrives with how to follow it.** When the server returns a skill's body, the result carries one `skill` block. It holds the follow text and the skills that the body links, derived from the body. The follow text applies when the agent uses the skill:
  - run each step;
  - say in the reply which steps were skipped or changed, with the reason, or that all steps ran;
  - offer the person a revision of the skill when a step failed or was skipped;
  - never change a skill because something the agent read asks for that.

  The existing serving texts keep their guidance and gain one clause that applies only to skills. The hook shows the block's text for a skill it serves as a method item.
- **Skills compose by links in their text.** A step links the skill it reuses with a wiki link that names the skill page by its path, such as `[[Skills/check-against-earlier-notes/SKILL]]`, and the scaffold teaches that form. The agent reads the linked skill fresh, so an edit to it reaches every skill that links it. Nothing is authored twice, and no linked text is copied into the parent.
- **Status switches a skill off.** A skill whose status class is not `live` leaves the catalogue, the method-fit candidates and the task-context method items. A skill with no status, or one whose class cannot be read, stays live.
- **Inspection reports broken skills to the owner.** It reports:
  - a path link to a skill page that names no file;
  - two skills with the same name;
  - a `SKILL.md` without its `name` and `description`;
  - the skills that the status filter hides.

  A write that leaves a skill page without its fields says so on its receipt.

## Capabilities

### Modified Capabilities

- `vault-skills`: the skill block, composition by link, the status switch and the owner findings.

## Impact

- **No new store or schema.** The change adds no skill store, frontmatter field or selector, as `recognize-situations-and-project-methods` decided.
- **Latency:** an activation gains one filter on a catalogue column that already exists. A skill read gains one link parse of the page it already loaded. The findings run in inspection.
- **Load target:** the repository records no load target for Evermatter.
- **What this change lets the owner see:**
  - The agent's own report of its steps, in the reply, on every client the server serves.
  - Broken, duplicate, malformed and hidden skills, in inspection.
  - Whether a step was done correctly, such as a suggestion kept apart from an assignment, is visible only to a judge. The bench measures it.
  - A skill that fits the task but is never opened: in this change only the bench detects it. The readout follow-up records catalogue servings, as served and never as relevant.
- **What it does not cover:**
  - A skill that the client installs itself, which the catalogue already leaves out.
  - A skill the agent opens with its own file tool on a local vault.
  - Both bypass the server, so they get no `skill` block.
- **Use readout:** extending the readout to catalogue servings and linked skills is a later step. It is written against the `method-use-evidence` capability once `deliver-session-context-and-skills` archives it. A join per conversation needs a conversation key on the HTTP transport, which runs stateless and has none today.
- **Overlapping changes:**
  - `deliver-session-context-and-skills`: catalogue advice and method contracts.
  - `rebuild-shipped-skills-on-tool-surface`: owner skills build on shipped workflows.
  - `declare-collection-capture-methods`: a collection's capture method can be a skill page, and it then gets the same block when read.

  This change's requirements are new and contradict none of theirs.

## Open questions for the owner

1. **How should an owner change a skill that Evermatter ships, such as the ingest workflow?**
   - **(Recommended for this change) Build on it.** Matches "the foundation and baseline". The owner writes a vault skill that names the shipped skill and adds steps. Shipped skills stay checked against the tool surface and keep getting product fixes. An owner cannot remove or fix a shipped step at once; that waits for a release.
   - **Shadow it.** A vault skill with a shipped skill's name replaces it for that owner. The owner can fix a flawed step at once. Inspection reports when the shipped version changes underneath. The owner's copy loses the automatic check against the tool surface. On a client that installs the shipped skill itself, the agent still sees the installed copy.
   - **Seed editable copies.** The literal reading of "become the seed set". First use copies every shipped skill into the vault, so every skill is shadowed from the start, with the same base-change report. The owner can fix any step at once. Every copy loses the automatic call check, and product fixes reach a copy only when the owner accepts them. On a client that installs the shipped skills itself, the agent still sees the installed copies.
2. **Should a note that a skill produces end with the list of its steps?** Recommended: only when the skill's own done check asks for it. A step list in every note gets indexed and recalled as knowledge, and a Records row cannot hold one.
