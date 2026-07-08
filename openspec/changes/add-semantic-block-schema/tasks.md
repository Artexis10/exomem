# add-semantic-block-schema — tasks

## 1. OpenSpec Contract

- [x] 1.1 Add proposal, design, and delta specs for semantic blocks and context-pack exposure.
- [x] 1.2 Validate the OpenSpec change structure.

## 2. Core Semantic Blocks

- [x] 2.1 Implement `src/exomem/semantic_blocks.py` with block/relation constants, dataclasses, parser, serializer helpers, and validation results.
- [x] 2.2 Make parsing fence-aware and Markdown-compatible: recognized headings become blocks, unknown headings remain ordinary Markdown, and leading `- key: value` metadata is optional.
- [x] 2.3 Parse and validate relation metadata for the supported relation vocabulary.

## 3. Integration

- [x] 3.1 Update claim extraction to prefer parsed semantic `claim` blocks before the existing section fallback.
- [x] 3.2 Update context-pack assembly to include parsed semantic blocks additively when present.

## 4. Tests And Docs

- [x] 4.1 Add `tests/test_semantic_blocks.py` covering parsing, validation, fence handling, aliases, duplicate ID warnings, claim extraction, and context-pack exposure.
- [x] 4.2 Add `docs/semantic-blocks.md` with the Markdown shape, supported vocabulary, and examples.

## 5. Verification

- [x] 5.1 Run OpenSpec validation for `add-semantic-block-schema`.
- [x] 5.2 Run targeted pytest coverage for semantic blocks, claims, and context packs.
