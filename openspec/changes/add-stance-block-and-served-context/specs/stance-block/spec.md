## Purpose

Serve bounded global stance lines from admitted compiled knowledge without changing authority or exposing withheld material.

## ADDED Requirements

### Requirement: An eligible page marks all its units as global stance

A page SHALL supply stance only when it is live compiled knowledge eligible under the existing semantic contract and carries boolean frontmatter `stance: true`. Sources, Evidence, pending pages and other ineligible pages SHALL supply none. Each semantic unit on an eligible marked page SHALL be a proposed global line. Existing unit admission and terminal egress SHALL determine which lines the caller receives, including references inside prose. The mark SHALL grant no authority.

A governed write SHALL reject nonboolean `stance`, with an error naming the key and `true` and `false`. An invalid hand edit SHALL produce a validation finding and SHALL supply no stance. Promoting an existing page SHALL require the owner's authorization for all resulting global lines; an explicit request for that exact promotion already supplies authorization. A scoped rule SHALL remain ordinary knowledge rather than global stance.

#### Scenario: A captured source cannot become stance

- **WHEN** a Source contains semantic-looking units and `stance: true`
- **THEN** it supplies no stance lines

#### Scenario: Existing-page promotion covers all units

- **WHEN** the agent proposes marking a compiled page with three units
- **THEN** the proposal identifies all three global lines, and the page is not marked without authority for them

#### Scenario: A malformed mark is refused

- **WHEN** a governed write supplies `stance: "yes"`
- **THEN** validation and commit reject it with the same remediation

### Requirement: Maps responses carry a bounded caller-visible stance block

Unless `EXOMEM_ACTIVATION_STANCE=off`, each maps response SHALL carry `stance` with `state`, `generation`, `items`, `omitted` and `expired`. State SHALL be `ready` for at least one served line, `empty` for none and `unavailable` for failed compilation. Items SHALL contain admitted `{ref, text}` lines and `review_due` when applicable. Compilation failure SHALL leave the existing map and slice available.

Lines SHALL follow page path and unit order. Keep whole lines under `STANCE_MAX_CHARS` (initially 800) and remaining counted-prose space. Charge actual new counted prose through the packet's existing budget rule. An empty block SHALL consume no stance prose allocation. Structural fields remain outside `max_chars`.

Omitted SHALL count admitted current lines that did not fit. Expired SHALL count admitted lines withheld by typed expiry. A caller-withheld page or unit SHALL affect neither count, public generation nor date boundary. Every line SHALL pass the same unit and terminal egress checks as other served units.

#### Scenario: A visible line cannot expose a hidden reference

- **WHEN** a visible stance unit names a page that this caller cannot see
- **THEN** the unit receives the existing unit egress decision, and the block exposes no hidden reference or hidden-material count

#### Scenario: An empty vault spends no stance prose

- **WHEN** no admitted eligible stance line exists
- **THEN** `stance.state` is `empty` and the stance block consumes no counted prose

#### Scenario: Overflow preserves whole lines

- **WHEN** only twelve of forty admitted current lines fit
- **THEN** the block serves twelve whole lines and reports `omitted: 28`

### Requirement: Cache lifetime follows admitted inputs and date boundaries

Cache the stance block separately from conversation-dependent packets. Its key SHALL bind the caller's released view and admitted stance inputs. Its lifetime SHALL end at the next relevant date boundary, using the due-state calendar day. Passed boundaries SHALL invalidate stale entries without a page write. Public generation SHALL change when the admitted result changes. Hidden inputs SHALL change no public generation or boundary. A conversation-dependent request MAY reuse a valid stance cache entry.

#### Scenario: Expiry invalidates an otherwise unchanged cache

- **WHEN** an admitted line expires on 2026-12-01 and the block was cached on 2026-11-30
- **THEN** the first response on 2026-12-02 recompiles, withholds the line and counts it as expired

### Requirement: Stance lines reuse supported currency and supersession

Every admitted line SHALL pass the existing currency step with typed date rules. Superseded or archived lines SHALL never serve. Expired lines SHALL be withheld and counted; passed `check_by` SHALL keep a line served with its review marker. No stance date SHALL be inferred from prose.

A replacement unit SHALL use the same-page unit supersession supported by current currency. A replacement on another page SHALL use the existing page-supersession route. This increment SHALL NOT promise cross-page unit supersession.

#### Scenario: A same-page replacement removes the earlier line

- **WHEN** a later unit on a stance page supersedes an earlier unit on that page
- **THEN** only the later line serves and the earlier line contributes to neither count

#### Scenario: A review date does not invent invalidity

- **WHEN** an admitted current line's `check_by` has passed
- **THEN** it remains served with `review_due` and is not counted as expired

### Requirement: One setting removes the block

`EXOMEM_ACTIVATION_STANCE` SHALL default to `on`. Setting it to `off` SHALL remove the block and its prose cost. Serving stance SHALL run no model. Capture authority SHALL continue to follow the engagement policy independently of this serving setting.

#### Scenario: Rollback removes stance

- **WHEN** the setting is `off`
- **THEN** maps responses contain no stance block and charge no stance prose
