---
type: insight
status: active
created: 2026-05-22
updated: 2026-05-22
sources: []
tags: [feature-flags, rollout, release-engineering]
---

# Gradual percentage rollout beats big-bang feature releases

## Claim

Release a risky feature by ramping its feature flag through percentage cohorts — 1%, 5%, 25%, 100% — rather than flipping it on for everyone at once. Each percentage step is a checkpoint where you watch error rates before widening exposure.

## Why it holds

A gradual percentage rollout bounds the blast radius: a regression that only shows under real traffic is caught at 1% of users, not 100%. The flag is the throttle; the cohort percentage is the dial.

## Where it applies

Any user-facing change behind a feature flag where a bad release would degrade many users at once.

## Connections

- [[Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases]]
