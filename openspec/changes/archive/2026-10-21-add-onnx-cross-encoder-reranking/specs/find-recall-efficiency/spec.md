# Spec Delta

## ADDED Requirements

### Requirement: Reranker Runtime Selection Is Explicit
The reranker SHALL use PyTorch by default and SHALL accept only the configured `torch` or `onnx` runtime. Selecting ONNX SHALL use an already-exported model and SHALL NOT export model weights during a retrieval request.

#### Scenario: Default runtime remains PyTorch
- **WHEN** no CrossEncoder runtime is configured
- **THEN** explicit reranking uses the PyTorch backend as before

#### Scenario: ONNX runtime uses a published export
- **WHEN** the operator selects ONNX and requests reranking
- **THEN** Exomem loads an existing ONNX model artifact without exporting model weights

#### Scenario: ONNX runtime is unavailable
- **WHEN** ONNX dependencies or a compatible exported model are unavailable
- **THEN** the reranker soft-fails and recall returns its existing non-reranked order with an unavailable or failed outcome

#### Scenario: Invalid runtime configuration
- **WHEN** the configured runtime is not `torch` or `onnx`
- **THEN** reranking soft-fails and recall returns its existing non-reranked order

### Requirement: Cross-Encoder Runtime Does Not Change Retrieval Policy
Selecting a CrossEncoder runtime SHALL NOT change whether reranking runs, the candidate limit, or the candidate ordering presented to the scorer.

#### Scenario: Explicit rerank remains bounded
- **WHEN** a caller explicitly requests reranking with either runtime
- **THEN** Exomem scores the same bounded candidate prefix used by the existing reranking path

#### Scenario: CPU auto-rerank remains disabled by policy
- **WHEN** auto-reranking is disallowed by the current compute policy
- **THEN** selecting ONNX does not cause reranking to run automatically
