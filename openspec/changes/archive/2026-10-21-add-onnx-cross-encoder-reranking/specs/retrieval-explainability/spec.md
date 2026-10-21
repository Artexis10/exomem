# Spec Delta

## ADDED Requirements

### Requirement: Retrieval Explanation Identifies The Cross-Encoder Runtime
When a CrossEncoder rerank runs, the retrieval profile SHALL report the effective runtime backend separately from the CrossEncoder model family.

#### Scenario: PyTorch rerank reports its runtime
- **WHEN** an explained request reranks with PyTorch
- **THEN** the rerank profile reports `torch` as its runtime backend

#### Scenario: ONNX rerank reports its runtime
- **WHEN** an explained request reranks with ONNX
- **THEN** the rerank profile reports `onnx` as its runtime backend

#### Scenario: Rerank does not run
- **WHEN** reranking is skipped, unavailable, or fails
- **THEN** the profile does not report an effective runtime as if scoring had occurred
