---
exomem_id: d87bd8ed-05a3-50c2-9ee9-3f976f30b2e0
type: entity
entity_type: decision
status: active
created: 2026-07-02
updated: 2026-07-02
decided: 2026-07-02
project: fernwatch
decision_status: accepted
tags: [software, ci]
---

# Run Fernwatch CI on one Linux runner

## Context

Fernwatch only ships as a web page served from a Linux machine, so building on other systems adds cost without information.

## Decision

CI runs on one Linux runner. A build, the unit tests and a headless browser smoke test all run there, and the whole pipeline takes about nine minutes.

## Alternatives considered

A matrix of Linux, macOS and Windows runners. Rejected as slow and pointless for a Linux-only deployment.

## Consequences

Build time is the main cost, and every minute there is felt on each push.

## Relations

- relates_to [[Entities/Concepts/Fernwatch]]
