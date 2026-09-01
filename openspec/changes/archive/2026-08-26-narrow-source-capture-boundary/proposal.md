# Proposal: narrow-source-capture-boundary

## Why

`ingest-attachments-as-sources` gave `capture_source` the same file-handle lane
`preserve_artifacts` has, and its spec says so: handles "SHALL be staged through
the same bounded, safe server-side retrieval used by `preserve_artifacts`, with
identical treatment of hostile URLs, redirect limits, timeouts, item counts, and
byte caps." Every property in that list holds. One that is not in the list does
not: the boundary the retrieval runs under.

`preserve_artifacts` is a member of `writer_lease._NARROW_BOUNDARY_COMMANDS`, so
its outer guard revalidates writer authority without holding the vault mutation
lock, and its leaf takes the lock once per committed artifact.
`client_artifacts.capture_source_artifacts` was written to the same shape — it
stages every handle first, then wraps each `add` in its own
`mutation_guard` — but `capture_source` is not a member, so the wide outer
boundary wraps the whole leaf. Measured on `origin/main`, a one-file Source
capture records:

```
[('mutation-boundary', 'capture_source'),
 ('fetch', 'f1'),
 ('mutation-boundary', 'capture_source_artifacts_commit')]
```

The lock is taken, then the network is used, then the lock is taken again inside
it. A full batch is eight fetches against a sixty-second batch deadline, and for
that whole window every other writer to the vault is blocked on a remote server's
latency. That is the precise failure `shorten-mutation-critical-section` exists
to prevent, reintroduced through a command it did not know about.

Adding the name to `_NARROW_BOUNDARY_COMMANDS` is not the fix. `capture_source`
carries two lanes under one name: the file lane, whose leaf self-guards, and the
text lane, which routes to `add` — and `add` acquires no guard of its own. Moving
the boundary by command name would leave every text capture unguarded. The
boundary has to follow the invocation, which is the shape `process_media` and
`manage_memory_file` already use in the same block: both select on `kwargs`, not
on the command name.

This was filed as task 10.1 of `ingest-attachments-as-sources` and deleted when
that change archived, because a finding written as a checkbox has no valid
resting state — open blocks the archive, ticked claims a fix that does not exist.
This change is where it goes instead.

## What Changes

- `writer_lease` selects the narrow boundary for `capture_source` when and only
  when the invocation carries `files`, alongside the two per-invocation
  predicates already there. The text lane keeps the wide boundary it depends on.
- `EXOMEM_WIDE_MUTATION_BOUNDARY` restores the wide boundary for the file lane,
  as it does for every other narrowed lane.
- No change to what is stored, where, or to any outcome payload. This moves only
  when the vault mutation lock is held.

## Capabilities

### Modified Capabilities

- `client-artifact-preservation` — the retrieval requirement states the boundary
  property it already implies, and states it for every command that stages client
  file handles rather than only for the Evidence one.

## Impact

- `src/exomem/writer_lease.py` — one per-invocation predicate.
- `tests/test_attachment_source_ingestion.py` — boundary ordering, the batch
  case, the text-lane control, and the kill switch.
- No tool-surface change: no descriptor, docstring, or parameter moves, so
  `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json`
  stay byte-identical and no hosted plugin fingerprint moves.
