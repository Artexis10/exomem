# Proposal

## Why

A Cloud user's cell upgrades often, sometimes daily, and nothing tells the person what changed. The developer changelog is built from conventional commit titles, so it names modules and internal terms that a person who does not code cannot read. Users miss improvements they would use, and the operator should not have to write release notes by hand.

## What Changes

- A pull request whose change a user would notice adds one plain sentence and a short title for users as a file under `user-notes/`. A pull request without one is not mentioned to users.
- A CI check asks every `feat`, `fix` or `perf` pull request to decide: it adds a `user-notes/` file or its body carries the line `User note: none`. The check reads only the title type, the changed paths and that marker line; it never judges the wording.
- The release build derives each release's notes from the release tags and bakes them into the image as package data, newest first and bounded.
- `bootstrap(profile="compact", section="whats_new")` returns the running version and the recent releases' notes. Substrate's Home reads it like the activity section.
- On a cloud cell, the first tool result for each connected app after the cell's version moves carries one `update_notice` field with a sentence for the assistant to relay. It then stops for that app until the next version with notes.

Out of scope: email, a notice on a self-hosted server, and rewriting notes with a model.

## Capabilities

### New Capabilities

- `user-release-notes`: how a note is written, checked, collected per release and shipped in the image.

### Modified Capabilities

- `hosted-tenant-cell`: serve the release notes and relay one update line per connected app after an upgrade.

## Impact

- Repository: `user-notes/`, a generator script, the PR check, the release workflow's image builds, `AGENTS.md` and the PR template.
- Code: the bootstrap section, the tool-result relay beside `with_service_notice`, and per-app state in the cell's state store.
- Public surface: one bootstrap section and one optional tool-result field on cloud cells. No new tool and no new argument.
- Data: one small row per connected app in cell state.
