## Why

New sources still land in `Sources/Other`. A capture with no kind, or with `other`, resolves to that catch-all, and an agent refused for another reason uses it as an escape: in live use, an agent holding a video transcript with no URL chose `other` to get past the URL refusal. The catch-all hides classification debt instead of recording it, and the owner wants no new source filed under it.

The kind vocabulary is already open, and a new slug registers on capture. So nothing forces a catch-all: the agent that reads the material can always name its kind, existing or new. Only a capture with no agent in the loop, such as a person typing into the terminal UI, has nobody to ask.

## What Changes

- An agent-facing capture (`capture_source`, `add`) refuses a missing kind, `other` and `unclassified` with `SOURCE_KIND_REQUIRED`, before any byte is fetched or written. The refusal lists the vault's choosable kinds with their use counts, most used first, capped at 30, and states what each count measures. Failed or incomplete reads produce visible count states and unknown values, never authoritative zeroes. It gives one rule: pick the closest kind or name a new slug. It never guesses a kind from the content.
- A capture with no agent in the loop records a missing kind as `unclassified`, a new built-in kind filed under `Sources/Unclassified/`. These are the terminal UI, the hosted web capture box, the upload form and a legacy-vault import. The surface adapter decides this, never an argument, so an agent cannot claim it.
- `other` becomes a deprecated built-in. Legacy `Sources/Other/` pages stay readable, findable and filterable by `source_kinds=["other"]`. No new capture or reclassification writes `other` or `unclassified`.
- The `source_classification_debt` advisory now reports debt instead of a recurring fallback pattern. A capture that commits while the vault holds sources with no chosen kind carries one moderate advisory. It holds the count and the folders that hold them: `Unclassified`, legacy `Other`, and `Imported`. The count comes from the per-folder counts the capture already takes, and a caller other than the owner gets no advisory.
- The URL refusal for `article`, `paper` and `video` tells the agent to supply the URL or to name what it actually holds as `source_kind`.
- Bootstrap's kind rule, the scaffold references, the capture and ingest workflow skills, the knowledge packs and the docs stop offering `Sources/Other` as a destination.

## Capabilities

### Modified Capabilities

- `command-surface`: source capture requires a kind from an agent, records `unclassified` when no agent is in the loop, retires `other`, reports classification debt, and names an alternative on a URL refusal.
- `agent-bootstrap-contract`: bootstrap teaches that every capture names a kind and that there is no fallback.

## Impact

The capture write path, the reclassification target check, the hosted refusal table, the upload route, the terminal UI capture call, the legacy-vault import, the committed-terminal advisory bounds, the bootstrap source-taxonomy block, the scaffold skill and its references, and the knowledge packs change. Generated surfaces are regenerated from their generators.

An agent-facing caller that captured without a kind now gets a refusal it must answer. The hosted web capture box keeps working without a change in `substrate`, because the cell recognises its private command router.

## Compatibility migration

This change deliberately migrates capture behavior on released hosted profiles. Historical command descriptors remain immutable, including their descriptions and optional argument shapes. Their existing open-string kind arguments support the correction without a transport version or a new client field.

Advance the existing bootstrap operating-contract version. Bootstrap and `SOURCE_KIND_REQUIRED` guidance state that current capture rules supersede earlier instructions permitting omission or `other`. Project the same rule into current compact guidance and every historical bootstrap profile. Correct current candidate descriptions through the existing generators.

Release runtime enforcement, bootstrap guidance and corrective refusals together. A caller following historical guidance receives a refusal before effects, then retries with a meaningful kind through either existing argument. Existing source pages need no migration. Automation that omits a kind must supply one before capture resumes.

Out of scope: moving the legacy `Sources/Other/` pages, which is the next slice through `reclassify`; the note-type registry; Planning and collection vocabularies.
