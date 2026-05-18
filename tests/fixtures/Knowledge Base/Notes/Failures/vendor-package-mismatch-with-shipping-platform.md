---
type: failure
status: active
created: 2026-05-06
updated: 2026-05-06
sources: []
projects: [endstate]
severity: moderate
tags: [vendor, packaging, windows]
---

# Vendor package mismatch with shipping platform

## What happened

Vendor packages developed on Linux/macOS frequently ship code that breaks on Windows — path/case/separator/locking issues vendor's daily driver doesn't experience.

## Mechanism

JS packaging has no platform metadata; maintainer incentives are local; the patch is invisible to upstream.

## Mitigation

patch-package as escape valve; upstream PR as long-term; fork as fallback.

## Connections

- [[Knowledge Base/Notes/Patterns/locked-cryptographic-contract-with-rfc-citations]]
