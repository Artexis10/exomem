# Proposal

## Why

OpenSpec-only pull requests cannot change the installed wheel or its journey inputs.
The current rule still repeats the installed journey and can block those changes on an unrelated runtime failure.

## What Changes

- Omit the installed journey only when every pull-request change is inside `openspec/`.
- Compare the actual merge checkout with its first parent, including deletions and both sides of renames.
- Run the journey for other changes, uncertain comparisons, and every main, scheduled, and manual run.
- Report omission explicitly without claiming runtime acceptance.
- Keep the remaining CI jobs and full release evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `product-e2e`: Scope pull-request execution to changes that can affect the installed journey.

## Impact

The change affects the installed journey job, its existing CI contract tests, and the canonical coverage requirement.
It uses Git and the existing workflow. It adds no dependency or runtime change.
Optional model and media lanes retain their existing scheduled or opt-in behavior and explicit availability reports.
Runtime failures and release acceptance remain open until their own evidence passes.
