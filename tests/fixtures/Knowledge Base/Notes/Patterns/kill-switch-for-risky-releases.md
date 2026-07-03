---
type: pattern
status: active
created: 2026-05-23
updated: 2026-05-23
sources: []
pattern_type: architectural
tags: [feature-flags, safety, operations]
---

# Kill switch for risky releases

## Problem

When a release starts misbehaving in production, the team needs to disable it in seconds, without a redeploy or a rollback that itself takes time.

## Solution

Put every risky change behind a boolean kill switch evaluated at request time. Flipping the switch off — a single config write — instantly returns to the previous safe behavior. The switch is independent of the deploy pipeline, so remediation does not wait on a build.

## When to use

Any change severe enough that "wait for a rollback deploy" is too slow a remediation.

## When NOT to use

Trivial changes where a normal rollback is fast enough; every switch is config surface to maintain.

## Connections

- [[Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout]]
