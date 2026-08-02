## Why

Two runtime settings chosen for capacity headroom would ship a hosted product that is
materially worse than the local one, and a third would make storage cost scale with
usage far faster than price.

The cell chart ships `workerLimit: 0`. In `hosted_runtime.py` that resolves
`workers_enabled = worker_count > 0` to false, which **sets** `EXOMEM_DISABLE_EMBEDDINGS`
and `EXOMEM_DISABLE_MEDIA_EXTRACTION`; `server_runtime.py` then reports embeddings,
file-watcher, and media as not-ready with `HOSTED_WORKER_LIMIT_ZERO`. A hosted cell
therefore offers capture and keyword recall but **no semantic search** — the capability
that distinguishes Exomem from a folder of markdown. Paying users would receive a
strictly lesser product than the free local runtime.

Separately, the durability worker takes a **complete portable archive every 30 minutes**
while the recovery bucket retains every object for 30 days with no thinning rule. That
is 1,440 full encrypted archives per cell coexisting — a 1,440× amplification with no
deduplication, since each archive is separately encrypted. Storage cost then scales with
vault size: break-even against the friends tier lands near a 150 MB average vault, and a
single user filling the 5 GiB entitlement would cost several times what the whole cohort
pays. The cadence also quiesces the cell each run against a two-minute objective, so it
manufactures up to 96 minutes of unavailability per user per day.

The 30-minute cadence was chosen for a one-hour RPO against volume loss. Hetzner Cloud
Volumes are replicated network storage; volume loss is the least likely way this system
loses data. Operator error, a bad migration, and account-level events are all more
probable, and none are helped by frequency — they are helped by retention depth.

## What Changes

- Enable the `embeddings` feature grant and raise the cell worker limit above zero so
  hosted cells run semantic recall, file watching, and media extraction.
- Resize the alpha node from CX33 to CX43 to hold six embedding-capable cells, and update
  the capacity contract's cost basis accordingly.
- Change the vault durability cadence from 30-minute full archives to daily, restating
  the recovery objective as 24 hours rather than one hour.
- Record the resulting economics honestly, including that the EUR 5 friends tier is
  approximately cost-neutral once the product is complete.

## Capabilities

### Modified Capabilities

- `hosted-alpha-operations`: cells must deliver the complete product surface, and the
  capacity/cost basis moves to the resized node.
- `hosted-durability`: the vault backup cadence and stated recovery objective change.

## Impact

- `infra/helm/cell/values.yaml` worker limit and feature grants; the rendered cell
  environment stops setting `EXOMEM_DISABLE_EMBEDDINGS`.
- `infra/terraform/foundation` server type, and the `server_type == "cx33"` validation.
- `infra/operations/private-alpha-capacity-v1.json` and its byte-identical chart copy:
  cost basis and the resource envelope behind the six-cell cap.
- The durability worker's schedule and the declared RPO in runbooks and specs.
- No change to tenant isolation, encryption, admission, or routing.
