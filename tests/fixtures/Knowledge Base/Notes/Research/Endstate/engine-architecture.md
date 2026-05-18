---
type: research-note
project: endstate
status: active
created: 2026-05-08
updated: 2026-05-08
sources: []
tags: [architecture, go, engine]
---

# Endstate Engine Architecture

## Question

How is the Endstate Go engine structured, and what's the boundary between engine and GUI?

## Findings

Single Go binary `endstate.exe` v2.0.0. Internal packages: backup, bundle, commands, config, driver, envelope, events, manifest, modules, planner, restore, snapshot, state, verifier. CLI surface includes capabilities, apply, verify, capture, restore, bundles, modules, profile, report, bootstrap, backup subcommand family.

Output contract: stdout final envelope (schemaVersion/cliVersion/command/runId/timestampUtc/success/data/error), stderr NDJSON events with runId.

## Connections

- [[Knowledge Base/Entities/Concepts/Envelope]]
