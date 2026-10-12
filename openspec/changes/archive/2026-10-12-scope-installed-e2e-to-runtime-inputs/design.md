# Design

## Context

See the proposal for motivation. The wheel and source archive exclude OpenSpec.
The installed journey treats its OpenSpec reference as an opaque Planning pointer.

## Goals / Non-Goals

Use the existing job and Git comparison. Keep installed proof for every change to its possible inputs.
The scope decision supplies no runtime acceptance and repairs no query deadline failure.

## Decisions

Check out the synthetic pull request merge with depth two and compare it against its first parent.
Use Git path selection with rename detection disabled so both rename paths participate.
A successful comparison with no changes outside OpenSpec permits omission. Every other result retains execution.
Conditional setup and journey steps share that decision; an explicit summary identifies omitted execution.
A new action, helper script, or job would duplicate Git and add maintenance.

## Risks / Trade-offs

Incomplete ancestry can hide earlier source changes. The comparison defaults to execution when ancestry is unavailable.
A green scope job can look like runtime proof. The step summary states that the journey did not run.
Package inputs could later include OpenSpec. The maintained contract proof must detect that boundary change.
The job still allocates a runner. Savings remain unmeasured.

## Migration Plan

1. Deliver the repair with its installed journey enabled; expect ordinary CI to pass.
2. Revert the repair if the selection is incorrect; expect unconditional journey execution.
