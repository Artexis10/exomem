# Proposal

## Why

Speech-to-text and quick typing often split a closed compound with a space. A turn that writes "home work tracker" for an anchor named "Homework Tracker" makes no contact with that anchor's name. The half "home" equals the one-word name of an unrelated project, so the project resolves on `exact_alias` and its units fill the packet. Field reports on 2026-10-17 and 2026-10-21 show this failure on a live vault.

## What Changes

- Join two adjacent content tokens of one clause into a turn term. The join is a lead only, never a phrase, so it never earns `exact_alias` alone.
- When a join equals a name term of any indexed anchor the caller can see, the turn wrote that word: its two halves name nothing on their own. A half that the turn also writes on its own still counts.
- Do not join two tokens when exactly one is capitalised inside a sentence: "the book Mark lent me" is a word and a name.
- Read earlier conversation entries the same way, against the whole visible catalogue.
- The audience decision reads the same turn words as candidate matching, so an anchor that the turn reaches only through a join is decided for a non-owner reader.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: Read a spaced closed compound as its closed word.

## Impact

The change affects how `working_set_resolve` reads a turn's words. It adds no vocabulary, word list or setting. Whether a pair is a compound is decided by the vault's own authored names. Plural folding, containment in unspaced scripts, budget allocation and role precedence do not change.
