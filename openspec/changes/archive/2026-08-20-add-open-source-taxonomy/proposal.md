## Why

Source classification is a closed vocabulary whose machine truth is a markdown table. The runtime regex-scrapes six lowercase tokens out of the vault's frontmatter reference at startup, refuses any value outside that list, and maps each accepted value through one hard-coded table to one folder. The scraper's token pattern cannot express a hyphen, so a multi-word source kind is unrepresentable by construction.

The consequence is classification debt rather than a missing feature. An artifact whose kind and subject are both obvious — a research report about travel, belonging to a user project — has exactly one legal destination, the `other` fallback. Real vaults then grow subject folders underneath that fallback, so `other` becomes an accidental parent namespace for categories the model refuses to name. The portable agent contract compounds it by publishing the fallback as the default capture argument and by describing the vocabulary in prose as a closed list of six.

Two axes are being forced through one closed field. What an artifact **is** and what it is **about** are independent, and neither is knowable in advance by the product: a user's meaningful source kind or subject domain must not require a software release. The existing project-key registry already solves exactly this for the project axis — open slug vocabulary, auto-registration on first write, derived display label, typo guard — so the pattern is established and only needs applying to the two axes that lack it.

## What Changes

- Open the source-kind vocabulary. Any normalized, safe, slug-shaped kind is accepted and canonicalized, including one the product has never seen, with no schema migration and no release.
- Add an independent, equally open subject `domain` axis, and a multi-valued `projects` axis that never constrains where a source is stored.
- Move the machine truth for both vocabularies out of the scraped markdown table into a vault-owned registry beside the existing project-key, relation, semantic-language, and traversal registries. The reference doc becomes documentation again.
- Derive the on-disk location as a deterministic projection of the canonical semantic keys, so the filesystem stops being the ontology. Canonical machine keys, display labels, and path segments become separately addressable rather than the same string.
- Accept `source_kind` as the preferred parameter name alongside the existing `source_type`, refusing only an explicit conflict between the two.
- Make `other` a genuine low-confidence fallback: a confidently supplied kind SHALL NOT be demoted to `other` merely because the product has not seen that label before.
- Return at most one bounded advisory suggestion on capture when a source lands in `other` while carrying evidence that a real kind exists, reusing the established advisory-suggestion channel rather than adding a second one.
- Support independent retrieval filtering by kind, domain, and project, and any combination.
- Teach bootstrap, the canonical scaffold skill, and the knowledge packs that both vocabularies are open, that `other` is a fallback rather than a default, and that a reusable new label should be used rather than avoided.

## Capabilities

### Modified Capabilities

- `command-surface`: source capture accepts open, independently extensible kind and domain vocabularies plus multi-valued project association; derives its destination as a deterministic projection of those canonical keys; keeps every legacy value and every already-captured path valid; and may carry one bounded advisory classification suggestion.
- `agent-bootstrap-contract`: bootstrap teaches that source kind and domain are open vocabularies, stops publishing the fallback as the default capture argument, and teaches how to treat `other` and a classification suggestion.

## Impact

The source-capture write path, the source schema validator, the source index descriptions, the capture and recall command signatures, the structured-filter field set, the scaffold skill and its frontmatter, page-type and operations references, the knowledge-pack schema, and the bootstrap payload change, plus a new taxonomy module, a new vault-owned registry file, and focused tests. Generated artifacts — the tool-schema fixture and packaged surface digest, the capability document, and the Claude plugin skill copies — are regenerated from their generators.

The capture parameter set changes, so the packaged tool-surface fingerprint moves and the external connector attestation records a pending refresh. No tool is added or renamed, so the active-capability and command-fingerprint identities are unchanged.

No new table, index, background worker, embedding call, model call, persistent review object, or graph schema change is introduced, and no reindex is required. Existing write-latency, retrieval-latency, governance, and mutation-safety gates continue to apply unchanged.

Deliberately out of scope, and recorded as a named gap rather than left implicit: reclassifying or relocating an already-captured source. No file is moved, no reference is rewritten, and no path history is introduced. Existing `other` material stays exactly where it is and stays fully readable and retrievable. A governed source-reclassification operation — dry-run preview, atomic reference rewriting, path history, audit trail, refusal on ambiguous partial migration — is a separate follow-up change. Also out of scope: durable dismissal or cooldown state for the new advisory, which belongs to the write-warning suppression change that owns that mechanism.
