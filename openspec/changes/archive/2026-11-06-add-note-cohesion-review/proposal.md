## Why

Compiled notes grow until one page answers several independent questions. A user then has to notice the overload and ask an agent to split the note. Exomem ships two detectors that are meant to catch this, and neither works:

- The write-time lexical `scope_divergence` suggestion compares unit tags with the page's declared identity. On a labelled set of 40 large notes it flagged 8 of 16 overloaded notes and 7 of 22 coherent long notes. Every note it flagged had 13 or more units, so in practice it tracks size. It missed compact mixed notes, for example a short note that held an inventory, a how-to and a dated plan.
- The opt-in audit category `scope_divergence_semantic` clusters stored unit vectors. With every unit of the same 40 notes embedded and routing switched off, it flagged 0 of 40.

The judgement is about meaning: which independent questions does this page answer? The connected agent is a frontier model that reads the page and answers that question well whenever a user asks it. Generic destination guidance in tool descriptions did not change agent behaviour. A specific question at the right moment does.

## What Changes

- **BREAKING** Remove the lexical `scope_divergence` structural-promotion suggestion from compiled writes, with its resolution and size rules.
- **BREAKING** Remove the `scope_divergence_semantic` audit category and its detector.
- Add a `note_cohesion` review family. A compiled, non-breadth page that reaches a size step opens one review item. The steps start at 9 semantic units and grow by half each time (9, 14, 21, 32, 48, …). The item states counts only and never claims that the page is overloaded.
- When a compiled write commits to a page whose `note_cohesion` item is open, the write response carries one `structure_suggestion` of kind `note_cohesion`. It holds the counts, the review ref and fingerprint, and the question for the writing agent.
- The agent answers through existing surfaces. To keep the page whole, it dismisses the item through `triage_memory`; the page then stays quiet until its next step. To split the page, it proposes a split through the curation lane, which keeps the user's exact plan confirmation.
- The same item is part of the due-state projection, so notes that are already past a step form a visible backlog that any connected agent can work. It sorts after dated obligations and before the Source backlog.
- The item contract reserves an optional slot for a future server-side sensor reading. It needs no reading to be complete.
- The capture `source_classification_debt` suggestion is unchanged. It keeps the `structure_suggestion` channel.

## Capabilities

### New Capabilities

None. The family extends the existing attention queue.

### Modified Capabilities

- `command-surface`: replace the structural-promotion requirements with the note-cohesion write-time question; reword the capture-debt requirement that referred to structural advice.
- `attention-queue`: add the `note_cohesion` family, its size steps, its decision binding and its projection order.
- `action-first-audit`: remove semantic scope divergence.
- `agent-bootstrap-contract`: replace structural-promotion teaching with note-cohesion teaching.
- `delegation-envelope`: change the example family in one scenario.

## Impact

- Code: `structure_promotion.py` keeps only the term helpers that other modules import. `structure_promotion_semantic.py` is deleted. The audit, due-state, attention and write-path registrations change, along with the bootstrap and tool-description text and the scaffold references.
- Clients: compiled writes no longer return `kind: scope_divergence`. They can return `kind: note_cohesion`. A client that branched on `strength` or `cluster_terms` loses those fields; no shipped client does.
- Data: no migration. Existing `scope_divergence_semantic` review decisions become inert records.
