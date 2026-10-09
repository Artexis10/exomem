# Live vocabulary

Read the current registry before choosing or changing vocabulary. A saved registry
change takes effect immediately; a queued proposal does not change the registry.
Use the caller's existing authority and the returned result, not an assumption
that every caller can save.

## Discover the current contract

Inspect the exposed bootstrap schema. When it supports `section`, obtain
`bootstrap(profile="compact", section="vocabulary")`. Otherwise use
`bootstrap(profile="full")`; released profiles reject section arguments.
Follow the returned routes and use only operations exposed by the active surface.
If the registry contract is unavailable, report that limitation instead of
treating the registry as empty.

The `subject` argument of `schema_memory` names the current registry subjects.
They are `schema_memory` subjects, not a list of allowed vocabulary entries. Compiled applicability comes from
`semantic_authoring.minimum_semantic_unit` in the authoring bootstrap section,
or the full-profile fallback. Note types have no registry subject here.

## Reuse before promotion

1. Call `schema_memory(operation="inspect", subject=<subject>)` to obtain entries, `content_hash`, and the `save` contract.
2. Follow each returned `continuation` with the same subject until it is null, so the inventory is complete.
3. Read `save.fields`, `save.attributes`, `save.fixed_once_saved`, and `save.promotion` to establish the permitted delta.
4. Reuse a truthful existing key, or add an alias when only the spelling differs, so meaning stays stable.
5. If nothing fits, propose justified recurring meaning with a new canonical key, retaining only fields the returned schema supports.

Use a parent only when `save.fields` supports `parent`, and inspect its inherited
meaning. A parent or fixed behavior cannot be redefined in place. New meaning
uses a new canonical key and deprecates the replaced key according to the returned
contract. Keep a truthful generic relation or no edge when a specific meaning
does not fit; a question alone does not justify a new relation type.

`source-kinds` and `domains` use `auto-register` promotion: their owning source
writer registers valid new labels. Read its receipt; do not require a separate
promotion save before preserving a source. A valid unseen semantic category also
needs no registry write merely to author it.

Counts describe the returned projection and its source. `counts="unavailable"`
or an omitted count is unknown, not zero. A bootstrap summary is not the complete
registry; use paginated inspection before concluding that a key is absent.

## Propose and save

1. Pass the delta as `proposal` to `schema_memory(operation="propose", subject=<subject>, proposal=<delta>)` to inspect validation and collisions.
2. Review `valid`, findings, collisions, inherited attributes, and near-duplicates to choose a supported delta.
3. Call `schema_memory(operation="save", subject=<subject>, proposal=<delta>, expected_hash=<current_hash>, why=<reason>)` to request the governed save.
4. Inspect `saved`, `state`, and `vocabulary_receipt` to report the actual result and its revert route.

A delta uses the returned verbs: `upsert` maps a key to its supported entry fields;
`alias` maps a canonical key to additional aliases; `deprecate` maps a key to its
replacement. Use the current inspection's `content_hash`, or the proposal's
returned `expected_hash`, as `expected_hash`. Never substitute `effective_digest`.
On a stale hash, inspect and reconsider the delta against the changed registry.

Report a committed promotion with its subject, key, meaning, and returned restore
route. If `state="pending_review"` and `saved` is null, report the queued item and
that the registry remains unchanged. A refusal or invalid proposal is not a save.

## Revert without rewriting pages

1. Call `schema_memory(operation="history", subject=<subject>)` to obtain kept versions and the current `content_hash`.
2. Select the intended version, then call `schema_memory(operation="restore", subject=<subject>, version=<version>, expected_hash=<current_hash>, why=<reason>)` to request its restoration.
3. Inspect the restore result and removed keys to report what changed and any remaining unregistered usage.

Restore uses the current registry hash, not the historical hash of the chosen
version. It preserves a new history entry and never rewrites pages. Existing
pages or edges that use a removed key retain their bytes and can remain
unregistered. Source kinds and domains share one overlay, so its restoration can
revert changes on both axes; inspect the reported removed keys.

## Page statuses

Inspect `schema_memory(subject="statuses", operation="inspect")` when that route is exposed.
Read each entry's `attributes.class`; the class controls current service without rewriting pages.
Canonical shipped labels keep their meanings. Statusless pages stay live without private definitions.
An admitted unknown label is live with review debt; withheld classification leaves dependent operations unavailable.
A live compiled result needs a semantic unit; a pending result may remain unit-free until activation.
Use a distinct new key and governed replacement/deprecation to change meaning; saved classes are immutable.
Registry restore restores definitions, not page bytes.

If the profile has no registry tool, use its full bootstrap operating guidance and its exposed validation route.
Historical authoring artifacts retain their identity; the operating guidance states the current shared validation rule.
Do not invent a status inspection endpoint for a frozen profile.
