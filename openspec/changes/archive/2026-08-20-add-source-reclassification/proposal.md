# Correct a captured source's classification without breaking its provenance

## Why

Source classification is currently a one-way door. `edit.py:519` refuses every write into `Sources/` as append-only, so a captured source's `source_type` and `domain` can never be corrected. Capture it wrong and it is wrong permanently.

That was tolerable while the vocabulary was closed and every artifact had one legal destination. It is not tolerable now. Opening the vocabulary made classification a judgement an agent makes per capture, and judgements made under uncertainty need a correction path or they accumulate as permanent debt.

Three consequences are already live. The `source_classification_debt` advisory reports that a real source kind probably exists for material sitting in the fallback, and nothing can act on it — an advisory with no remedy is a nag. The taxonomy registry accepts `status: deprecated` with `replaced_by:`, warns at capture time, and never migrates the sources still carrying the retired key. And a user who renames a kind's `path_label` gets a registry that silently disagrees with every file already filed under the old segment.

Adoption compounds all three: `adopt_vault` imports external trees into `Sources/`, unclassified by construction.

## What Changes

A source's classification becomes correctable through one governed operation that changes `source_type` and `domain`, relocates the file to the projection those values imply, rewrites every inbound reference, records the prior path, and requires a stated reason.

The body stays byte-identical. Only the classification fields change.

Both halves are already precedented rather than new ground. `move_file` permits relocation within `Sources/` and already treats a Sources-to-Evidence promotion as a *reclassification* of "the same raw item", noting that the Source-versus-Evidence judgement is "made at capture time when the answer is often unknowable"; it requires a reason for exactly that transition. And `note.py:763` already mutates an append-only source's frontmatter, appending to `ingested_into:` whenever a compiled note cites it. Rule 2 protects the body, not the frontmatter, and a capture-time judgement is already understood to be correctable.

A read-only proposal mode reports what a reclassification would do, with the evidence behind each proposed value, so a caller decides rather than a heuristic deciding for it.

## Capabilities

- **Modified: `command-surface`** — the reclassification operation, its refusals, its provenance guarantees, and its read-only proposal mode.

## Impact

Sources acquire a correction path they have never had. The advisory shipped with the open taxonomy becomes actionable, deprecated kinds become migratable, and a registry rename becomes reconcilable.

The operation deliberately does not classify anything on its own. It applies a decision and reports evidence; choosing the value stays with the caller, which keeps model inference out of the core exactly as the open taxonomy did.

Bulk reclassification is a caller looping over the single-source operation, not a separate subsystem. Nothing reclassifies automatically, and no existing source moves unless something explicitly asks.
