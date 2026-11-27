---
name: evermatter-capture
description: 'Save what a conversation settled into Evermatter: a conclusion, a decision, a fact about a person or thing, a recurring item or measurement, or a proof file. Use when the user says save, remember, log or file this, or when the session lands on something durable that the engagement level allows you to capture.'
metadata:
  skill_contract: f7588bce55c4db96ad1f99ad20b4fffee7a1a82475af73bb706bdba3d9424655
  version: "0.2.0"
---

# evermatter-capture

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Save durable material once, in the home that fits it, at its true strength.

## When to use
The user asks to save, or the session lands on a durable conclusion, decision, entity fact, recurring item, observed outcome or proof file.

## Durable personal baselines
A stable preference, recurring routine, historical baseline or durable affiliation is eligible only
when both its stability or recurrence and its reusable comparison, interpretation or decision value
are clear. Attach it to a uniquely resolved Entity as a facet or affiliation; otherwise write one
concise compiled observation under `proactive_capture`. A recurring named item or measurement goes
to Records (step 3). Fleeting preferences, one-off activity, incidental associations and trivial
metrics get no note. A tentative or unresolved claim never becomes a baseline; save it as
tentative, attributed, with its uncertainty. A new Entity or your own affiliation edge follows
`proactive_capture`; accepting a queued relation uses `link_acceptance`; a merge or substantial
curation uses confirmed `restructure_execution`. A decision or standing rule agreed in conversation
is durable: save it when it is agreed, as `proactive_capture` allows.

## Workflow
1. Find where it belongs before you write. Reuse the `recall_context` packet if the conversation holds one; otherwise call `recall_context(turn=<the user's message verbatim>)`. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to capture and where it goes. Look at its `skills` for a Records collection that fits, and use `search_memory` for a named thing the packet does not cover.
2. Split the material into objects that a later question would ask for separately, and give each one home:
   - a raw text or file the user shares: `save_source` (`content` or `files`, with `source_kind` and `domain`);
   - a proof file (a receipt, label, invoice, warranty): `save_evidence(files=...)` in the turn it arrives, or `upload_files(lane="evidence", ...)` when you hold no file handle, and put the returned `cite` wikilink in the text you write about it; never keep a card number, code or password;
   - a recurring named item or measurement: a Records row (step 3);
   - a stated intent or commitment: a plan item (step 4);
   - a fact about a known person, place or thing: that entity (step 5);
   - a conclusion, decision, failure or method: a compiled note (step 6).
3. Records: call `query_records(collection=<name>)` on the collection that fits, then `add_record` or `edit_record` by its natural key. When none fits and the item recurs, `add_record` with `new_collection` creates a minimal one under the capture disposition. A fact that no field holds goes in with `new_fields`, never into another field.
4. Planning: look for an existing item with `query_plan_items` before `add_plan_item`; change an item with `edit_plan_item`. An observed outcome never moves a plan item by itself.
5. Entities: call `inspect_memory(operation="connections.resolve_entity", arguments={"name": ...})`. One match: add the fact with `add_observation` or `edit_note` on that page. No match, and the identity is stable, and central or recurring, and useful beyond this source: `manage_memory(operation="connections.create_entity", arguments={...})`. When the name you write is in another script than the user's spelling of it, add a native-script alias (`aliases` at create, or through `edit_note`). A stable fact about a resolved person, the vault owner's own entity included, goes to that entity under `proactive_capture`, not just in the reply that used it. Contact details (phone, address, email, identifiers) go on the same page in a dedicated `## Contact` section, each as its own `- [contact] ...` unit, never in the summary: recall serves them only when the turn asks to reach the person. Do not create a separate contact page or page type.
6. Compiled notes: check for an existing note on the same question first, and prefer `edit_note`, `add_observation` or `supersede_note` over a parallel page. Otherwise call `create_note` with the note type that fits, `sources` naming what steps 2 and 3 saved, and the links you already know; ask `create_note(..., suggest_links=true)` or `inspect_memory(operation="connections.suggest_links", arguments={"path": "<page path>"})` (or `"draft_title"` and `"draft_body"` for an unsaved draft) when links are still unknown.
7. Save each claim at its true strength: a held view attributed and with its uncertainty, an unsure report as unsure, an open question as open, settled values in full.

## Output contract
Report the `user_summary` of each write in one line, in the user's words; name a collection the way recall cites a page. The words Planning, Records, collection, schema and natural key never lead the report. If nothing durable was saved, say why.

## Save rules
Keep raw material verbatim in Sources or Evidence and conclusions concise in compiled notes. One review of the whole write plan, not one per write.
Before you promote or correct material that `artifact_role_promotion` or `transient_state_review` flags, follow [artifact role and transient state](references/audit-checks.md#artifact-role-and-transient-state).

## Mistakes to avoid
Do not write before you have looked for the home that fits. Do not save brainstorming as a conclusion, bury raw evidence inside a note, or create a parallel page where an existing note, entity or collection should change. A passing mention gets no note.
In a software project, the repository and its specifications carry transient progress; save the reusable decisions and verified milestones, not the progress.
