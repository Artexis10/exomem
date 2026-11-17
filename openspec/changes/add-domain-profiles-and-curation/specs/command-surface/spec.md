## ADDED Requirements

### Requirement: A subject domain may declare a parent domain

A domain registry entry MAY declare one `parent`, which SHALL be another registered domain key.
Registry validation SHALL reject a parent that is not a registered domain, and a parent chain that returns to its own domain. It SHALL name the entry and the remedy.
A parent SHALL change no stored page, no folder and no domain membership. It SHALL describe only which domain is broader.
A vault overlay MAY set or change a parent on a shipped or vault domain.

#### Scenario: A narrower domain names its parent

- **WHEN** a vault overlay saves `games` with `parent: media`, and `media` is registered
- **THEN** the save succeeds, and `games` resolves `media` as its parent

#### Scenario: A cycle is refused

- **WHEN** a vault overlay saves `media` with `parent: games` while `games` has `parent: media`
- **THEN** the save is refused, and the refusal names both entries
