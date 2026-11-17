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
the precedent directly: `[[Test Content Manual]]` resolves to a file whose
stem is `2026-05-15-tu-test-manual`. A link written as the human
title is already independent of how the file is named.

## Goals / Non-Goals

**Goals**

- One vault-level setting, not a per-call argument nobody remembers to pass.
- A `title` style that produces the same name on Windows, macOS and Linux.
- No change to any file already on disk, whichever style a vault uses.

**Non-Goals**

- Renaming existing files. Not in this change, and probably not ever as an
  implicit consequence of a setting — if a vault wants its history renamed, that
  is a deliberate, reversible, link-rewriting operation and its own change.
- Introducing a permalink. It would be the principled way to decouple identity
  from display, but it is a new frontmatter field on every note and a second
  resolution key; it should be proposed on its own merits, not smuggled in as
  the enabling half of a filename change.
- Renaming a Source or an Episode: a dated name stays as it is.

## Decisions

### The default is `title`, for new compiled notes and entities

The filename is the identity, so the default decides the address of every page
written after an upgrade. It is `title` because a title name is the readable one,
and because the name is link-safe: `corrected_title_name` turns `# [ ] ^ |` into
spaces and drops leading dots, so no title yields a name that a wikilink cannot
carry, and `fit_filename` keeps the name under the byte cap. A vault that wants
the old names sets `slug` in `EXOMEM_FILENAME_STYLE` or `project-keys.yaml`.

The risk of a mixed vault (old pages slugged, new pages titled) is accepted and
bounded: no file is renamed, and a link written by title resolves under both.
Case and Unicode-form clashes are caught by `unique_path` and the entity check
before any write.

A Source is `YYYY-MM-DD-<name>`. Its caller marks it `dated`; with no style
configured it keeps the slug form, so the default flip renames no Source. A style
that a vault or the environment sets applies to a Source as it did before.

An experiment and a production log are `YYYY-MM-<name>`: a compiled note, so they
take the default like any other. The caller passes the date text as `date_prefix`
(from the type's registry `stem`), and `fit_filename` counts it against the byte
cap. The name that `resolve_filename_slug` returns never includes the prefix, and
a `FILENAME_ADJUSTED` warning shows the whole stem that the page is linked by.

### A changed name is reported once

The title is stored as given. When the final file name differs from it (in NFC),
the write carries one `FILENAME_ADJUSTED` warning. The writer builds it where the
name is final, after `unique_path` has added a collision number, so the warning
names the stem that a link resolves by. It compares that stem with the title and
says whether characters were changed, the name was cut, or a number was added; it
shows the name, not a path. The rule functions are not changed here; the warning
is the only text this change adds. Building it from the final name, not from the
rule's own correction list, is what reports a character that the file-name
sanitiser removes (`|`, `/`) and a name that a collision number changed.

`unique_path` counts the collision number in the byte cap: it shortens the title
part, at a character, so that the name, the number and the extension stay at 200
bytes.

### Entities use the vault naming path

An entity filename used to come from a private sanitiser in `link.py`, which
dropped diacritics and kept other characters the vault path removes. The entity
writer now calls `resolve_filename_slug`, so one rule names every derived page.
An entity page keeps `Name With Marks.md`; the frontmatter title is untouched.

An entity is create-only, so a clash must refuse rather than disambiguate. The
writer lists the folder and compares each leaf by `held_fs.name_variants`, the
same key the vault uses for case and NFC. The refusal names the page that holds
the name, in that page's own spelling.

### Sanitise by the union, not by the host

The obvious implementation sanitises against the running platform's rules. That
produces a vault that is fine until it is opened elsewhere — a name with `:` or
`?` written on Linux cannot be checked out on Windows at all, and the vault is
explicitly a portable artifact synced between machines.

So the removed set is the union: `< > : " / \ | ? *`, control characters,
trailing dots and spaces, and the Windows reserved device names. A device name
leaves no usable name, so it falls back to the slug style; the slug (`nul`) is
still a reserved name on Windows, an open gap that the naming rule owns. macOS
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

### Length caps are fixed

#598 suggests truncating near 60 and says it "costs nothing". It does not: the
warning quoted above exists because a truncated name is not re-derivable from the
title, so it is the name the caller has to link to. Lowering the cap makes that
warning fire on more notes. The derived-name cap stays at 100 characters and the
file name stays within 200 bytes. Neither is a vault setting: the shipped code has
no key for it, and a key can be added if a vault asks for one.

## Risks / Trade-offs

- **A mixed-style vault.** Real, and accepted: it is the direct cost of never
  renaming. Title-keyed wikilinks resolve across both, and path- or stem-keyed
  links keep pointing at the file they always pointed at.
- **Spaces in filenames.** Some tooling handles them badly. They are already
  ubiquitous in Obsidian vaults, and the issue's own comparison shows the other
  indexer writing them into this same vault, so the vault already contains them.
- **`project-keys.yaml` gains a key.** That file is per-vault configuration read
  by `project_keys.py:93` and already the right home for the filename style. The
  scaffold copy under `src/exomem/_scaffold/` must stay generic —
  `test_scaffold_no_leak.py` enforces it — so the key ships documented and unset.

## Adjustment After Review

The first design moved the storage path of a page read and of a resolve-entity
candidate under `diagnostics`. The installed-wheel product end-to-end run and the
stdio import-guidance test both read `path` from a `read_page` reply, so the move
broke real clients. The reply now leads with the title and keeps `id`, `ref` and
`path` at the top level, as before. Title-first presentation to a person stays the
job of the served citation guidance. Search hits and write replies are unchanged
by this adjustment.

## Open Questions

- Is a permalink field worth proposing separately? It is the only thing that
  would make renaming existing files safe, and therefore the only route to a
  fully readable existing vault.
