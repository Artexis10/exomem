# Page Types

Eight page types in the Knowledge Base. Each has a required location, naming convention, and content shape. See `frontmatter.md` for the YAML spec per type.

## source

**Location:** `Sources/Articles/`, `Sources/Sessions/`, or `Sources/Books/`
**Naming:** `YYYY-MM-DD-<slug>.md` (e.g., `2026-05-09-llm-wiki-v2-gist.md`)
**Mutability:** Append-only. Never edit after creation.
**Required frontmatter:** `type: source`, `source_type`, `captured`, `url` (if applicable), `tags`, `ingested_into` (initially empty list, updated when compiled into notes).

**Content shape:**

```markdown
---
type: source
source_type: article
captured: 2026-05-09
url: https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
tags: [llm-wiki, knowledge-base]
ingested_into: []
---

# Source: <Title>

> Brief one-line description of what this source is.

## Capture

<full text or substantive excerpt here>

## Why captured

<one or two sentences from Hugo about why this is in the KB>
```

For long sources, it's fine to capture an excerpt + URL rather than full text. For Sessions, dump the conversation verbatim under `## Capture`.

## research-note

**Location:** `Notes/Research/<scope>/` where scope is one of: `Substrate`, `Q`, `Endstate`, `Sift`, `Together Unprocessed`, `Health`, `Finance`, `Creative`, `Science`, `Travel`, `Book Club`. (Personal/cross-cutting research can use a research-note with `project: personal` and live in any of these folders, or be elevated to an Insight or Pattern if it's truly cross-cutting.)
**Naming:** `<topic-slug>.md` — concise, dash-separated, lowercase. No date prefix (research notes evolve).
**Mutability:** Editable. Replace via supersession when a major rewrite is needed.
**Required frontmatter:** `type: research-note`, `project`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `supersedes`, `superseded_by`, `tenant` (for Q tenants — see SKILL.md § Q tenants).

**Content shape:**

```markdown
---
type: research-note
project: q
tenant: ask-dr-chaffee
status: active
created: 2026-05-09
updated: 2026-05-09
sources:
  - "[[Sources/Articles/2026-05-09-llm-wiki-v2-gist]]"
tags: [agentic-rag, retrieval, knowledge-graph]
---

# <Topic>

## Question

What problem or topic this note addresses.

## Findings

The substance. Multiple subsections OK.

## Connections

- [[Entities/Concepts/Agentic RAG]]
- [[Domain - AI Systems & Architecture]]
- [[Products/Q/Strategy]]

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
  - "[[Notes/Research/Q/agentic-rag-retrieval-budget]]"
  - "[[Sources/Sessions/2026-05-04-q-strategy-debate]]"
projects: [q, endstate]
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

## Connections

- [[...]]
```

## failure

**Location:** `Notes/Failures/`
**Naming:** `<failure-slug>.md`
**Required frontmatter:** `type: failure`, `status`, `created`, `updated`, `sources`, `tags`. Optional: `projects`, `severity` (one of: minor / moderate / serious / critical — qualitative, not numeric).

**Content shape:**

```markdown
---
type: failure
status: active
created: 2026-05-09
updated: 2026-05-09
sources: [...]
projects: [q]
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

## Connections

- [[...]]
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
projects: [q, endstate]
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

## Connections

- [[...]]
```

## experiment

**Location:** `Notes/Experiments/<domain>/` where domain is `Food`, or another sub-domain that emerges (`Health`, `Workflow`, etc.). Add a new sub-domain only when the first experiment in that domain is being written.
**Naming:** `YYYY-MM-<slug>.md` — date-prefixed because experiments are time-bounded events, not evolving notes. Use the **start month**, e.g., `2026-05-30-day-low-carb.md`.
**Mutability:** Editable while ongoing; once concluded, body is read-only except for supersession or follow-up notes that link back.
**Required frontmatter:** `type: experiment`, `domain`, `status`, `created`, `updated`, `started`, `duration`, `tags`. Optional: `n` (sample size, default 1), `concluded`, `hypothesis`, `sources`, `supersedes`, `superseded_by`.

**Content shape:**

```markdown
---
type: experiment
domain: food
status: active
created: 2026-05-09
updated: 2026-05-09
started: 2026-05-09
duration: "30 days"
n: 1
hypothesis: "Eliminating dairy reduces sinus inflammation"
sources:
  - "[[Sources/Books/2026-04-the-elimination-diet]]"
tags: [diet, elimination, sinus]
---

# <Experiment name>

## Hypothesis

What I expected to happen, and why.

## Protocol

How the experiment is run. Specific enough that someone (including future me) could repeat it.

## Baseline

Starting state. Measurements, observations, qualitative descriptions.

## Intervention

What's actually being changed. The independent variable.

## Results

What happened. Both quantitative measurements (if any) and qualitative observations. Updated as the experiment runs; finalized at conclusion.

## Conclusion

What I learned. May be inconclusive — that's a valid result.

## Connections

- [[Entities/Concepts/Elimination Diet]]
- [[Domain - Health & Performance]]
- [[Notes/Insights/some-related-insight]]
```

### Experiments are not research notes

The distinction matters. A research note synthesizes secondary sources (papers, articles, conversations) to compile understanding. An experiment runs a protocol and captures primary data. They have different epistemic status: an experiment's `n=1` self-data is direct evidence about you; a research note is filtered through somebody else's frame.

When in doubt, ask: did I just *do* something and observe results, or did I *read about* something and synthesize? First → experiment. Second → research-note.

## production-log

**Location:** `Notes/Productions/<medium>/` where medium is `Reels`, `Episodes`, `PDFs`, `Posts`, or another medium that emerges. Add a new medium subfolder only when the first production in that medium is being written.
**Naming:** `YYYY-MM-<slug>.md` — date-prefixed by start month. Productions, like experiments, are time-bounded.
**Mutability:** Editable across the production lifecycle (planned → recorded → edited → published → reflected). Once the lifecycle is complete and reflection is logged, treat as read-only except for supersession.
**Required frontmatter:** `type: production-log`, `medium`, `status`, `created`, `updated`, `tags`. Optional but typically present: `projects`, `host`, `editor`, `recorded`, `published`, `sources`, `related`, `supersedes`, `superseded_by`.

**Status values for production-logs:**

- `planned` — design / outline phase
- `recorded` — primary capture done, edit pending
- `edited` — production assets finalized
- `published` — live; outcomes accumulating
- `reflected` — published + reflection complete; no further updates expected
- `dropped` — abandoned (use `archived` if you want it out of active rotation entirely)

**Content shape:**

```markdown
---
type: production-log
medium: reels
status: recorded
created: 2026-05-09
updated: 2026-05-09
recorded: 2026-05-09
published: null
projects: [tu]
host: Hugo
editor: Kim
sources:
  - "[[Sources/Sessions/2026-05-09-metabolism-curriculum-design]]"
related:
  - "[[Notes/Research/Together Unprocessed/metabolic-literacy-curriculum]]"
  - "[[Notes/Patterns/conversational-reel-script]]"
tags: [reels, metabolism, batch-02, tu]
---

# <Production batch name>

## Frame

What this production is, who it's for, why now. The strategic framing.

## Artifact

The actual creative output — scripts, outlines, assets, links to footage. The production's *content*. Multiple subsections per piece if it's a batch.

## Production session

How the capture/recording/writing actually went. Takes, edits, decisions made on the fly. Tables welcome — takes data, time per scene, etc.

## Outcomes

To be filled across the lifecycle. Engagement metrics, distribution-level signals, post-publish observations. Tables for per-piece metrics if relevant.

## Reflection

Filled once outcomes have settled. What worked, what didn't, what to keep / change / drop for the next production in this medium.

## Connections

- [[Notes/Research/<related-curriculum>]]
- [[Notes/Patterns/<applicable-pattern>]]
- [[Notes/Failures/<applicable-failure>]]
- [[Entities/Concepts/<core-concept>]]
- [[Domain - <relevant-domain>]]
```

### Production-log vs experiment

These are easy to confuse. Both are time-bounded with date-prefixed filenames; both have outcomes. The difference is the epistemic object:

- **Experiment:** a hypothesis tested under a protocol with primary data. Conclusion confirms / refutes / qualifies the hypothesis. The artifact's value is the *finding*.
- **Production-log:** a creative artifact + the production knowledge around it. Outcomes are engagement metrics, audience response, what to do differently next time. The artifact's value is the *thing made*.

Quick test: did Hugo set out to learn whether X is true (experiment) or to make a thing the world will see (production)?

### Production-log vs research-note

A research-note is *secondary synthesis* — reading and connecting. A production-log captures the production of a primary creative artifact and the operational knowledge around making it. The curriculum *informing* a reel batch is a research-note (`Notes/Research/Together Unprocessed/<curriculum>`); the reel batch itself, with scripts and recording session and metrics, is a production-log (`Notes/Productions/Reels/<batch>`).

## entity

**Location:** `Entities/<entity-type>/` where entity-type ∈ {People, Concepts, Libraries, Decisions}
**Naming:** Title Case, the entity's actual name. `Andrej Karpathy.md`, `Agentic RAG.md`, `pgvector.md`.
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
affiliation: Tesla / OpenAI alumnus
relationship: public-figure
tags: [ml, llm]
---

# Andrej Karpathy

## Summary

One paragraph.

## Why in the KB

What he's relevant to in Hugo's work.

## Connections

- [[...]]
```

### Concepts

Optional frontmatter: `domain` (e.g., retrieval, infrastructure, governance, metabolism).

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
project: q
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

## Connections

- [[...]]
```

## Naming conventions summary

- Sources: date-prefixed, dash-slug, lowercase: `2026-05-09-llm-wiki-v2-gist.md`
- Notes (research, insight, failure, pattern): no date prefix, dash-slug, lowercase: `agentic-rag-retrieval-budget.md`
- Experiments: date-prefixed (start month), dash-slug, lowercase: `2026-05-30-day-low-carb.md`
- Production-logs: date-prefixed (start month), dash-slug, lowercase: `2026-05-metabolism-basics.md`
- Entities: Title Case, actual name: `Andrej Karpathy.md`, `Agentic RAG.md`

The naming asymmetry is intentional. Sources, experiments, and production-logs are dated because their value is partly temporal (when was this captured / when did this run / when did this go out). Notes are not dated because they evolve. Entities are named after the thing they are.

## `_attachments/` convention

Any location holding compiled notes or source captures may have a sibling `_attachments/` folder for binary outputs or originals that need to live near the parent note. The underscore prefix marks it as not-a-primary-content folder (Obsidian convention).

**Allowed locations:**

- `Sources/Articles/_attachments/` — PDF or document originals the markdown source-note captures verbatim from.
- `Notes/Experiments/<domain>/_attachments/` — binary outputs an experiment generates (protocol docs, exports, clinical handoff versions).
- `Notes/Productions/<medium>/_attachments/` — raw/edited media masters for a production (use sparingly; large media usually stays in Drive).
- `Notes/Research/<scope>/_attachments/` — generated artifacts a research-note produced.

**Discipline:**

- **Append-only.** Binaries are not edited after placement. Revisions go in as new files (e.g., `protocol_v2.docx`, `protocol_v3.docx`).
- **No frontmatter** on binary files (none possible).
- **Referenced from the parent note**, never standalone. The parent note's body should contain a link to each binary in `_attachments/` it produced or captures from.
- **Filename = description.** Date prefix where temporal anchoring matters; otherwise descriptive slug. Versioning suffix (`_v2`, `_v3`) preserves history.
- **Not Evidence.** If the artifact came from a third party and must be preserved as-received without analytical processing (legal document, clinical letter from a medical team), it belongs in `Evidence/<scope>/<category>/` instead. See `write-scope.md` § binary placement.

## Research-note scope heuristic

A research-note's `project` field reflects *audience and reuse domain*, not *triggering occasion*. A bloodwork interpretation triggered by mother's chemotherapy is `project: health` because anyone running a mother-case protocol or thinking about chemo bloodwork would reuse it. A methodology critique triggered by the same incident is `project: science` (or an insight) because the reusable artifact is the epistemic frame, not the case.

When a note feels like it could go in two scopes, that's usually a signal it's an insight or pattern (cross-cutting) and should be promoted accordingly.

## Experiment hub-decomposition heuristic

Long-running experiments accumulate. The experiment note is the canonical aggregator and link hub, but it should not absorb arbitrarily large research sections. Decompose when **both** conditions hold:

1. A section of the experiment body exceeds ~500 words.
2. That section is referenced from ≥2 other compiled notes (or expected to be).

Decomposition: extract the section to `Notes/Research/<scope>/<topic-slug>.md` and replace the section in the experiment with a one-paragraph summary + wikilink. The experiment stays the hub; the standalone note becomes the reusable surface.

The audit operation surfaces hub-decomposition candidates as a proposal, never auto-decomposes.
