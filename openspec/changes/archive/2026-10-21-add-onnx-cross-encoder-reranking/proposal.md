# Proposal

## Why

Exomem already reranks an explicitly selected candidate set with a CrossEncoder, but it only loads that scorer through PyTorch. Sentence Transformers supports an ONNX CrossEncoder backend, so operators can choose ONNX without replacing Exomem's scorer or changing its retrieval policy.

## What Changes

- Add an opt-in `EXOMEM_RERANKER_BACKEND` setting with `torch` as the default and `onnx` as the alternate.
- Add a separate `reranker-onnx` extra so the lean BGE-M3 `embeddings-onnx` profile stays torch-free.
- Load only an existing ONNX model artifact; do not export weights on a user request.
- Preserve explicit reranking, CPU auto-rerank policy, candidate caps, soft failure, and the default PyTorch behavior.
- Report the selected scorer runtime in retrieval explanations.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `retrieval-explainability`: Report the effective CrossEncoder runtime separately from its model family.
- `find-recall-efficiency`: Add an explicit CrossEncoder runtime choice without changing reranking policy or candidate bounds.

## Impact

The change affects reranker loading, retrieval explanation metadata, optional dependencies, and configuration documentation. It does not change candidate retrieval, reranking defaults, activation, service settings, or model weights. A matched 40-query synthetic evaluation found identical ONNX and PyTorch rankings, with no reliable quality gain over no reranking. ONNX lowered measured CPU query latency but increased peak RSS and model-load time, so it remains opt-in and this result does not justify language-based auto-selection.
