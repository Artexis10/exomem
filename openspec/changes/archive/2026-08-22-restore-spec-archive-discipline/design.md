## Context

On 2026-08-20 the repository has 137 active changes before this change, 39 archived changes, and 36 canonical capability specs. Eighty active changes have every task checked, while several other shipped changes retain open desk-side, deployment, or archive tasks. CI runs strict validation only over `openspec/specs/`; active deltas are therefore outside the required gate and do not become canonical requirements until archive sync.

The accepted August 16 audit established the governing constraints: archive is a spec merge, not a directory move; task checkboxes drift both ways; created-date order resolves most dependent `MODIFIED` deltas; and every tranche must keep strict validation green. A disposable replay against `origin/main` confirmed the current shape: 68 of the 80 task-complete changes archive mechanically and the final combined contract remains strict-valid; 12 refuse safely because their deltas are stale, duplicate already-canonical requirements, or depend on an unarchived base.

## Goals / Non-Goals

**Goals:**

- Bring shipped requirements into canonical specs without losing newer scenarios.
- Preserve the complete proposal/design/spec/task history under dated archive paths.
- Make both active deltas and canonical specs part of required CI.
- Add a cheap deterministic tripwire for the unambiguous subset of archive debt: active changes whose task lists are fully checked.
- Record the evidence-first closure rule where repository agents load it.

**Non-Goals:**

- Declare genuinely unfinished product work complete.
- Infer implementation solely from task checkboxes or automatically archive changes.
- Rewrite OpenSpec itself or add a package/runtime dependency.
- Fold unrelated programme follow-ups into the archive repair.

## Decisions

### Archive through OpenSpec, never with a bare move

Each selected change is applied with `openspec archive`, so its delta is merged into `openspec/specs/` before the change directory moves. A bare filesystem move was rejected because it preserves history but silently strands the contract.

### Select from implementation evidence; use task completion only as a tripwire

Archive eligibility is established from shipped code, tests, and merged delivery evidence. A fully checked task list is sufficient to demand human attention, not sufficient to prove shipment; open checkboxes may remain on already-shipped work. The committed audit therefore detects only fully checked active changes and tells the maintainer to archive or correct the task state. It does not archive or classify partially checked changes.

### Order by creation date, then explicit dependency

The batch is processed oldest first. Same-date or known prerequisite relationships override lexical ordering—for example the base vector-backend contract precedes the opt-in change. Strict validation runs after bounded tranches so a bad merge is localized.

### Preserve the current superset when repairing stale deltas

When OpenSpec refuses a `MODIFIED` block because the canonical requirement has newer scenarios, the repaired delta starts from the full current canonical requirement and applies only the older change's still-relevant semantics. When an `ADDED` requirement is already canonical and equivalent, the archive skips that already-synchronized delta only after an exact content comparison. Missing-base cases archive the implemented base first or convert the delta only when the resulting canonical history remains complete.

### Gate the complete contract surface

The required CI job runs strict validation over all active changes and canonical specs, followed by the archive-debt audit. This makes malformed active contracts visible immediately while the backlog tripwire prevents a new fully-complete pile from accumulating.

### Close changes in the delivery that finishes them

Repository agent instructions require synchronization and archive in the same delivery once implementation is genuinely complete. A change stays active only when named work remains; “we will archive later” is not a valid terminal state.

## Risks / Trade-offs

- **Large rename-heavy diff** → Process in reviewable tranches, inspect `--stat` and rename detection, and keep all changes within `openspec/` except the small gate/test/instruction edits.
- **Stale `MODIFIED` delta drops newer scenarios** → Treat OpenSpec's refusal as a hard stop; repair from the current full requirement and compare the resulting canonical block.
- **Checkbox tripwire produces a false positive** → It reports names and never mutates; the maintainer either archives from evidence or corrects inaccurate ticks.
- **Previously unvalidated active deltas break CI** → Establish the `--all --strict` baseline before the migration and require it after every tranche.
- **Rollback is noisy** → The entire migration is one reviewable commit series and all history is retained; reverting restores both active paths and the previous canonical specs.

## Migration Plan

1. Capture the current active/archive/spec census and strict-validation baseline.
2. Replay the archive set in a disposable worktree to enumerate safe merges and refusals.
3. Archive mechanically safe shipped changes in dated tranches.
4. Repair each refused delta from the current canonical superset, then archive it and its implemented prerequisites.
5. Add the archive-debt audit, focused tests, complete-surface CI validation, and repository delivery rule.
6. Run strict OpenSpec validation, focused tests, the lean suite, lint, and diff checks.
7. Archive this change itself so `openspec-record-discipline` becomes canonical.

## Open Questions

None. The remaining classification work is evidence gathering, not a product decision.
