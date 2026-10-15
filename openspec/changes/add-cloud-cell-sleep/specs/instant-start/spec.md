## ADDED Requirements

### Requirement: Read warm does not wait for write prerequisites

On a Cloud cell, the read warm SHALL run beside the write prerequisites, not behind them. The read warm is the lexical caches, then the recall encoder preload. The write prerequisites are the graph handoff and the semantic corpus. The `lexical` and `embeddings` readiness components SHALL become ready without waiting for either write prerequisite. They SHALL keep their order: `lexical` before `embeddings`.

Governed writes SHALL still wait for the graph handoff and the semantic corpus.

A cell SHALL read its semantic corpus state from its volume when that state is current. It SHALL NOT rebuild the state on every start.

#### Scenario: A woken cell answers semantic recall before its graph handoff finishes

- **WHEN** a Cloud cell starts on a vault with a complete, published vector sidecar, and its graph handoff is still running
- **THEN** a hybrid recall returns vector results without a `warming` marker for `embeddings`
- **AND** a governed write waits until the graph handoff and the semantic corpus are ready

#### Scenario: An unchanged semantic corpus is not rebuilt on start

- **WHEN** a Cloud cell restarts on a vault that did not change while it was stopped
- **THEN** it loads its semantic corpus state from its volume, without rebuilding it from the vault
