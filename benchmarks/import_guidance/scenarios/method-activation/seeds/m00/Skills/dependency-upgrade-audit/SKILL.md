---
exomem_id: 44dc769d-1596-519f-9fbb-af785447bd2a
name: dependency-upgrade-audit
description: "Checks what changes when a project moves a library, runtime or tool it already uses to a newer version: changelog, breaking changes, migration steps and the project's own usage. Use when the user is about to upgrade a dependency or asks whether a version bump is safe."
status: active
created: 2026-06-24
updated: 2026-10-06
---

# Dependency upgrade audit

How I want an upgrade of something a project already depends on checked before it happens.

## When it applies

I say I am about to bump a library, a language version or a build tool in a project, or I ask whether it is safe. The dependency is already in use.

## When it does not apply

- A tool or library the project does not use yet. That is an evaluation, not an upgrade.
- General news about a release.
- A review of the code change that performs the upgrade.

## Steps

1. Name the dependency, the current version and the target version.
2. Read the changelog between the two versions and list breaking changes, deprecations and changed defaults.
3. Search the project for each affected API and list the places that use it.
4. Check whether the build and release pipeline needs changes, in particular for the Windows build.
5. Propose a migration order and a rollback step.
6. State a verdict: safe now, safe after the listed changes, or wait.

## Rules

- Work from the changelog and the maintainers' notes, not from forum posts.
- Do not run the upgrade unless I ask.
- Flag any dependency that has had no release in over a year.

## Where results go

A short upgrade note under the project's folder in Notes/Research. The verdict goes in the reply as well.

## Format of the result

Begin with the verdict. Then give a short table of breaking changes, each with the number of places that use the affected API in the project. Finish with the migration order as numbered steps and the rollback step.

## Common mistakes

- Reading only the release announcement and not the full changelog.
- Forgetting the platforms that the release pipeline builds for.
- Upgrading two dependencies at once so that a failure has two suspects.
- Treating a major version as safe because the project passes its tests on the current platform only.

