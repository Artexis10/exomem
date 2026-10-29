## Context

`batch_atomic_write` stages each planned file in a private workspace, then
replaces destinations one at a time. Before each replacement it revalidates
every pending stage and every already installed destination. For a note this
costs little. For a binary stage, `_WorkspaceArtifact.recheck` hashes the whole
file through its descriptor. For an installed binary, `_BatchArtifactGuard`
holds a content `PathGuard` that hashes the whole file through its path. A
binary is therefore hashed once per remaining replacement as a stage, once per
later replacement as a destination, plus fixed passes for capture and the
post-loop checks: W + 9 in total.

The copy from stage to destination (`held_fs.publish_bytes` with
`Chunks(_descriptor_chunks)`) does not hash. Content safety relied on the
recheck before the copy and the destination hash after it. A stage changed
between those two points is installed, then the destination hash fails after
the destination is recorded as replaced but before it is bound, so rollback
cannot remove it.

Only `PreparedBinaryContent` reaches the stream stage. Its producers are file
addition, artifact preservation, scene frames, and adoption filing. The media
store publishes hydrated originals through `held_fs.publish_bytes` directly and
is not affected.

## Goals / Non-Goals

**Goals:**
- Read and hash a binary's bytes at the two points where they change hands:
  into the stage, and into the destination.
- Never install bytes that differ from the planned digest.
- Keep peak memory independent of binary size on every path.

**Non-Goals:**
- Changing note (text) stage or destination checks.
- Changing restore of pre-existing destinations, which are text snapshots held
  in memory by contract.
- A Windows change witness for installed destinations through a held reopen.
  Windows keeps the content check for installed binaries.

## Decisions

### 1. Verify the copy, not only the source

`replace_artifact` wraps the stage's chunk iterator in a generator that hashes
what it yields and raises `PATH_GUARD_CONTENT` after the last chunk when the
digest differs from the artifact's planned digest. `publish_bytes` removes its
temporary when the source raises, so nothing is installed. This applies to
every artifact: one rule, and a note's hash is negligible. A retried publish
opens a fresh iterator and hashes again.

The legacy route without a vault root renames the stage itself and has no
copy. That route keeps the full content check immediately before the rename.

### 2. A trusted change witness for stream stages

After `create_stream_artifact` has hashed the stream while copying it into the
stage, it records the stage's version: `HeldFile.version()` for a held stage
(Windows reads ChangeTime through the handle), or `stat_generation(fstat)` for
an unheld stage when `STAT_GENERATION_TRUSTED`. Otherwise it records nothing.
`_WorkspaceArtifact.recheck` compares the recorded version instead of hashing
when one exists. Text stages record no version.

Alternative rejected: the `generation` leaf policy for everything. It works by
path lstat, Windows `st_ctime` is not a change time, a fresh stage is never
outside the timestamp granularity window, and it would not verify the bytes
users receive. Decision 1 supplies that verification.

### 3. Bind installed binaries by generation where it is trusted

`replace_artifact` already reopens the installed destination through the held
filesystem and checks its identity against the published identity. It also
reads that handle's version and hands it to the batch. When
`STAT_GENERATION_TRUSTED`, `_BatchArtifactGuard` binds that destination with a
`generation` `PathGuard` built from the handle's size, modification time and
change time, and keeps the planned digest as its own field for rollback.
Elsewhere, and in the post-error binding path, it keeps the content policy.

### 4. Remove duplicate rechecks

`_BatchArtifactGuard.capture` rechecked its guard immediately after
`PathGuard.capture` had done so. The batch also rechecked every installed
destination twice after the loop. One post-loop recheck remains, after the
directory census, as the last check before commit.

### 5. Stream the remaining exact checks

Rollback removal of a batch-created destination keeps its exact content check,
which the requirement mandates, but hashes the held descriptor in chunks.
Post-error binding does the same. The vocabulary gate already refuses a stage
larger than the classifier's image bound before reading it, so no binary path
holds a whole file in memory.

## Risks / Trade-offs

- [A same-size rewrite within one timestamp tick of the last witness is not
  detected] → The requirement's same-owner exclusion names it. Stages live in a
  private owner-only workspace under random names; publication temporaries use
  random names. Kernels with fine-grained timestamps after a stat narrow the
  window further. Bytes copied into a destination are still verified.
- [Windows keeps hashing installed binaries per replacement] → Remote cells run
  Linux. Stages on Windows still use the held handle's witness.
- [A filesystem with coarse timestamps widens the tick] → The witness also
  compares size and identity; a coarse-timestamp cell would be a reason to
  revisit this decision.
