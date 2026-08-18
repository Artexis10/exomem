# Design

## Context

Five facts shape this change, and four of them are things the repository already does.

`edit.py:519` refuses every write whose path sits inside `Sources/`, so no existing operation can change a captured source's `source_type` or `domain`. Classification is a one-way door today.

`move_file.py` already relocates a file within `Sources/`, rewrites every inbound wikilink across the vault, and does it through one `batch_atomic_write` with a log entry. Because provenance entries in `sources:` and `ingested_into:` are wikilinks in text, they are already rewritten by that pass. The relocation half of this change is therefore mostly assembly, not construction.

That same module already accepts the argument this change needs. It permits a Sources-to-Evidence move as a *promotion*, describing it as "the same raw item, reclassified once it becomes proof-bearing", and observes that the judgement "is made at capture time when the answer is often unknowable — a receipt is reference material until the appliance fails." It requires a `promotion_reason` naming why. A capture-time classification judgement being correctable later, with a stated reason, is an accepted principle here rather than a new one.

`note.py:763` appends to a cited source's `ingested_into:` frontmatter whenever a compiled note names it. Frontmatter on an append-only source is already system-mutated. Rule 2 protects the body.

Finally, the open taxonomy shipped a `source_classification_debt` advisory that detects fallback material and has nothing to act on, a registry that supports `status: deprecated` with `replaced_by:` and never migrates the sources still using the retired key, and a `path_label` a user may rename with no way to reconcile what is already filed.

## Goals

- One governed correction path for a source's kind and domain, with provenance intact.
- Make the shipped advisory actionable, and make deprecated-kind and registry-rename drift reconcilable.
- Keep the decision with the caller: report evidence, never infer a classification.

## Non-Goals

- No automatic reclassification, from any trigger.
- No bulk migration subsystem. Bulk is a caller looping over the single-source operation.
- No model inference in the core, matching the open taxonomy's rule.
- No change to the body of any source, ever.
- No retroactive invariant requiring an already-captured source to match today's projection. That remains deliberately absent; this operation is how a source moves, not a rule that says it must.

## Decisions

### Correct the classification and relocate in one operation, rather than composing two

A caller could in principle move the file with the existing move operation and then correct the metadata. It cannot: the metadata edit is refused outright, and even if it were not, the two steps would be independently observable and could interleave with a concurrent write, leaving a source whose location and metadata disagree.

Making it one operation is also what keeps the projection honest. The location is derived from the corrected classification rather than supplied by the caller, so a reclassification cannot invent a destination the projection would never produce, and every path-safety guarantee capture already has applies unchanged.

### Require a reason, following the promotion precedent

The existing promotion refuses without a `promotion_reason` because reclassifying an item's purpose is a judgement someone should have to state. The same argument applies with more force here, because this correction also rewrites references across the vault.

The reason is recorded on the source. A correction that cannot be explained is usually a correction that should not happen, and the recorded reason is what makes a later reader able to tell a deliberate correction from a mistake.

### Change frontmatter, keep the body byte-identical

This is the only genuinely new ground, and the line is drawn where the repository already draws it. `ingested_into:` proves frontmatter is not covered by content immutability; the body is what rule 2 protects. So the operation rewrites exactly the classification fields plus the fields recording the correction, and asserts the body is unchanged rather than trusting that it is.

Identity, capture timestamp, origin, tags, and ingested-into entries are explicitly preserved. A reclassification that silently reset any of those would destroy the provenance the change exists to protect.

### Record the previous path on the source rather than leaving a tombstone

A tombstone file would keep the fallback tree exactly as cluttered as it is now, and would double the number of files a reader has to disambiguate. Recording the previous path on the moved source keeps one file, and still answers the question a stale reference is asking.

Inbound references are rewritten regardless, so the recorded path is a recovery aid for holders outside the vault — an export, an external note, a URL someone wrote down — not the mechanism that keeps the vault consistent.

### Propose with observable evidence, and decline rather than guess

The read-only mode reports what it can observe: the domain segment already present in the source's location, whether an origin URL exists, the recorded title, and the existing metadata. That is enough to propose a domain for material already filed under one, and rarely enough to propose a kind.

When the evidence supports no kind, the operation says so. Presenting the fallback as a proposal would be exactly the failure the open taxonomy removed, and a plausible guess is worse than no guess because it invites approval without judgement.

An agent may of course decide the kind by reading the source. That is a caller doing its job, and it stays outside the core, which is the same boundary the open taxonomy drew.

So the read-only mode also previews a *supplied* classification, which is the path that actually gets used: the agent reads the source, judges the kind, and previews that judgement to show the user the destination and the affected-reference count before anything is written. Without it the mode can only preview its own proposal — usually empty, by the rule above — and propose-then-confirm is unusable for the case it exists to serve. Supplied values resolve through the same taxonomy rules the correction applies, so a value the write would refuse is refused during the preview, rather than after the user has approved it.

### Bulk is a loop, not a subsystem

The backlog that motivated this change is one vault's fallback tree. Building a migration engine for it would be building for a one-time shape.

A caller that wants to correct many sources calls the operation many times, each atomic on its own. Nothing is half-migrated if the caller stops, every correction carries its own reason, and there is no partially-applied batch state to recover.

## Risks and Trade-offs

Reclassification makes source locations mutable in a way readers may not expect. The recorded previous path and the rewritten references bound that, but a consumer that cached a path outside the vault will still need the recovery aid.

Requiring a reason adds friction to bulk correction, which is deliberate. The alternative is a bulk path that omits reasons, and the recorded reason is the only thing distinguishing a considered correction from an accident once both are in the past.
