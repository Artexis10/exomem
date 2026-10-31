## Context

The media runtime keeps a durable job ledger (`media_jobs.py`). A supervisor in
the service (`media_worker.py`) starts one local child process at a time. The
child claims a job, computes the extraction and records it with
`record_result` under the job's `claim_revision`. The parent drains the result
and publishes it with `_publish_parent_result`. Before it writes, the parent
checks that the claim revision, the sidecar hash, the evidence version and the
binary identity still match. Every vault write happens in the parent, under the
mutation guard.

The child is the only executor. Its liveness is a process ID, and crash
recovery (`recover_interrupted`) works only for a local process. The claim
order, pinning and hydration slots come from `add-cloud-media-store` (design D3
amendment M4, D12). The GPU-busy defer and `skip_media_types` come from
`contain-asr-gpu-faults`.

## Goals / Non-Goals

**Goals**

- A worker on another machine can run media jobs for a vault. The vault owner
  keeps the queue, the originals, the claim fence and every vault write.
- A lost worker never strands a job, never leaks a pin and never lands a stale
  result.
- The owner decides by configuration which media kinds go remote, and whether
  the local worker may still take them.
- Voice profiles stay on the vault owner.

**Non-Goals**

- Resuming a remote job from partial progress. A lost lease restarts the job.
- A pool of shared workers across vaults or cells. The protocol allows it
  later; this change serves one vault per worker credential.
- Changing the MCP tool surface, the result payload contract of local jobs, or
  the claim order.

## Decisions

### D1. The remote worker replaces the child at the result seam

A remote worker does what the local child does between claim and
`record_result`, and nothing else. The service records the remote result with
`record_result` and publishes it with the existing parent path. One
publication path means one set of preconditions for both executors.

### D2. The worker pulls over HTTPS

The worker calls the service. It never accepts connections, so it works behind
NAT and needs no inbound port. A claim call long-polls for up to 25 seconds and
returns `204` when no job is eligible. The routes live on the service's public
listener under `/media-worker/v1/`.

| Route | Purpose |
|---|---|
| `POST /media-worker/v1/claim` | Lease the next eligible job, or `204` |
| `POST /media-worker/v1/leases/{job}/heartbeat` | Extend the lease |
| `GET /media-worker/v1/leases/{job}/original` | Stream the original bytes, with `Range` |
| `POST /media-worker/v1/leases/{job}/result` | Hand in the extraction or failure |
| `POST /media-worker/v1/leases/{job}/release` | Give the job back without a result |

Each call names the `claim_revision` that the claim returned.

### D3. A lease is a claim with an expiry

A remote claim is the existing `claim_next` transition, so it increments
`attempts` and `claim_revision`, sets the pin and takes a hydration slot when
the original needs one. A lease row beside the job stores the worker's
identity, the lease expiry and the snapshot from D4. `claim_revision` stays the
fencing token: a heartbeat, a byte read or a result with another revision is
refused with `409 MEDIA_LEASE_LOST`.

The lease length and the heartbeat interval come from configuration (D7). The
supervisor sweeps expired leases on its existing loop. The sweep moves an
expired job from `running` to `pending`, which releases the pin through the
existing `jobs_release_pin` trigger. An expiry counts against the job's existing
timeout-stop budget, so a job that keeps losing its worker becomes `blocked`
with `over_budget` instead of looping.

### D4. The service takes the snapshot at grant

A local child reads the sidecar and the original itself and reports
`sidecar_before_hash`, the evidence version and the binary identity. A remote
worker cannot read the vault, and the service must not trust it to report them.
The service computes these values at grant and stores them in the lease row.
The language pin and, for `reprocess`, the digest of the extraction it
replaces are stored the same way. At result time the service passes the stored
snapshot to `record_result`. The worker supplies only the payload.

### D5. Bytes come from the service, from a held read handle

The worker reads the original through the lease route. The service streams it
from a held read handle, with no private copy (D12 of `add-cloud-media-store`).
A store-backed original hydrates under the job's pin and hydration slot, as a
local claim would. The claim response carries the original's size and SHA-256,
and the worker checks both before it computes. A worker never receives a media
store credential.

The route applies the same release gate as an original download. A job whose
original the gate does not release is not eligible for a remote claim, so it
stays with the local worker.

### D6. A worker credential is owner-issued, per worker, and narrow

The owner runs `exomem media-worker credential issue --name <name>` on the
vault host. The command writes the token to a file with owner-only permissions
and stores only its hash in machine-local state. `revoke` and `list` complete
the set. The credential admits the five worker routes and nothing else. A
heartbeat, read, result or release also requires that the credential's worker
holds the lease.

OAuth does not fit a headless daemon, and the local ingress grant is bound to
the loopback listener. A dedicated credential keeps a leaked worker token from
reaching notes, tools or uploads.

### D7. Routing is configuration

The service's per-vault media configuration, the machine-local file that
already holds the media store policy, gains a `remote_workers` block:

- `media_types`: the media kinds that remote workers may lease.
- `local_fallback`: whether the local worker still takes those kinds when no
  remote worker is present.
- `lease_seconds`, `heartbeat_seconds` and `presence_seconds`.

A remote worker is present when its credential made a claim or heartbeat within
`presence_seconds`. While one is present, or whenever `local_fallback` is
false, the local child claims with `skip_media_types` set to the routed kinds.
The claim route admits only routed kinds that the worker's declared
capabilities cover. Remote claims follow the same claim order and hydration cap
as local claims. The defaults route nothing, so a vault without the block
behaves as today.

### D8. The worker returns structure; the service renders

A remote extraction payload carries the ASR segments with their times and
`asr` facts. When diarisation ran, it also carries the raw speaker turns and one
voice embedding per raw cluster. The service matches the clusters against its
local voice-profile store, renders the transcript text with the same functions
as the local path, and records the result. Voice profiles therefore never leave
the vault owner, and a remote transcript renders exactly as a local one. The
service drops the embeddings after matching and stores none of them.

### D9. The vault's policy travels with the job

The claim request carries the protocol version and the worker's capabilities:
media kinds, engines, device and whether diarisation is available. The service
refuses an unsupported protocol version with
`426 MEDIA_WORKER_PROTOCOL_UNSUPPORTED`. The claim response carries everything
the computation depends on that the vault decides: the language pin and mode,
the reprocess flag, timestamps and the ASR decoding policy. The worker applies
the job's settings, not its own configuration. The sidecar records the engine
and the `asr_*` facts that the worker reports, as it does for a local job.

## Risks / Trade-offs

- **New routes on the public listener.** They refuse every request without a
  worker credential and serve original bytes only for a held lease through the
  release gate. Tokens are stored hashed and are revocable.
- **Large originals over the public path.** The original streams in the
  response direction, with `Range` for resume. Results stay within the existing
  payload cap.
- **Lost leases cost compute.** A lost lease restarts the job from the start.
  The timeout-stop budget bounds the waste.
- **Two executors share one queue.** Both use `claim_next` under
  `BEGIN IMMEDIATE`, so a job has one holder. Routing only narrows which kinds
  each executor asks for.

## Migration Plan

The ledger gains a lease table, created by migration on start. With no
`remote_workers` block, routing is empty and the routes refuse every
claim, so an upgrade changes nothing until the owner configures a worker and
issues a credential. Rollback: remove the block and revoke the credentials. The
expiry sweep then returns any leased job to `pending`.
