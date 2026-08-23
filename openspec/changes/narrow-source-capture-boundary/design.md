## Context

`writer_lease.invoke_command` chooses between two outer guards for every
mutation. The wide one, `mutation_guard`, holds the vault mutation lock across
the whole leaf. The narrow one, `writer_authority_guard`, revalidates writer
authority and leaves the lock to the leaf, which is correct only where the leaf
takes the lock itself.

Selection is by three tests today, and two of them are already per-invocation:

```python
narrow_media_commit = command.name == "process_media" and kwargs.get(
    "operation", "process"
) in {"process", "retry"}
narrow_tier2_file_commit = (
    command.name == "manage_memory_file"
    and kwargs.get("operation", "list") in {"create", "append"}
    and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
)
```

`manage_memory_file` is the precedent that matters: one command whose operations
differ in whether their leaves self-guard, resolved by reading `kwargs` rather
than by splitting the command. `capture_source` is the same situation reached
from a different direction — not two operations, but two argument shapes.

## Goals / Non-Goals

**Goals.** Retrieval for a Source capture completes before the vault mutation
lock is taken, matching the Evidence lane. The text lane keeps the wide boundary.
The kill switch works for both.

**Non-Goals.** No change to lane routing, storage, receipts, or outcomes. No
change to the tool surface. No revisiting which commands belong in
`_NARROW_BOUNDARY_COMMANDS` — this change adds no member to that frozenset.

## Decisions

**Per-invocation, not per-command.** `kwargs.get("files")` is the discriminator
because it is the same value `commands.op_capture_source` branches on one frame
later: `if files:` routes to `client_artifacts.capture_source_artifacts`, and
everything else routes to `op_add`. Reading the same value in the same way keeps
the boundary decision and the routing decision from drifting apart. `bool()`
rather than a presence test, because an explicit empty list routes to the text
lane and must keep the wide guard.

**Not a frozenset member.** Membership is a claim that every routing of the
command self-guards. That claim is true of `preserve_artifacts` and false of
`capture_source`. Encoding a conditional truth as membership would be wrong even
if the conditional happened to hold for today's callers.

**The kill switch is on the new predicate, not on the aggregate.** Matching
`narrow_tier2_file_commit`, which carries its own env check;
`narrow_media_commit` deliberately does not, and this change does not touch it.

## Risks / Trade-offs

**The window between staging and commit widens.** Under the wide boundary the
lock was already held when the first commit began; under the narrow one it is
acquired per artifact, so another writer can interleave between two artifacts of
one batch. That is the same interleaving `preserve_artifacts` has had since
`shorten-mutation-critical-section`, and it is the intended trade: a batch is not
atomic, each artifact is. Append-only collision refusal is what protects a
destination, and it is inside the per-commit guard.

**A stale predicate if the routing branch moves.** If `op_capture_source` ever
routes on something other than `files`, this predicate silently keeps the old
answer and the boundary is wrong again. Mitigated by the ordering tests, which
observe the real guard through `LeaseManager.mutation_guard` rather than
asserting frozenset membership — the shape of the existing
`test_preserve_artifacts_uses_the_narrow_replay_boundary` pin, which would not
have caught this defect and does not catch a regression of it.

**Text-lane control is load-bearing.** `test_a_text_capture_still_runs_under_the_wide_boundary`
passes both before and after this change, by design: it is the guard against a
later simplification that moves `capture_source` into the frozenset and drops the
predicate.
