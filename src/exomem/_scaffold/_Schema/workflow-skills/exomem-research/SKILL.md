---
name: exomem-research
description: Run a focused research loop with Exomem: gather sources, preserve evidence, compile attributed findings, and connect prior notes.
version: 0.1.0
---

# exomem-research

## Purpose
Answer a focused question and save what should compound.

## When to use
Use when the user asks to research a topic, compare options, investigate a claim, or save findings from a research pass.

## Workflow
1. Search Exomem first for prior conclusions and related sources.
2. Gather external sources only as needed; preserve important raw sources with `add` or `preserve`.
3. Attribute findings to sources and distinguish evidence from interpretation.
4. Compile the result with `note` as a `research-note` unless another type clearly fits.
5. Use `suggest_links` and connect related prior notes.

## Output contract
Return findings, cited sources, confidence/limits, saved path, and follow-up questions.

## Save rules
Save findings that answer the question, change a decision, or create reusable context. Keep source attribution explicit.

## Mistakes to avoid
Do not research from scratch before checking Exomem. Do not save a source list without synthesis. Do not hide uncertainty or source gaps.
