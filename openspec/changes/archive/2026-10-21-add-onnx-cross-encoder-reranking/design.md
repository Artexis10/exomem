# Design

## Context

See `proposal.md` for the requested behavior. Sentence Transformers 5.5.1 provides a CrossEncoder ONNX backend and accepts an ONNX execution provider and an export flag. The current Exomem loader already provides lazy singleton loading, offline-first cache behavior, and soft failure at the retrieval boundary.

## Goals / Non-Goals

**Goals:**

- Add one explicit runtime choice for the existing reranker.
- Reuse Sentence Transformers' supported CrossEncoder implementation and prediction API.
- Preserve PyTorch as the default and keep the lean embedding-only ONNX profile unchanged.
- Prevent export work from entering a user retrieval request.

**Non-Goals:**

- Change automatic reranking, candidate retrieval, candidate depth, or activation behavior.
- Change model weights, quantize the model, or claim a quality, memory, or latency gain.
- Enable ONNX in a live service or change its environment settings.

## Decisions

- Use `EXOMEM_RERANKER_BACKEND=torch|onnx`, defaulting to `torch`. This keeps the new choice local to reranking and avoids coupling it to `EXOMEM_EMBED_BACKEND`, which selects the bi-encoder.
- Add a separate `reranker-onnx` optional extra containing Sentence Transformers' maintained ONNX dependencies. Do not add these packages to `embeddings-onnx`, whose torch-free contract is used by hosted embedding-only installs.
- For ONNX, pass `export=False` and the provider selected by Exomem's existing accelerator policy. Use CPU when that policy selects CPU or MPS; use CUDA only when it explicitly selects CUDA. Do not ask ONNX Runtime to auto-select a stronger provider, which could bypass Exomem's CPU-default and GPU-headroom policy.
- Keep the model load behind the existing lazy singleton and model guard. Leave model cache behavior, scorer inputs, score parsing, and soft-failure handling intact.
- Add the effective runtime to retrieval explanation metadata while retaining the existing CrossEncoder family marker.

## Risks / Trade-offs

- [Risk] ONNX Runtime may trade memory and startup cost for faster inference on a given CPU. → Mitigation: keep the runtime opt-in; the synthetic evaluation found lower query latency but higher RSS and model-load time, with no reliable quality gain.
- [Risk] A model repository may lack an ONNX export. → Mitigation: disable runtime export and soft-fail reranking while preserving the unre-ranked results.
- [Risk] A CUDA provider may be missing from the installed ONNX Runtime build. → Mitigation: preserve the existing rerank failure path and report the effective runtime only after scoring runs.

## Migration Plan

No migration is required. Existing installs and environment settings keep the PyTorch backend. Operators may install the separate extra and select ONNX in a later, separately authorized configuration change.
