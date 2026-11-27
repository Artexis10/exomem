# Live vocabulary

Read the current registry before you choose or change vocabulary. A saved registry change takes effect at once.
A queued proposal changes nothing. Use the caller's existing authority and the returned result: do not assume
that every caller can save.

`read_instructions(section='vocabulary')` gives the policy block, and `read_instructions(topic='schema.save')`
lists the fields of one operation. If the registry is unavailable, report that limitation. Never treat the
registry as empty.

The `subject` of a `schema.*` operation names a registry, such as `note-types`, `statuses` or `domains`. It is
not a list of allowed entries. Read the entries with
`inspect_memory(operation="schema.inspect", arguments={"subject": ...})`. A page type is compiled when its
`note-types` entry has the note-type role `compiled`. Current structural applicability comes from
`semantic_authoring.minimum_semantic_unit` in `read_instructions(section='authoring')`.

## Reuse before promotion

1. Read the registry with `schema.inspect`. It returns the entries, a `content_hash` and the `save` contract.
2. Read `save.fields`, `save.attributes`, `save.fixed_once_saved` and `save.promotion` to see the permitted delta.
3. Reuse a truthful existing key, or add an alias when only the spelling differs, so meaning stays stable.
4. If nothing fits, propose a justified recurring meaning under a new canonical key. Keep only the fields the
   returned schema supports.

Use a parent only when `save.fields` supports `parent`, and inspect its inherited meaning. A parent or a fixed
behaviour cannot be redefined in place. New meaning takes a new canonical key, and the replaced key is
deprecated as the returned contract says. When no specific meaning fits and the meaning is not durable and
recurring, write a plain link, or no edge. A question alone does not justify a new relation type. A write that
adds the generic relation is refused.

`source-kinds` and `domains` use `auto-register` promotion: the source writer registers a valid new label. Read
its receipt, and do not require a separate promotion save before you preserve a source. A valid unseen semantic
category needs no registry write to author it.

Counts describe the returned projection and its source. `counts="unavailable"` or an omitted count is unknown,
not zero. A `read_instructions` summary is not the complete registry. Page through the inspection before you
conclude that a key is absent.

## Propose and save

1. Pass the delta as `proposal` to `inspect_memory(operation="schema.preview", arguments={"subject": ..., "proposal": ...})`.
2. Read `valid`, the findings, the collisions, the inherited attributes and the near-duplicates, and choose a
   supported delta.
3. Request the governed save with
   `manage_memory(operation="schema.save", arguments={"subject": ..., "proposal": ..., "expected_hash": ..., "why": ...})`.
4. Read `saved`, `state` and `vocabulary_receipt`, and report the actual result and its revert route.

A delta uses the verbs the preview returns: `upsert` maps a key to its supported entry fields, `alias` maps a
canonical key to more aliases, and `deprecate` maps a key to its replacement. Use the current inspection's
`content_hash`, or the proposal's returned `expected_hash`, as `expected_hash`. Never substitute
`effective_digest`. On a stale hash, inspect again and reconsider the delta against the changed registry.

Report a committed promotion with its subject, key, meaning and returned restore route. If
`state="pending_review"` and `saved` is null, report the queued item and say that the registry is unchanged. A
refusal or an invalid proposal is not a save.

## Answering a vocabulary item

`list_review_items(queue='vocabulary')` lists the owner's open considerations. Raise `limit` when more remain.
`inspect_memory(operation="review.item_context", arguments={"ref": ...})` returns one item's evidence. Record the
decision with
`manage_memory(operation="review.decide_vocabulary", arguments={"ref": ..., "decision": ...})`.

A decision is not execution or permission. A new identity still goes through its family's save below, under the
live envelope.

- **A relation.** Find a truthful existing one with `inspect_memory(operation="connections.resolve_relation")`,
  and propose a new one only when none fits. Preview it with
  `inspect_memory(operation="schema.propose_relation", arguments={"subject": "relations", "proposal": ...})`. Save
  it with `manage_memory(operation="schema.save_relations")`, passing the `proposal`, `expected_hash` and `why`
  from the preview, with `vocabulary_ref` and `vocabulary_fingerprint` from the item to bind the save to it.
- **An entity type.** Find a truthful existing one with
  `inspect_memory(operation="schema.resolve_entity_type", arguments={"subject": "entity-types", ...})`. Save a
  new one with `manage_memory(operation="schema.save_entity_types")`. The finding supplies the `proposal` and
  `expected_hash`, and `why` and the item's `vocabulary_ref` and `vocabulary_fingerprint` complete the call.
- **A relation suggestion that lacks the right relation.** Record the relation through the vocabulary decision
  first. Then accept the suggestion with
  `decide_review_item(review_item=..., decision="accept", vocabulary_ref=..., vocabulary_fingerprint=...)`. When
  the suggestion already offers the relation, pass `relation=<option key>` instead.

## Recorded positions

Recall carries a unit as the person's recorded position only when its category or its kind has the trait
`position`. The shipped `preference` category has it. A position category holds a view the person stated, and its
text names them. Words such as "my position" in a unit's text do not make it a position.

1. Capture a view that the person states as theirs under a category with the trait, within the live engagement policy.
2. Record your own analysis under a category without the trait.
3. Keep their wording and qualifier, and put the date in the context slot: `- [preference] The owner prefers a slow cool for celadon; not proven (2026-03-02)`.
4. To mark a category as holding positions, propose `traits: [position]` on it through the save steps above.
5. For a shipped category, propose only `traits`. Its description and aliases stay fixed.
6. Read the preview findings before you save. A trait outside the closed set is refused with `unknown_trait`.

The next turn whose leads reach that page carries the unit in the `positions` block.

## Topic carriers

A `topic-carriers` entry files the pages of one project key, folder or tag under the domains it names. The map
uses a save at once, and no page changes. Declare carriers from evidence, never from a list of your own.

1. Read the owner's considerations from `list_review_items(queue='vocabulary')`. Each names one carrier.
   `review.item_context` returns its counts and a bounded sample of titles.
2. Infer a topic from that evidence. Reuse a domain key from
   `inspect_memory(operation="schema.inspect", arguments={"subject": "domains"})` only when it names the subject
   the pages share. A neighbouring or broader domain is not a fit. When none fits, register one first with
   `manage_memory(operation="schema.save", arguments={"subject": "domains", ...})`. That is part of this workflow
   and needs no separate approval. A refused or pending save stands: report it and do not declare the topic. Skip
   a carrier whose pages share no subject, such as a status tag or a folder that mixes topics.
3. Save the carrier with `manage_memory(operation="schema.save", arguments={"subject": "topic-carriers", ...})`.
   `proposal.upsert` maps a key to `attributes` of `kind` (`project`, `folder` or `tag`), `match`, `topics` and
   `origin` (`inferred` for each topic). Add `expected_hash` and `why`.
4. Show the person one review list. Each row names the carrier, its topic, the origin and a few entry titles. The
   list names every domain you registered. They can accept all, change a row, or skip.

Accepting saves the same carrier with origin `reviewed`. Changing a row saves new `topics` by replace and lists
the turned-down key in `rejected`, so it is never offered again. Skipping keeps every declaration `inferred` and
in effect, and it stays listed for later review. A hand-written `domain` on a project key is read as `reviewed`
and never rewritten. A refusal names its reason, such as an unresolved or rejected topic or a type folder. Fix it
from the finding instead of retrying the same save.

A page that no carrier reaches joins a topic through its own `topics` field. A page's `domain` field names a
source's subject or an experiment's folder, and it files no topic.

## Revert without rewriting pages

1. Call `inspect_memory(operation="schema.history", arguments={"subject": ...})` to get the kept versions and the current `content_hash`.
2. Select the intended version. Call `manage_memory(operation="schema.restore", arguments={"subject": ..., "version": ..., "expected_hash": ..., "why": ...})`.
3. Read the restore result and the removed keys, and report what changed and any usage that is now unregistered.

Restore uses the current registry hash, not the historical hash of the chosen version. It adds a history entry
and never rewrites pages. Pages or edges that use a removed key keep their bytes and can stay unregistered.
Source kinds and domains share one overlay, so a restore can revert changes on both axes. Read the reported
removed keys.

## Page statuses

Read `inspect_memory(operation="schema.inspect", arguments={"subject": "statuses"})`. Each entry's
`attributes.class` controls current service, and it never rewrites pages. Canonical shipped labels keep their
meanings. A page with no status stays live without private definitions. An admitted unknown label is live with
review debt, and a withheld classification leaves dependent operations unavailable. A live compiled result needs
a semantic unit, and a pending result may stay unit-free until activation. To change a meaning, use a new
distinct key with a governed replacement or deprecation, because saved classes are immutable. A registry restore
restores definitions, not page bytes.
