## Status (2026-10-31): deferred, not merged

This change is parked on its branch. Its code and tests are complete except
for the real-machine proof (task 5.2). It is not merged.

- The personal case does not need it now. When the vault host's GPU is busy,
  speech jobs wait and then run on that GPU (`contain-asr-gpu-faults`). A
  remote worker only removes that wait.
- Exomem Cloud keeps media processing inside each cell
  (`add-cloud-multimodal-processing`), so tenant content never crosses a shared
  process. A shared GPU pool needs a different trust model. The "Revival
  triggers" section of `design.md` names it.

Revive this change when a revival trigger holds. Then rebase it, run the
independent review and do task 5.2.

## Why

Every media job runs on the machine that owns the vault. Speech recognition and
speaker diarisation need an accelerator, so a vault host without a free GPU
either decodes slowly on its CPU or takes the GPU away from other work on that
machine. An owner often has a second machine with an idle GPU, and an Exomem
Cloud cell has no GPU at all. Neither can help today: the only executor is a
local child process that the media supervisor starts and watches by process ID.

The media runtime already separates computation from publication. The child
computes an extraction and records it in `media_job_results` under the job's
claim fence. The long-lived parent then checks the sidecar and the original
against the claim and publishes through the governed write path. A remote
executor can take the child's place at that seam. The vault owner keeps the
queue, the originals, the claim fence and every vault write.

## What Changes

- A worker on another machine can lease a media job from the vault-owning
  service, fetch the job's original, compute the extraction and return the
  result. The service publishes the result through the existing parent path,
  with the same claim, sidecar and original checks as a local result.
- A lease has an expiry. The worker renews it with heartbeats. An expired lease
  returns the job to `pending` and releases its pin. A result from an expired
  or superseded lease is refused by the claim fence.
- The owner issues and revokes a worker credential from the host CLI. The
  credential allows only the worker routes, and only for jobs that the
  credential's worker holds.
- The service's per-vault media configuration says which media kinds go to remote workers and
  whether the local worker may still take them when no remote worker is
  present. While a remote worker is present, the local worker skips the kinds
  that the configuration routes away.
- Named speaker attribution stays on the vault owner. A remote worker returns
  anonymous speaker clusters with their voice embeddings. The service matches
  them against its local voice profiles. Voice profiles never leave the vault
  owner.
- A new `exomem media-worker` command runs the worker on the second machine.
- Media status shows each remote lease, its worker and its age, and when each
  worker was last seen.

## Capabilities

### New Capabilities

- `remote-media-workers`: leasing media jobs to remote executors, the worker
  credential and routes, byte delivery, result admission, lease expiry and
  routing between local and remote executors.

### Modified Capabilities

None. The parent publication path, the result payload validation, the claim
order and the voice-profile store keep their current contracts. This change
adds a second executor behind them.

## Impact

- Code: `media_jobs.py` (lease columns, remote claim, expiry sweep),
  `media_worker.py` (routing and the expiry sweep in the supervisor), a new
  `media_worker_remote.py` (service routes) and a new worker client module, the
  `exomem` CLI, and media status.
- Depends on the claim order, pinning and hydration accounting of
  `add-cloud-media-store` (design D3 amendment M4 and D12). A remote lease holds
  the same pin and hydration slot as a local claim.
- Security: a new credential type and new routes on the public listener. The
  routes serve original bytes, so they pass the same release gate as an
  original download.
- No change to the MCP tool surface, so no client plugin refresh.
