## Context

Governance already restricts disclosure:
- Scopes select pages by paths, projects, tags, types, classes and refs.
- Rules grant an audience a ceiling, optionally under a purpose.
- Every read door filters through the egress chokepoints.

There is no notion of what a page *is* in sensitivity terms. Agents compensate by omission. This change adds the label and the capture contract only. Disclosure by label comes in later changes, after inheritance along citations and withheld-equals-missing pass an adversarial re-run.

The server validates; the agent reasons (the "pure substrate" constraint in `openspec/config.yaml`). The capture server never reads content to guess a kind (`add.capture_kind`).

## Decisions

### A dedicated field, not the free-form `classes`
`classes` is an author-declared list that scopes may select on. It already has whatever meaning a vault gave it. Sensitivity gets its own field, `sensitivity`, so each concept has one name. Its values are keys of the `sensitivity-classes` registry. A value outside the registry refuses with `SENSITIVITY_CLASS_UNKNOWN` and names the registered keys.

### The governed pointer write carries the class on append-only pages
`page-fields` already admits one governed writer per pointer field on Sources and Evidence. The write proves every other byte identical and leaves a reason record that a revert replays.
- The new writer `classify` uses that write unchanged.
- Unlike `reclassify`, it never moves the file. Sensitivity is not where a page belongs on disk.

A binary Evidence file keeps its bytes. The class sits on its Evidence page (the sidecar).

Alternative rejected: a separate classification store keyed by page id. It keeps even frontmatter untouched, but:
- it adds a second metadata authority beside frontmatter;
- it is invisible in the vault's files;
- membership would need a new input path.

### Classification is the agent's judgement, with registry defaults
- The capture tools take `sensitivity` from the caller.
- When the caller gives none, a Source kind's optional `sensitivity` attribute supplies a default. This is registry data, so an owner sets it for a kind such as an identity-document photo in the vault overlay.
- Otherwise the page is unclassified.

The server adds no model call and no content matching:
- A model call would send the document to one more provider.
- A word list or pattern over content cannot decide what a document means (C6).

The shipped source-kinds carry no default, because none is unambiguously sensitive.

### `default_disclosure` is recorded now and enforced later
Each class entry declares `default_disclosure` (`owner-only` | `vault-default`). This change validates the attribute and reports it. The per-audience change makes it decide disclosure. The shipped pack sets `owner-only` for `identity`, `health` and `financial`. An overlay may add a class or retire a shipped one. Promotion is `propose-save`, because an entry bears behaviour.

### Reporting is honest about the absent policy
A capture or classify result carries:

```
sensitivity: {classes: [...], basis: declared | kind-default | none, disclosure: {...}}
```

`disclosure` comes from the live governance state:
- With no policy, it says that governance is off and every client signed in as the owner reads the page.
- With a policy, it reports the caller's own ceiling.

It never reports `owner-only` as enforced while nothing enforces it.

### Collections
A collection definition may declare `sensitivity`, and its rows take it. A row may add classes. A row never drops the collection's classes, so a collection-level class acts as a floor.

## Risks

- **Over-classification hides useful material later.** Mitigation: classes are cheap to change. Defaults attach to kinds, not to domains (a health-domain research note is not a health record).
- **The field must not widen access.** Every page is unclassified today, and this change adds no rule. Disclosure therefore stays exactly as it is.

## Migration

None in code. After release, the owner's agent classifies existing identity, health and financial pages through the classify operation, one page at a time, with reasons. Every step is reversible.
