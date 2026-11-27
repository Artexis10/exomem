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

The server adds no model call and no content matching to decide a class:
- A model call would send the document to one more provider.
- A word list or pattern over content cannot decide what a document means (C6).

The server does already alter one kind of content: capture redaction removes Luhn-valid card numbers and labelled credentials from every kept text (`governance/capture_redaction.py`). This change keeps that behaviour and teaches agents the same authenticator rule.

A kind default reaches only a capture whose kind an agent or an owner chose. An Evidence upload that arrives without an agent takes a generic kind, so a kind default does not classify it; the owner classifies it later.

A kind default that names no registered class applies its registered keys and skips the rest. A label must never stop material from being kept. A class that the calling agent declares and the registry does not hold still refuses, naming the registered keys, because the agent can correct it in the same turn.

The shipped source-kinds carry no default, because none is unambiguously sensitive.

### A class is a label; disclosure stays in governance policy
A class entry has a label and a description and no attribute. Who may read a page of a class is decided where every other disclosure is decided: the `_Governance/` policy, under its fingerprint and its guards. The per-audience change adds a `sensitivity` scope selector beside `classes`, and an owner-only default is that scope with the existing `default_deny`. Keeping a disclosure default in the vocabulary registry would put a second, weaker-guarded policy store in `_Schema/`.

The shipped pack holds `identity`, `health` and `financial`, and they are fixed: an overlay row that names a shipped key is refused, and the shipped classes stay in force. An overlay adds a class. Promotion is `propose-save`, because a policy scope will name classes: a new class is a deliberate save, never a typo that a capture registers.

### A plain edit takes the same keys
On a compiled page, `sensitivity` is ordinary frontmatter, but a plain edit accepts only a list of registered keys, the same check that capture and classify apply. A string or an unknown key would otherwise read as unclassified to every reader of the field.

### Reporting is honest about the absent policy
A capture or classify result carries:

```
sensitivity: {classes: [...], basis: declared | kind-default | collection-floor | none, disclosure: {...}}
```

`disclosure` comes from the live governance state:
- With no policy, it says that governance is off and every client signed in as the owner reads the page.
- With a policy, it reports the caller's own ceiling.

It never reports a restriction while nothing enforces one.

### Collections
A collection definition may declare `sensitivity`, and its rows take it with basis `collection-floor`. A row may add classes through its own `sensitivity` field when its schema declares one. A row never drops the collection's classes, so a collection-level class acts as a floor. Classify covers pages only; a row's own classes change through an ordinary row edit, which can add but not remove floor classes.

### Granularity is the page
A class labels a whole page or row. A single sensitive fact inside a broader page, such as an identity number in an observation on an entity page, makes the whole page carry the class. Finer granularity is left to the propagation and per-audience changes, which decide how restriction treats a page whose only sensitive part is one fact.

## Risks

- **Over-classification hides useful material later.** Mitigation: classes are cheap to change. Defaults attach to kinds, not to domains (a health-domain research note is not a health record).
- **The field must not widen access.** Every page is unclassified today, and this change adds no rule, so the field itself changes no one's access.
- **More sensitive material is stored before any restriction exists.** The instruction change makes agents keep identity, health and financial material that they used to refuse. Until the per-audience change ships, every owner client, recall injection, search and backup reads it, and on a hosted cell so can the operator. Capture results say that governance is off, and agents never tell a person that a page is protected unless a policy applies.
- **Any writer can clear a class.** Clearing a class with a reason is recorded and reversible. Once a policy restricts by class, clearing one widens access, so the per-audience change decides whether a clear needs the owner.

## Migration

None in code. After release, the owner's agent classifies existing identity, health and financial pages through the classify operation, one page at a time, with reasons. Every step is reversible.
