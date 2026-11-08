# Design: one-step import

## Context

`adoption_studio` keeps each run as a stored object with a phase (`selecting`, `planned`, `applying`, `applied`, `partial`, `failed`, `done`, `cancelled`) and opaque cursors of kind `plan`, `apply` and `after`. A page of a large folder is a run of its own. A used `after` cursor holds the id of the run that it opened, so the pages form a chain. `apply` already recovers an interrupted chunk: it saves an apply invocation before it publishes, and the writer lease admits an exact retry of an interrupted `apply` under its original identity.

## Goals

- One call, repeated with the continuation it returns, files a staged folder of any size.
- An interrupted or retried call never files a source twice and never reports work it did not do.
- The step path stays as it is, so an agent that wants control keeps it.

## Non-goals

- A new way to write. `file` calls `start`, `select`, `plan`, `apply` and `finish`.
- Changing what a Source, artifact or note contains.

## Decisions

### D1. `file` is a driver over the step functions, chosen from saved state

Each call finds the newest page. A `continuation` names the run that issued it. A path alone resumes the newest run of that path with a step left, else the newest run, so a retried first call makes no second run, two drivers that take turns file each source once, and a finished import answers `done` again. A path alone with another collection than the run's is refused with the continuation's refusal, because the scan keeps the label of its first page. The call then runs the step that the saved phase calls for:

- `selecting` with no selection: `select` the whole stage. A page with nothing eligible is cancelled, so the scan moves to the next page.
- `selecting` with a selection: `plan`.
- `planned` or `applying`: `apply` the stored plan with its cursor.
- `partial` or `failed` with failed files that no retry has run: `apply` with `retry_failed`. The marker is `apply`'s own `scope`, which a full `apply` leaves empty and a retry sets to the files it retried, so the retry runs once.
- `applied` or `partial`: `finish`.
- `failed` after its retry: `cancel`. `finish` refuses a failed page and `plan` refuses a later page while it stays open. The failed outcomes stay on the page, so the counts and the residual keep them.
- `done` or `cancelled` with an open `after` cursor: `start` the next page.

The state is the run object, never the arguments, so a call carries on from wherever the run stands, including a run that the step path began.

A call ends after `apply` or its retry, after `finish`, after a `plan` that leaves the page `selecting`, before a `start` once it has run a step, and when the budget cannot cover the delivery reserve. One page costs two or three calls.

### D2. The reply is small and claims only what the run records

`filing` is `working`, `stopped` or `done`. The envelope key `state` is reserved, so `filing` carries it. `summary` is built from three counts, filed, failed and left, and nothing else. `progress` counts the whole import over every page, and carries the newest page's position (`page`: number, phase, files planned) so that a caller can see a call that moved the page without moving a count. Only `finish` verifies filed text against its Source page, so `done` is the only state that carries `filed`. It holds the stage's residual, and under `last_finished_page`, with the page number, the coverage, first question, route and indexing of the last `finish`, which describe that page alone. The reply names its `collection`.

`stopped` carries `stop` with a code and a reason. A space stop (`apply_stop`) names the bytes needed and free. An error after a run exists stops the reply instead of raising, so work already committed stays reported. An error before any run exists raises, because it is a bad argument.

`person` lists what only the person can do: free space, review the residual, discard, or report an unresolved recovery to the operator. `continuation` is a fresh `file` cursor on every `working` reply and on a space stop. The writer lease replays identical calls for 600 s, so a stateless call that reused one continuation would replay a stale reply.

The generic outcome sentence is `updated`, not `saved`: a call may only finish a page or stop for space.

### D3. Recovery reuses the apply identity

`apply` saves its invocation under the mutation identity before it publishes. A reconciling `file` call looks for an invocation under the identity that it carries, on its page or an earlier page. When it finds one, it repeats that `apply` exactly and returns. When it finds none, the attempt published nothing, and the call runs every step except `apply`. A different identity that meets an interrupted chunk is refused by `apply` as today, and the reply stops with that code.

Both apply-only gates in `writer_lease.py` take `file` in their action set: the one that admits a fast acknowledgement and the one that passes `reconcile_abandoned`. The change needs nothing else there. A reconciled call whose recovery stays unresolved stops with `IMPORT_RECOVERY_UNRESOLVED` and tells the person to report it to the operator.

### D4. The CLI files each landed window

A staged window lands on the cell as a separate import. `exomem cloud import` calls `file` for each landed window while it waits for the next, and again for every unfiled window at the end. State is in memory: a rerun seeds it from landed windows that still report unfiled members and whose filing the status does not report done (`filing_done`). A `stopped` reply, or an error reply, ends filing for that window with its continuation kept, and the command reports the cell's code and reason. The command repeats the exact call once after an unknown outcome, and after a wait while the acknowledgement is pending, because the cell reconciles only under the same call. Only a refusal of the action itself is an older cell: `INVALID_MODE`, or a tool error from a cell whose published `import_memory` schema lists kinds without `file`. That raises `FilingUnavailable`, and the command leaves the windows to the step path for the rest of the run. A window stalls when five replies move no count and leave the page's position unchanged. The CLI reads the reply's states and counts from `adoption_run`. `--no-file` leaves every window for the agent.

### D5. Guidance names one call

The `exomem import` closing line names `import_memory` `file`, with the path. The scaffold references and the `exomem-ingest` skill describe the repeat-until-not-working loop first and the step path as the manual alternative. A hidden-file hint names `start` with `include_hidden`, because `file` scans without hidden files.

## Risks

- **A retry files twice.** D1 resumes from saved state and D3 repeats only the identity's own `apply`, so the existing `already-applied` outcome covers a chunk that landed before the crash.
- **A reply overclaims.** D2 builds the sentence from counts, and `done` alone reports verified coverage.
- **A stale continuation.** D2 issues a fresh cursor per reply, and a continuation from another path or collection is refused with `INVALID_CONTINUATION`.
