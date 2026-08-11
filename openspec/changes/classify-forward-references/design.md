## Context

`audit._check_broken_wikilinks` currently emits `broken_wikilink` for every body wikilink that fails Markdown-page or attachment resolution. A missing Markdown page and a definite attachment error therefore look identical, even though a missing page can be an intentional forward reference that self-resolves when the page is later created.

This change affects audit classification only. Relation disposition independently resolves body wikilinks against connectable governed pages, so unresolved links remain unable to satisfy the connectivity lane.

## Goals / Non-Goals

**Goals:**

- Report unresolved Markdown-page targets as informational `forward_reference` findings.
- Retain `broken_wikilink` for deterministic errors: missing explicit-extension attachments, ambiguous title resolution, and extensionless note links that collide with an existing non-Markdown file.
- Reuse the existing resolver pass so creating the target clears the finding on the next audit.

**Non-Goals:**

- Inferring author intent or predicting whether a missing page will eventually be written.
- Changing semantic relation disposition or allowing unresolved targets to satisfy connectivity.
- Auto-creating pages or mutating links.

## Decisions

### Classify by what the resolver can prove

A target that names a nonexistent Markdown page is a `forward_reference` at `info` severity. The audit cannot deterministically distinguish a deliberate future page from a typo or deleted page without adding authoring syntax or heuristic reasoning, so it does not claim to. Definite resolution conflicts remain `broken_wikilink`.

### Keep forward references in the registered audit category set

`forward_reference` is a normal audit category so default audit reporting remains complete after the split and callers can filter it explicitly. The existing broken-link category remains independently filterable.

### Share one wikilink scan

The existing checker receives the selected subset of the two categories and emits only requested classifications. This avoids parsing the corpus twice when both categories are selected.

## Risks / Trade-offs

- [A typo to a never-created Markdown page is initially reported as a forward reference] -> Keep the signal informational and let reviewers decide whether to create, correct, or remove it.
- [Callers filtering only `broken_wikilink` no longer see missing Markdown pages] -> Expose `forward_reference` explicitly and include it in the default audit category set.
- [Classification accidentally relaxes governance] -> Do not modify semantic-contract code; retain a regression assertion that disposition remains unsatisfied.
- [A crafted relative target escapes the vault during collision probing] -> Resolve the candidate parent and refuse to enumerate it unless it remains under the vault root.
