# Page types

Each page type has a location, a naming rule, the fields it carries and a body shape.
This file also defines what each frontmatter field means. You never write YAML: the typed
tool for the write (`create_note`, `save_source`, `edit_note`) builds the frontmatter, and
the server fills the generated fields.

The note types and statuses are registries, not lists in this file. Read the live set with
`inspect_memory(operation="schema.inspect", arguments={"subject": "note-types"})` and
`arguments={"subject": "statuses"}`. A vault adds its own types in its overlay.
`read_instructions(topic="create_note")` lists the call's fields.

Every compiled-note body shape below shows a canonical `## Observations` example. The fence
keeps documentation examples from becoming indexable units. Observation category is open
vocabulary, page `tags` remain secondary retrieval labels, and a valid non-empty block headed
by a governed rich kind is the alternative when richer semantics are intended.

Each `## Relations` line below shows one registered relation that often fits that page type.
It is an example, not a default. [Vocabulary](vocabulary.md) and [writing](writing.md) say how
to choose a relation, and when to write none.

## Fields

Every page carries the first group. The `Set by` column says who writes the value.

| Field | Set by | Meaning |
|---|---|---|
| `exomem_id` | server | Immutable identity (a UUID). Legacy pages gain one only through an explicit id backfill |
| `type` | you | A registered note type |
| `title` | server, from your `title` | The display title. The canonical H1 repeats it, and it does not depend on the file name |
| `status` | you | Lifecycle state, from the statuses registry. See Status below |
| `created`, `updated` | server | UTC instants at second granularity; `created` never changes and `updated` moves on every edit |
| `tags` | you | Lowercase and dash-separated, with no `#`. Skip catch-alls such as `important` or `todo` |
| `sources` | you | Wikilinks to the pages this one draws from; `create_note(sources=[...])` |
| `projects`, `project` | you | Project keys the page serves. A new slug registers itself |
| `supersedes`, `superseded_by` | server | Set by `supersede_note`; `superseded_by` names the replacement |
| `summary` | you | One line of context, at most 1024 characters: `fields={"summary": ...}` |
| `topics` | you | Domain keys that file the page under topics: `fields={"topics": [...]}`. The server resolves aliases, replaces deprecated keys and registers a new key. Invalid keys and directory collisions refuse the write |

Pages written before second granularity carry a date-only `created` or `updated`. The server
never rewrites them, because midnight would claim a precision the page never had. Readers accept
both forms. Two values on the same day are unordered when either is date-only; a tool reports that
and never guesses an order.

Wikilinks in frontmatter are double-quoted, vault-rooted and carry no `.md`. The server
normalizes any input form (bare name, `[[ ]]` wrapper, `|alias`, `#anchor`), so write what you have.

A page's project keys can also declare topics through their registry `domain`. The page's own
`domain` field names a source subject or an experiment folder and files no topic.

Compiled notes may carry the three cross-domain bridge fields. [Governance](governance.md) owns them.

### Status

The statuses registry gives each status a class: live, pending, superseded, retired or
abandoned. The class decides how a page serves, so read the registry before using a new label.

- `superseded` means a newer page replaced this one, and `superseded_by` must name it.
- `archived` steps a page out of rotation into `<location>/_archive/`. It is never deletion, and
  no page carries a `deleted` status.
- An experiment is `active` while it plans and runs. When it finishes, set `concluded` and record
  `outcome`. Archive it only when nothing references it any more: a settled result is what later
  work cites.
- A refuted experiment is not superseded. Nothing replaced it; the question got an answer. It
  keeps `concluded`, stays retrievable and carries the evidence that refuted it. A vault that
  retires its negative results loses the expensive half of what it learned.
- A production-log's status follows its lifecycle: planned, recorded, edited, published, then
  reflected. `dropped` and `archived` end it early. The registry holds no order, so this line is
  the only place that states it.

### Fields that do not exist

- `confidence`. A confidence or degree of belief is never a frontmatter field. Trust comes from
  sources and link counts. An experiment's `outcome` and a unit's `verdict` are lifecycle state in
  words, and a number, percentage or hedge is never a valid value. A degree of belief that its
  holder stated is a `## Belief` unit: see Recording a degree of belief below.
- `decay_at` and `expires_at`. Knowledge does not expire on a schedule. Supersession or archival is
  explicit.
- Any `auto_*` field. Nothing reflects a background process; operations on the Knowledge Base are
  explicit.

## source

**Location:** `Sources/<Kind>/` — the directory is a projection of `source_type`, with `domain` adding one level below it when present, e.g. `Sources/Articles/`, `Sources/Reports/Travel/`. The metadata is authoritative and the folder derives from it.
**Naming:** `YYYY-MM-DD-<slug>.md` (e.g., `2026-05-09-retrieval-patterns.md`)
**Mutability:** Append-only in body. Never edit the captured content. The classification is a capture-time judgement and stays correctable: `manage_memory(operation="files.reclassify", arguments={...})` rewrites `source_type` and `domain`, moves the file to the folder the new classification projects to, and repoints every inbound reference. The body stays byte-identical and the reason is recorded. Preview it on `inspect_memory` first. Nothing reclassifies a source automatically. A pointer field such as `project` is corrected the same way, with a reason.

| Field | Required | Meaning |
|---|---|---|
| `source_type` | yes | What the artifact is, as a slug-shaped key in the source-kinds registry. An unknown key registers on first use, and `source_kind` is the argument name. Read the registry with `schema.inspect` and subject `source-kinds` |
| `domain` | optional | What the artifact is about, on an axis independent of `source_type`; the same open vocabulary |
| `projects` | optional | Project keys the source serves. This never affects where it is filed |
| `captured` | yes | ISO date; the same as `created` for sources |
| `url` | conditional | Required for the kinds whose registry entry sets `requires_url` |
| `author` | optional | |
| `ingested_into` | yes | Wikilinks to the compiled notes that cite this source. The server fills it when a note cites the source |
| `source_identity`, `capture_input_digest` | server | Written only at capture, so a repeat capture reconciles |

There is no catch-all kind. Name the closest existing kind, or a new slug. A capture without a kind
is refused with the vault's known kinds. `unclassified` marks a capture that no agent classified
(upload form, hosted capture box, legacy import), and `other` is retired. Neither is ever a choice for a
new capture. Both stay readable and filterable, and `files.reclassify` classifies them.

**Content shape:**

```markdown
# <Title>

> Brief one-line description of what this source is.

## Capture

<the captured content, verbatim>

## Why captured

<one or two sentences on why this is in the Knowledge Base>
```

Save originals whole through `save_source`. A source takes one of two forms with the same fields: a
transcript (content as is) or an origination record (a session-reasoning capture whose `ingested_into`
lists what it produced). A session goes in verbatim under `## Capture`.

## research-note

**Location:** `Notes/Research/<scope>/` where scope is a registered project key (see Research scope keys in [writing](writing.md) and `_Schema/project-keys.yaml`). The set is open, not a closed enum: a new `project:` slug registers on first use.
**Naming:** `<Title>.md`, the title as written. No date prefix, because research notes evolve.
**Mutability:** Editable. Replace through supersession when a major rewrite is needed.

| Field | Required | Meaning |
|---|---|---|
| `project` | yes | The scope key above |
| `sources` | yes | The pages this note draws from |
| `tenant` | optional | The tenant the note concerns. Only a project whose registry entry sets `tenant_scoped: true` takes it; audit flags it elsewhere |

**Content shape:**

```markdown
# <Topic>

## Question

What problem or topic this note addresses.

## Findings

The substance. Multiple subsections are fine.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- about_entity [[Knowledge Base/Entities/Concepts/Agentic RAG]]

## Open threads

- Things still uncertain
- Things to dig into next
```

A research note's `project` reflects the audience and the reuse domain, not the occasion that
triggered it. A note that fits two scopes is usually an insight or a pattern: promote it. A research
note has informal subtypes: standard; hub (orients a subsystem and links out, refreshed on major
ships); and snapshot (point in time, drift is fine, say "snapshot" in the body).

## insight

**Location:** `Notes/Insights/`
**Naming:** `<Title>.md`, phrased as a claim or a noun phrase, not a question.
**Mutability:** Editable. Prefer supersession for a substantial rewrite.

Fields: `sources` (required) and `projects` (optional, the projects the insight applies to).

**Content shape:**

```markdown
# <Insight as a claim>

## Claim

One paragraph stating the insight cleanly.

## Why it holds

The reasoning, evidence and examples.

## Where it applies

Concrete domains, projects and decisions this should influence.

## Counter-considerations

What could weaken or invalidate this insight.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- derived_from [[...]]
```

## failure

**Location:** `Notes/Failures/`
**Naming:** `<Title>.md`

Fields: `sources` (required), `projects` (optional) and `severity` (optional): a qualitative word
such as minor, moderate, serious or critical, never a number.

**Content shape:**

```markdown
# <Failure mode as a name>

## What happened

The concrete event or pattern.

## Mechanism

Root cause: why it happened, not just what.

## Detection

How it was noticed, or how it would be noticed earlier next time.

## Mitigation

What changed, or should change, to prevent recurrence.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- caused_by [[...]]
```

## pattern

**Location:** `Notes/Patterns/`
**Naming:** `<pattern-name>.md`

Fields: `sources` (required), `projects` (optional; a pattern that spans projects lists them all) and
`pattern_type` (optional, for example architectural, workflow, prompting or pedagogical).

**Content shape:**

```markdown
# <Pattern name>

## Problem

What this pattern is solving.

## Solution

How the pattern works.

## When to use

Conditions where it fits.

## When NOT to use

Conditions where it is a regression.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- mitigates [[...]]
```

## procedure

**Location:** `Notes/Procedures/`
**Naming:** `<procedure-name>.md`

A procedure is a reusable way of working that you write once and follow again. Fields: `summary`
(recommended, one line that says when to use it), `sources` and `projects` (both optional). Put each
distinct way of working in its own `## Procedure` block so that the block reads on its own, and
state in a "Not for" section when the procedure does not apply.

**Content shape:**

```markdown
# <Procedure name>

## Procedure

1. First step, with the check that shows it worked.
2. Second step.

## Not for

Situations where this procedure does not apply, such as a hotfix.

## Observations

- [technique] Run the full checks before tagging #release

## Relations

- depends_on [[...]]
```

## profile

**Location:** `Notes/Profiles/`
**Naming:** `<domain>.md` for a domain profile, `global.md` for the global profile.

A profile holds the owner's standing preferences, one short confirmed line each, and links the long
note that holds the detail. A domain profile has exactly one `topics` entry, and the global profile
has none. A page with two topics is refused, and a topic that no registered domain resolves makes a
page that supplies nothing. [Profiles](profiles.md) covers seeding and changing one.

**Content shape:**

```markdown
# Games

## Observations

- [rule] Prefer tactical play over luck.
- [rule] Keep sessions under two hours.

## Relations

- derived_from [[Notes/Insights/how-i-play]]
```

## session-transcript

**Location:** `Notes/Sessions/`
**Naming:** `<title-slug>.md`, written by `manage_memory(operation="media.group_session")`, not by `create_note`.

A session transcript is one page for separate recordings of one event, each from its own microphone. Its
`sources` link each recording with its track name as the link label, `track_offsets` gives each track's
start in seconds, and `alignment` says how the starts were found: from a time the files publish, or
`assumed-start` when they share none. Its `## Transcript` is generated and is overwritten when a member
changes. Everything below its owner-notes marker is the owner's and is kept.

## experiment

**Location:** `Notes/Experiments/<Domain>/`, where the folder is the domain's path label (`workflow` lives in `Workflow/`). Add a new sub-domain only when the first experiment in that domain is written.
**Naming:** `YYYY-MM-<Title>.md`, date-prefixed by the start month, because an experiment is a time-bounded event and not an evolving note.
**Mutability:** Editable while ongoing. Once concluded, the body is read-only except for supersession or follow-up notes that link back.

| Field | Required | Meaning |
|---|---|---|
| `domain` | yes | Lowercase key (`workflow`, `research`, `ops`) that names the subfolder |
| `started` | yes | ISO date the experiment began; it may differ from `created` when planning came first |
| `duration` | yes | Free text such as `"30 days"` or `"ongoing"` |
| `concluded` | optional | ISO date the experiment ended; absent while ongoing |
| `outcome` | optional | How it turned out, as a verdict of the registry. A categorical state, never a score |
| `n` | optional | Sample size; 1 when absent |
| `hypothesis` | optional | One line, restated in the body |
| `sources` | optional | Material that informed the protocol |

`concluded` says the run finished and its result stands. Pair it with `concluded:` (the date) and
`outcome:` (a verdict such as `confirmed`, `refuted`, `qualified`, `inconclusive` or `abandoned`).

**Content shape:**

```markdown
# <Experiment name>

## Hypothesis

What you expected to happen, and why.

## Protocol

How the experiment is run, specific enough that someone could repeat it.

## Baseline

Starting state: measurements, observations, qualitative descriptions.

## Intervention

What is actually being changed. The independent variable.

## Results

What happened. Updated as the experiment runs; finalized at conclusion.

## Conclusion

What you learned. Inconclusive is a valid result.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- tests [[...]]
```

### Closing the loop: predictions, check dates, and verdicts

An experiment is worth much more when it recorded what you expected before it ran. Write that as a
rich semantic unit with the governed `prediction` kind and the two metadata rows that make it
checkable:

```markdown
## Prediction
- id: batching-cuts-switches
- check_by: 2026-08-01
- verdict: refuted

Batching review into one daily slot will cut context switches by half.

## Evidence
- relations: contradicts: [[Knowledge Base/Notes/Experiments/Workflow/2026-05-batching]]

Switch count fell 12%, well short of the predicted halving.
```

A verdict introduces no new relation kind. Attach the refuting material with governed relations you
already have (`contradicts`, `evidenced_by`, `supports`) and let `verdict:` carry the judgment.

- `check_by:` is one ISO date (`YYYY-MM-DD`) for when to revisit the claim. It is a due date, not a
  deadline: nothing expires when it passes, and the claim becomes findable as overdue.
- `verdict:` is one verdict of the vault's registry, as its key or a label. A verdict is a judgment in
  words, never a number, so `verdict: 0.7` is rejected. `inspect_memory(operation="schema.infer",
  arguments={"subject": "categories"})` lists the vault's verdicts.

Both rows exist only in the rich form; a compact `- [category] ...` observation carries no metadata.
Edits preserve both rows, so correcting a prediction's wording never costs its verdict. A refuted
prediction stays active and ranked, for the same reason a refuted experiment stays concluded.

### Recording a degree of belief

A claim carries no confidence score. A degree of belief is its own `## Belief` unit: the belief in
prose, the rows `credence_holder` and `credence`, and an `assesses` relation to the claim.
`credence` is a unit row, never a frontmatter field. It is a belief level or a probability from 0 to
1. A probability needs `credence_basis`, and you never invent a number. A belief records what its
holder stated: never write one on a holder's behalf. `schema.infer` with subject `categories` lists
the accepted levels under `belief_levels`.

```markdown
## Belief
- credence_holder: Dana
- credence: 0.7
- credence_basis: Three of the last four batching trials cut switches by a third.
- relations: assesses: [[Knowledge Base/Notes/Experiments/Workflow/2026-05-batching]]

Dana puts the chance that the daily review slot halves context switches at 0.7.
```

A level such as `likely` replaces the number and its basis.

### Experiments are not research notes

A research note synthesizes secondary sources to compile understanding. An experiment runs a protocol
and captures primary data, so an experiment's `n=1` self-data is direct evidence, and a research note is
filtered through somebody else's frame. Ask: did I just do something and observe results, or did I read
about something and synthesize? The first is an experiment. The second is a research-note.

## production-log

**Location:** `Notes/Productions/<medium>/`, where medium is `Posts`, `Articles`, `PDFs`, `Episodes` or another medium that emerges. Add a new medium folder only when the first production in that medium is written.
**Naming:** `YYYY-MM-<Title>.md`, date-prefixed by the start month.
**Mutability:** Editable across the production lifecycle. Once complete and reflected on, treat it as read-only except for supersession.

| Field | Required | Meaning |
|---|---|---|
| `medium` | yes | `posts`, `articles`, `pdfs`, `episodes`, or another; matches the subfolder |
| `recorded` | optional | ISO date the primary capture happened |
| `published` | optional | ISO date, or `null` before publication |
| `host`, `editor` | optional | Who is on camera or leads, and who edits or produces |
| `related` | optional | Wikilinks to the notes the production draws on or applies |
| `projects`, `sources` | optional | As in Fields |

**Content shape:**

```markdown
# <Production batch name>

## Frame

What this production is, who it is for, why now.

## Artifact

The creative output: scripts, outlines, assets, links to footage.

## Production session

How the capture, recording or writing went. Takes, edits, decisions made on the fly.

## Outcomes

Filled across the lifecycle: engagement metrics, post-publish observations.

## Reflection

Filled once outcomes settle: what worked, and what to keep, change or drop.

## Observations

- [operating constraint] Keep retries bounded #reliability

## Relations

- implements [[...]]
```

### Production-log vs experiment and research-note

Both a production-log and an experiment are time-bounded, date-prefixed and outcome-bearing. The
epistemic object differs. An experiment tests a hypothesis under a protocol with primary data, and
its value is the finding. A production-log records a creative artifact and the knowledge around making
it, and its value is the thing made. A research note is secondary synthesis. The curriculum informing
a reel batch is a research note; the batch itself, with scripts, recording session and metrics, is a
production-log. Ask whether you set out to learn whether X is true (experiment) or to make a thing the
world sees (production-log).

## entity

**Location:** `Entities/<registered-folder>/`, with the folder resolved from the active entity
registry: `inspect_memory(operation="schema.inspect", arguments={"subject": "entity-types"})`.
**Naming:**

- **People:** the working short used in daily reference: the first name for people in your working
  circle, the full name for public figures. The H1 carries the full disambiguating name when it
  differs from the file name. Disambiguate the file name only when two entities would collide.
- **Other kinds:** Title Case, the entity's canonical name: `Agentic RAG.md`, `pgvector.md`.

Fields: `entity_type` (required): a stable id from the registry. Each type's optional fields come from
the same registry entry (`optional_frontmatter`). A vault extends the core kinds in its own entity-type
registry; [vocabulary](vocabulary.md) covers saving a new type.

### People

Optional fields: `affiliation`, `relationship` (for example colleague, public-figure, source-author) and
`aliases` for reviewed alternate spellings or names.

```markdown
# Jordan Lee

## Summary

One paragraph.

## Why in the KB

What this person is relevant to in your work.

## Contact

- [contact] A phone, address, email or identifier, one per unit.

## Relations

- produces [[...]]
```

`## Contact` is optional. Keep contact details in `[contact]` units there, never in the summary or in
`fact` units: activation serves them only when the turn asks to reach the person.

On a topic page, prefer `about_entity [[Entities/People/...]]` for the canonical topic-to-person edge.
On the person's own page, write a specific relation such as `member_of` or `produces`.

### Organizations

Use for a stable company, institution, team, agency or other organization whose facts, history or
relations recur across sources. Keep incidental or unresolved names in source or note context instead
of creating a speculative entity.

### Concepts, libraries and decisions

A concept anchors conclusions across sources. A library or package carries durable project context.
A decision (`entity_type: decision`) is a lightweight record of one choice, with `decided` (date), `project` and
`decision_status` fields, and a body that answers four questions:

```markdown
# <Decision title>

## Context

The situation that forced a decision.

## Decision

What was decided.

## Alternatives considered

Brief; one paragraph each.

## Consequences

What this commits us to and what it forecloses.

## Relations

- resolves [[...]]
```

## Naming conventions

The file name is a stable machine address. `title:` and the canonical H1 are the human display
identity, and a title can use any Unicode. By default a new note or entity takes its title as its file
name, keeping case, diacritics and spaces and dropping only what a file system or a link cannot carry.
A captured source keeps its dated lowercase slug name. A vault chooses a style with `filename_style`
or `EXOMEM_FILENAME_STYLE`. The server does no language-aware transliteration, and it never renames an
existing page on upgrade or reconcile.

Sources, experiments and production-logs carry a date because their value is partly temporal. Notes
carry none because they evolve. Entities are named after the thing they are. Renaming churns links
without information gain, so leave a file name alone until a title change makes the page misleading
([writing](writing.md) covers the rename suggestion).

## `_attachments/` convention

Any location that holds compiled notes or source captures may have a sibling `_attachments/` folder for
binary outputs or originals that need to live near the parent note. The underscore marks it as not a
primary-content folder.

- **Append-only.** Binaries are not edited after placement. A revision goes in as a new file, such as `protocol_v2.docx`.
- **No frontmatter**, because a binary has none.
- **Referenced from the parent note**, never standalone.
- **File name is the description.** Add a date prefix where the time anchors the file.
- **Not Evidence.** An artifact from a third party that must stay as received belongs in `Evidence/<scope>/<category>/`. [Write scope](write-scope.md) covers binary placement.
