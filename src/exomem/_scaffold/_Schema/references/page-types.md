# Page Types

Eight page types in the Knowledge Base. Each has a required location, naming
convention, and content shape. See `frontmatter.md` for the YAML spec per type.

## source

**Location:** `Sources/Articles/`, `Sources/Sessions/`, or `Sources/Books/` (also `Sources/Papers/`, `Sources/Videos/`, `Sources/Other/`)
**Naming:** `YYYY-MM-DD-<slug>.md` (e.g., `2026-05-09-retrieval-patterns.md`)
**Mutability:** Append-only. Never edit after creation.
**Required frontmatter:** `type: source`, `source_type`, `captured`, `url` (if applicable), `tags`, `ingested_into` (initially empty list, updated when compiled into notes).

**Content shape:**

```markdown
---
type: source
source_type: article
captured: 2026-05-09
url: https://example.com/retrieval-patterns
tags: [retrieval, knowledge-base]
ingested_into: []
---

# <Title>

> Brief one-line description of what this source is.

## Capture

<full text or substantive excerpt here>

## Why captured

<one or two sentences on why this is in the KB>
```

For long sources, capture an excerpt + URL rather than full text. For Sessions,
dump the conversation verbatim under `## Capture`.

## research-note

**Location:** `Notes/Research/<scope>/` where scope is a registered project key
(see SKILL.md § Research scope keys and `_Schema/project-keys.yaml`) — an open
set, **not a closed enum**: new scopes auto-register on first use when you write
with a new `project:` slug.
**Naming:** `<topic-slug>.md` — concise, dash-separated, lowercase. No date prefix (research notes evolve).
**Mutability:** Editable. Replace via supersession when a major rewrite is needed.
**Required frontmatter:** `type: research-note`, `project`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `supersedes`, `superseded_by`.

**Content shape:**

```markdown
---
type: research-note
project: project-alpha
status: active
created: 2026-05-09
updated: 2026-05-09
sources:
  - "[[Knowledge Base/Sources/Articles/2026-05-09-retrieval-patterns]]"
tags: [agentic-rag, retrieval, knowledge-graph]
---

# <Topic>

## Question

What problem or topic this note addresses.

## Findings

The substance. Multiple subsections OK.

## Relations

- relates_to [[Knowledge Base/Entities/Concepts/Agentic RAG]]

## Open threads

- Things still uncertain
- Things to dig into next
```

## insight

**Location:** `Notes/Insights/`
**Naming:** `<insight-slug>.md` — phrased as a claim or a noun phrase, not a question.
**Mutability:** Editable; supersession preferred for substantial rewrites.
**Required frontmatter:** `type: insight`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `projects` (list of projects this insight applies to).

**Content shape:**

```markdown
---
type: insight
status: active
created: 2026-05-09
updated: 2026-05-09
sources:
  - "[[Knowledge Base/Notes/Research/Project Alpha/agentic-rag-retrieval-budget]]"
projects: [project-alpha, project-beta]
tags: [retrieval, evaluation]
---

# <Insight as a claim>

## Claim

One paragraph stating the insight cleanly.

## Why it holds

The reasoning, evidence, examples.

## Where it applies

Concrete domains, projects, decisions this should influence.

## Counter-considerations

What could weaken or invalidate this insight.

## Relations

- relates_to [[...]]
```

## failure

**Location:** `Notes/Failures/`
**Naming:** `<failure-slug>.md`
**Required frontmatter:** `type: failure`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `projects`, `severity` (minor / moderate / serious / critical — qualitative, not numeric).

**Content shape:**

```markdown
---
type: failure
status: active
created: 2026-05-09
updated: 2026-05-09
sources: [...]
projects: [project-alpha]
severity: moderate
tags: [...]
---

# <Failure mode as a name>

## What happened

The concrete event or pattern.

## Mechanism

Root cause. Why it happened, not just what.

## Detection

How it was noticed (or how it would be noticed earlier next time).

## Mitigation

What changed (or should change) to prevent recurrence.

## Relations

- relates_to [[...]]
```

## pattern

**Location:** `Notes/Patterns/`
**Naming:** `<pattern-name>.md`
**Required frontmatter:** `type: pattern`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `projects`, `pattern_type` (e.g., architectural, workflow, prompting, pedagogical).

**Content shape:**

```markdown
---
type: pattern
status: active
created: 2026-05-09
updated: 2026-05-09
sources: [...]
projects: [project-alpha, project-beta]
pattern_type: architectural
tags: [...]
---

# <Pattern name>

## Problem

What this pattern is solving.

## Solution

How the pattern works.

## When to use

Conditions where it fits.

## When NOT to use

Conditions where it's a regression.

## Relations

- relates_to [[...]]
```

## experiment

**Location:** `Notes/Experiments/<domain>/` where domain is `Workflow`, or another
sub-domain that emerges (`Research`, `Ops`, etc.). Add a new sub-domain only
when the first experiment in that domain is being written.
**Naming:** `YYYY-MM-<slug>.md` — date-prefixed because experiments are time-bounded events, not evolving notes. Use the **start month**.
**Mutability:** Editable while ongoing; once concluded, body is read-only except for supersession or follow-up notes that link back.
**Required frontmatter:** `type: experiment`, `domain`, `status`, `created`, `updated`, `started`, `duration`, `tags`. Optional: `n` (sample size, default 1), `concluded`, `hypothesis`, `sources`, `supersedes`, `superseded_by`.

**Content shape:**

```markdown
---
type: experiment
domain: workflow
status: active
created: 2026-05-09
updated: 2026-05-09
started: 2026-05-09
duration: "30 days"
n: 1
hypothesis: "Batching code review into one daily slot cuts context-switching"
sources:
  - "[[Knowledge Base/Sources/Books/2026-04-deep-work]]"
tags: [workflow, batching, focus]
---

# <Experiment name>

## Hypothesis

What you expected to happen, and why.

## Protocol

How the experiment is run. Specific enough that someone could repeat it.

## Baseline

Starting state. Measurements, observations, qualitative descriptions.

## Intervention

What's actually being changed. The independent variable.

## Results

What happened. Updated as the experiment runs; finalized at conclusion.

## Conclusion

What you learned. May be inconclusive — that's a valid result.

## Relations

- relates_to [[...]]
```

### Experiments are not research notes

A research note synthesizes secondary sources (papers, articles, conversations)
to compile understanding. An experiment runs a protocol and captures primary
data. They have different epistemic status: an experiment's `n=1` self-data is
direct evidence; a research note is filtered through somebody else's frame.

When in doubt, ask: did I just *do* something and observe results, or did I *read
about* something and synthesize? First → experiment. Second → research-note.

## production-log

**Location:** `Notes/Productions/<medium>/` where medium is `Posts`, `Articles`,
`PDFs`, `Episodes`, or another medium that emerges. Add a new medium subfolder only
when the first production in that medium is being written.
**Naming:** `YYYY-MM-<slug>.md` — date-prefixed by start month.
**Mutability:** Editable across the production lifecycle (planned → recorded → edited → published → reflected). Once complete and reflection is logged, treat as read-only except for supersession.
**Required frontmatter:** `type: production-log`, `medium`, `status`, `created`, `updated`, `tags`. Optional but typically present: `projects`, `host`, `editor`, `recorded`, `published`, `sources`, `related`, `supersedes`, `superseded_by`.

**Status values for production-logs:**

- `planned` — design / outline phase
- `recorded` — primary capture done, edit pending
- `edited` — production assets finalized
- `published` — live; outcomes accumulating
- `reflected` — published + reflection complete; no further updates expected
- `dropped` — abandoned (use `archived` to step it out of active rotation entirely)

**Content shape:**

```markdown
---
type: production-log
medium: posts
status: recorded
created: 2026-05-09
updated: 2026-05-09
recorded: 2026-05-09
published: null
projects: [project-alpha]
host: the host
editor: a teammate
sources:
  - "[[Knowledge Base/Sources/Sessions/2026-05-09-launch-planning]]"
related:
  - "[[Knowledge Base/Notes/Research/Project Alpha/launch-messaging]]"
  - "[[Knowledge Base/Notes/Patterns/short-form-post-template]]"
tags: [posts, launch, batch-01]
---

# <Production batch name>

## Frame

What this production is, who it's for, why now. The strategic framing.

## Artifact

The actual creative output — scripts, outlines, assets, links to footage.

## Production session

How the capture/recording/writing actually went. Takes, edits, decisions made on the fly.

## Outcomes

To be filled across the lifecycle. Engagement metrics, post-publish observations.

## Reflection

Filled once outcomes have settled. What worked, what to keep / change / drop next time.

## Relations

- relates_to [[...]]
```

### Production-log vs experiment

Both are time-bounded with date-prefixed filenames; both have outcomes. The
difference is the epistemic object:

- **Experiment:** a hypothesis tested under a protocol with primary data.
  Conclusion confirms / refutes / qualifies the hypothesis. The value is the
  *finding*.
- **Production-log:** a creative artifact + the production knowledge around it.
  Outcomes are engagement metrics, audience response, what to do differently next
  time. The value is the *thing made*.

### Production-log vs research-note

A research-note is *secondary synthesis* — reading and connecting. A
production-log captures the production of a primary creative artifact and the
operational knowledge around making it. The curriculum *informing* a reel batch
is a research-note; the reel batch itself, with scripts and recording session and
metrics, is a production-log.

## entity

**Location:** `Entities/<entity-type>/` where entity-type ∈ {People, Concepts, Libraries, Decisions}
**Naming:**

- **People:** the working short used in daily reference — typically the first name
  for people in your working circle, the full name for public figures. The H1
  carries the full disambiguating name when it differs from the filename.
  Disambiguate the filename only when two entities would otherwise collide.
- **Concepts, Libraries, Decisions:** Title Case, the entity's canonical name.
  `Agentic RAG.md`, `pgvector.md`, `Adopt pgvector for the engine.md`.

**Required frontmatter:** `type: entity`, `entity_type`, `status`, `created`, `updated`, `tags`. Optional fields by entity-type.

### People

Optional frontmatter: `affiliation`, `relationship` (e.g., colleague, public-figure, source-author).

```markdown
---
type: entity
entity_type: person
status: active
created: 2026-05-09
updated: 2026-05-09
affiliation: independent researcher
relationship: public-figure
tags: [research, methods]
---

# Jordan Lee

## Summary

One paragraph.

## Why in the KB

What this person is relevant to in your work.

## Relations

- relates_to [[...]]
```

### Concepts

Optional frontmatter: `domain` (e.g., retrieval, infrastructure, governance).

### Libraries

Optional frontmatter: `language`, `repo`, `license`, `used_in` (list of projects).

### Decisions

These are essentially lightweight ADRs.
Optional frontmatter: `decided` (date), `project`, `decision_status` (proposed / accepted / superseded).

```markdown
---
type: entity
entity_type: decision
status: active
created: 2026-05-09
updated: 2026-05-09
decided: 2026-05-09
project: project-alpha
decision_status: accepted
tags: [retrieval]
---

# <Decision title>

## Context

What was the situation that forced a decision.

## Decision

What was decided.

## Alternatives considered

Brief; one paragraph each.

## Consequences

What this commits us to and what it forecloses.

## Relations

- relates_to [[...]]
```

## Naming conventions summary

The filename is a stable machine address; `title:` and the canonical H1 are the
human display identity. Titles support arbitrary Unicode. Callers may pass an
explicit lowercase ASCII kebab-case `slug` (maximum 100 characters) for a new
source, note, or entity without changing the stored title. When omitted, the
legacy automatic filename policy remains for compatibility. Exomem does not
attempt language-aware transliteration, and it never renames existing pages on
upgrade or reconcile.

- Sources: date-prefixed, dash-slug, lowercase: `2026-05-09-retrieval-patterns.md`
- Notes (research, insight, failure, pattern): no date prefix, dash-slug, lowercase: `agentic-rag-retrieval-budget.md`
- Experiments: date-prefixed (start month), dash-slug, lowercase: `2026-05-30-day-low-carb.md`
- Production-logs: date-prefixed (start month), dash-slug, lowercase: `2026-05-launch-recap.md`
- Entities: the Unicode canonical name is stored in `title:` and the H1; an
  explicit ASCII slug may be used for the filename.

Sources, experiments, and production-logs are dated because their value is partly
temporal. Notes are not dated because they evolve. Entities are named after the
thing they are. Filenames are stable addresses; renaming them churns wikilinks
across the graph without information gain.

## `_attachments/` convention

Any location holding compiled notes or source captures may have a sibling
`_attachments/` folder for binary outputs or originals that need to live near the
parent note. The underscore prefix marks it as not-a-primary-content folder
(a common convention; Obsidian also treats `_`-prefixed folders this way).

**Discipline:**

- **Append-only.** Binaries are not edited after placement. Revisions go in as new files (e.g., `protocol_v2.docx`).
- **No frontmatter** on binary files (none possible).
- **Referenced from the parent note**, never standalone.
- **Filename = description.** Date prefix where temporal anchoring matters; otherwise descriptive slug.
- **Not Evidence.** If the artifact came from a third party and must be preserved as-received without analytical processing, it belongs in `Evidence/<scope>/<category>/` instead. See `write-scope.md` § binary placement.

## Research-note scope heuristic

A research-note's `project` field reflects *audience and reuse domain*, not
*triggering occasion*. When a note feels like it could go in two scopes, that's
usually a signal it's an insight or pattern (cross-cutting) and should be promoted
accordingly.
