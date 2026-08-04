## Why

Tenant density on the alpha node is capped by the embedding runtime, not by the
architecture. The node has 7751 MiB, of which 815 MiB is in use by K3s, containerd,
metrics-server, and CoreDNS. Every remaining cell-shaped megabyte is a paying seat.

The bi-encoder is loaded through `sentence-transformers`, which pulls the full PyTorch
runtime into every cell. Measured on the identical model (`BAAI/bge-base-en-v1.5`):

| | torch | ONNX Runtime |
|---|---|---|
| import cost | 431 MiB | 79 MiB |
| warm resident | 854 MiB | 528 MiB |
| encode throughput | baseline | 17% faster |

The vectors are interchangeable: minimum cosine similarity 0.999977 across five texts
including edge cases, same 768 dimensions. That is roughly forty times tighter than the
fp16-versus-fp32 drift `_maybe_half` already documents as harmless for ranking, so this
is a runtime substitution rather than a model change.

326 MiB of warm resident per cell is the difference between four seats and six or more
on hardware already paid for. Hetzner has retired the CX line, so the node cannot be
resized and successors cost roughly four times as much for the same memory — density on
the existing node is the only cheap capacity available.

The alternative considered and rejected was a shared embedding service. It would collapse
six model copies into one, but it cannot offer zero cross-tenant exposure: tenant text
would cross a process boundary the isolation model currently forbids. ONNX reaches the
same density with the isolation boundary untouched, so the trade is unnecessary.

This change is deliberately sequenced before the first platform install. Composing the
deployment lock pins an exact release; swapping the runtime afterwards would mean a
second release, a second candidate, and recomposing the lock.

## What Changes

- Introduce an embedding-backend seam so the bi-encoder can be served by ONNX Runtime
  without changing any calling code. All 32 call sites across 12 modules already route
  through `get_model()` and `embed_texts()`.
- Make device selection backend-aware. `accel.py` is torch-shaped end to end; ONNX
  execution providers are not torch devices and need their own mapping.
- Keep the measured batch-size policy (8 on CPU, 32 on accelerators) behind the seam,
  because it currently reads `model.device`, which an ONNX session does not expose.
- Build the hosted image from an ONNX-based stage and prove the offline load with an
  `InferenceSession` rather than a `SentenceTransformer` construction. Retain the
  model weights the grant promises; drop the torch weights the runtime no longer reads.
- Make install readiness report the backend actually in use, so a torch-free hosted
  image is not reported as unhealthy by a check that hardcodes torch.
- Re-derive the cell memory envelope and the USER cell cap from the new measurement
  rather than carrying the torch-sized values forward.

Explicitly out of scope: the reranker and CLIP stay on `sentence-transformers`. The
hosted image already disables ranking and does not carry CLIP, so the hosted lane can
shed torch entirely while the `ml` and `cuda` images keep it.

## Capabilities

- `install-readiness` — the doctor's embeddings check becomes backend-aware.
- `hosted-tenant-cell` — the cell's embedding runtime and memory envelope.

## Impact

No re-embedding and no migration. The model, its 768 dimensions, and the stored vector
format are unchanged, and existing sidecars remain valid.

That safety is currently incidental rather than enforced: the sidecar `meta` table stores
only an instance identifier and generation tokens, and `SEMANTIC_UNIT_SCHEMA_VERSION`
invalidates only on a parser change. Nothing records which model produced a vector, so a
future model change would silently mix vector spaces. This change records an embedding
runtime fingerprint so that hazard is closed while the code is open, without altering
behaviour for the ONNX substitution itself.
