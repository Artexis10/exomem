## ADDED Requirements

### Requirement: Lazy CLI Import Path

The CLI entry point SHALL defer imports of the server, embedding, and media stacks until a
command actually requires them: `exomem --help` and model-free one-shot product commands MUST
NOT import torch, sentence-transformers, FastMCP server modules, or media dependencies.
Command behavior and output MUST remain unchanged.

#### Scenario: Help path imports no heavy stacks

- **WHEN** `exomem --help` runs
- **THEN** the process completes without importing torch, sentence-transformers, or the
  FastMCP server module

#### Scenario: Model-free one-shot stays lean

- **WHEN** a model-free one-shot product command runs with embeddings disabled
- **THEN** no embedding or media stack is imported
- **AND** the command's output is identical to the pre-change behavior
