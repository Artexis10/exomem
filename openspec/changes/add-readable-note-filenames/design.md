## Context

The filename is the identity. That is the fact everything here follows from, and
it is the fact #598 assumed away.

`slugify_with_truncation_check` (`vault.py:370`) already says so in the warning it
emits:

> link to this note using `{slug}` — re-deriving a slug from the title will not
> resolve. Shorten the title if the truncation drops meaning.

There is no `permalink` field in exomem — `grep -rn permalink src/` returns
nothing. The comparison tool in the issue keeps a slugged permalink beside a
readable filename, so for it the filename is decoration. Here it is the address.

What makes the change tractable is that it is not the *only* address.
`WikilinkResolver` (`vault.py:4799`) keys four ways — full path, KB-stripped path,
filename stem, and lower-cased frontmatter **title** — with the docstring giving
the precedent directly: `[[North-Led Content Manual]]` resolves to a file whose
stem is `2026-05-15-tu-north-led-content-manual`. A link written as the human
title is already independent of how the file is named.

## Goals / Non-Goals

**Goals**

- One vault-level setting, not a per-call argument nobody remembers to pass.
- A `title` style that produces the same name on Windows, macOS and Linux.
- Zero change for any vault that does not opt in.

**Non-Goals**

- Renaming existing files. Not in this change, and probably not ever as an
  implicit consequence of a setting — if a vault wants its history renamed, that
  is a deliberate, reversible, link-rewriting operation and its own change.
- Introducing a permalink. It would be the principled way to decouple identity
  from display, but it is a new frontmatter field on every note and a second
  resolution key; it should be proposed on its own merits, not smuggled in as
  the enabling half of a filename change.
- Changing the default. Discussed below.

## Decisions

### The default stays `slug`

The tempting move is to default `title` on, since the issue argues it is the
better name and the product's pitch supports that. The reason not to: the
filename is the identity, so flipping the default would silently change the
address of every note written after an upgrade, in vaults whose owners never
asked. Half a vault named one way and half the other, caused by a version bump,
is worse than a vault consistently named the less pretty way.

New vaults can be offered the choice at setup, where the answer is a decision
rather than a side effect.

### Sanitise by the union, not by the host

The obvious implementation sanitises against the running platform's rules. That
produces a vault that is fine until it is opened elsewhere — a name with `:` or
`?` written on Linux cannot be checked out on Windows at all, and the vault is
explicitly a portable artifact synced between machines.

So the removed set is the union: `< > : " / \ | ? *`, control characters,
trailing dots and spaces, and the Windows reserved device names. macOS
additionally normalises to NFD on HFS+ and APFS behaves differently again, so
names are NFC-normalised before writing, which is what Obsidian and git both
expect.

### Remove rather than transliterate

`Q3: revenue / margin` could become `Q3- revenue - margin`. It does not: the
characters are removed and surrounding whitespace collapsed. A substitution
invents a character the user did not write and makes the filename disagree with
the title in a way that is harder to explain than an omission — and the title in
frontmatter is unaffected either way, so nothing is lost that the note does not
still carry.

### Case-insensitive collision is a new failure mode

Under `slug` style everything is lowercased, so two titles differing only by case
already produce the same name and hit the existing disambiguation. Under `title`
style they produce two *different* names that are the same file on Windows and
default macOS. The check therefore cannot be `path.exists()`; it has to be a
case-insensitive comparison against the directory, on every platform, so a vault
does not behave differently depending on where the write happened.

### Length cap stays at 100 by default

#598 suggests truncating near 60 and says it "costs nothing". It does not: the
warning quoted above exists because a truncated name is not re-derivable from the
title, so it is the name the caller has to link to. Lowering the cap makes that
warning fire on more notes. The cap becomes configurable, defaults to today's
100, and 60 is available to anyone who prefers shorter names to fewer warnings.

## Risks / Trade-offs

- **A mixed-style vault.** Real, and accepted: it is the direct cost of never
  renaming. Title-keyed wikilinks resolve across both, and path- or stem-keyed
  links keep pointing at the file they always pointed at.
- **Spaces in filenames.** Some tooling handles them badly. They are already
  ubiquitous in Obsidian vaults, and the issue's own comparison shows the other
  indexer writing them into this same vault, so the vault already contains them.
- **`project-keys.yaml` gains a key.** That file is per-vault configuration read
  by `project_keys.py:93` and already the right home for this. The scaffold copy
  under `src/exomem/_scaffold/` must stay generic — `test_scaffold_no_leak.py`
  enforces it — so the key ships documented and unset.

## Open Questions

- Should `exomem setup` ask, or default new vaults to `title` without asking?
  Asking adds a prompt to a flow whose value is being short; defaulting new-only
  makes two vaults on one machine differ for a reason their owner may not recall.
- Is a permalink field worth proposing separately? It is the only thing that
  would make renaming existing files safe, and therefore the only route to a
  fully readable existing vault.
