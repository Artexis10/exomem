## ADDED Requirements

### Requirement: Map items name their page date `updated`

A map item SHALL carry its page's last update date in the field `updated`. No map item SHALL carry a field named `activity`, because workflow contracts use `activities` for a kind of task, and the served unit already names the same date `updated`. The value, its source and its ordering role SHALL stay as they were under the old name. The paginated `exomem://map` view and the retrieve hook SHALL read the new name.

#### Scenario: A map item carries `updated`

- **WHEN** a maps packet lists a page whose `updated` frontmatter is 2026-10-30
- **THEN** its map item carries `updated: "2026-10-30"` and no `activity` field

### Requirement: An empty turn slice says what it does not cover

When the turn slice's state is `empty`, its `description` SHALL state that the map and the slice cover compiled material only, that `search_memory` reaches captured Sources, and that an empty slice is not proof of absence. The sentence SHALL name `search_memory` through `capabilities.active_call_text`. Other slice states SHALL keep their description.

#### Scenario: An empty slice points at captured Sources

- **WHEN** a maps packet's slice state is `empty`
- **THEN** the slice `description` says that the map and the slice cover compiled material only, that the caller's `search_memory` call reaches captured Sources, and that an empty slice is not proof of absence

### Requirement: Typed unit dates decide currency

The packet builder's currency step SHALL read a unit's typed `as_of`, `until` and `check_by` keys (semantic-unit-language) and SHALL apply these rules in order, the first match winning:

1. A superseded or archived unit is history, whatever its dates.
2. A unit whose `until` is earlier than today is history. Nothing is written, its lifecycle stays live, and it is served with `history: true` where history is served.
3. A unit whose `check_by` is earlier than today stays current and carries `review_due` with that date.
4. A unit with no typed date stays current.

When `as_of` is present, it SHALL be the unit's own time and SHALL be served as the unit's `updated`, in place of the first date in the unit's prose context. The prose date SHALL remain the fallback for a unit without `as_of`. Today SHALL be the calendar day that the due-state projection uses. Every comparison SHALL be between typed dates.

#### Scenario: A unit past `until` becomes history without a write

- **WHEN** a unit carries `until: 2026-10-31` and today is 2026-11-02
- **THEN** the unit is served with `history: true`, its page is unchanged, and its lifecycle stays live

#### Scenario: Supersession wins over dates

- **WHEN** a superseded unit carries `check_by: 2027-01-01`
- **THEN** the unit is history and carries no `review_due`

#### Scenario: `as_of` replaces the prose date

- **WHEN** a unit's context reads "decided on 2026-03-04" and the unit carries `as_of: 2026-05-01`
- **THEN** the served unit's `updated` is `2026-05-01`
