## Why

The filename is product surface here, not an internal key. The pitch is "plain
Markdown in a vault you own — edit it anywhere, forever", and Obsidian's quick
switcher, file explorer, graph labels and hand-typed wikilinks all display and
target the **filename**, not the frontmatter title. `ls` and `grep -l` print
filenames too.

Today every derived filename is the whole title kebab-slugged, capped at
`SLUG_MAX_LENGTH = 100` (`vault.py:61`). These invented examples show its shape:

```
80  support-billing-review-the-missing-credit-and-the-case-204-component-mapping-gap.md
41  quarterly-audit-summary-checks-120-to-128.md
```

The same invented titles under a readable filename style:

```
82  Support billing review — the missing credit and the CASE-204 component mapping gap.md
43  Quarterly audit summary — checks 120 to 128.md
```

The numbers count characters in each filename stem. The shorter pair shows
the same title under each style:

```
slug : quarterly-audit-summary-checks-120-to-128.md
title: Quarterly audit summary — checks 120 to 128.md
```

A long kebab string is a fine permalink and a poor thing to pick out of a
list of a hundred.

## What the issue got wrong, and why it matters here

#598 proposes "keep the permalink slugged regardless — frontmatter already
carries `permalink`, so the two concerns are separate today and only the filename
needs to change."

**Exomem has no permalink.** `grep -rn permalink src/` returns zero matches. That
is the comparison tool's model, not this one's. In exomem the filename slug *is*
the durable identity, which `slugify_with_truncation_check` states outright when
it truncates:

> link to this note using `{slug}` — re-deriving a slug from the title will not
> resolve.

So this is not a cosmetic rename behind a stable identifier. It changes the thing
links point at, and the proposal has to carry that weight rather than assume a
separation that does not exist.

Two facts make it tractable anyway:

- `WikilinkResolver` already resolves by frontmatter **title** as well as by path
  and stem (`vault.py:4812`), with the documented precedent that
  `[[Test Content Manual]]` resolves to a file whose stem does not match its
  title. A link written as the human title keeps working under either style.
- The style only has to apply to **newly derived** names. Nothing requires
  renaming what is already on disk.

## What Changes

- Add a vault-level **filename style**, `title` or `slug`, resolved once for the
  vault rather than per call. Precedence: explicit per-call `slug` argument
  (unchanged, still strict ASCII kebab), then `EXOMEM_FILENAME_STYLE`, then the
  vault's `_Schema/project-keys.yaml` key, then the built-in default.
- Under `title` style, a derived filename preserves the title's capitals, spaces
  and inner punctuation, removing only what a filesystem or Obsidian cannot carry:
  the Windows-reserved set `< > : " / \ | ? *`, control characters, reserved
  device names, and trailing dots or spaces. The result is NFC-normalised.
- **The default is `title`.** A vault that sets no style names each new compiled
  note and entity by its title, and `slug` is the opt-out through
  `EXOMEM_FILENAME_STYLE` or the vault key. The default changes the name of new
  pages only: no file on disk is renamed. A Source
  (`<date>-<name>`) keeps its slug name when no style is set, and so does an
  Episode. An experiment and a production log (`YYYY-MM-<name>`) take the
  default and keep their date prefix.
- **A title name is link-safe.** The name passes through `corrected_title_name`
  and `fit_filename`: the characters `# [ ] ^ |` that break a wikilink become
  spaces, leading dots are dropped, and the name is cut under the byte cap. The
  title is stored as given. When the name differs from the title, the write
  carries one `FILENAME_ADJUSTED` warning that names the final file name and whether
  the name was cut.
- **`exomem doctor` shows the filename style** and where it came from
  (environment, `project-keys.yaml` or the default).
- **A public page read and a resolve-entity candidate lead with the title.** The
  reply carries the title and the handle (`id` and `ref`) that every tool accepts;
  the path stays at the top level, where clients read it.
- **Entity creation uses the vault naming path.** An entity filename is derived
  by `resolve_filename_slug`, as a compiled note's is, so it keeps the name as
  written: diacritics, case and punctuation. It no longer passes through a
  second, private sanitiser. The existence check is case- and NFC-insensitive
  against the folder, so two titles that map to one file are refused before the
  write. An entity follows the vault's style, so a vault that keeps `slug` names
  its entities that way.
- Existing files are **never renamed** by this change. The style governs
  derivation of new names only, so no link that resolves today stops resolving.
- Keep the derived-name cap at 100 characters, cut on a word boundary, and keep
  the whole file name within 200 bytes. #598 suggests 60; that is not adopted,
  because truncation is exactly what produces the non-re-derivable slug the
  warning above describes — shortening the cap makes that warning fire more
  often, so it is a tradeoff rather than free. Neither cap is a vault setting.
- Collision handling is style-independent: a derived name that already exists
  gets the existing disambiguation, and two titles differing only by case collide
  on case-insensitive filesystems and must be detected before the write, not
  after.

## Impact

- Affected specs: `vault-file-naming` (new capability).
- Affected code: `vault.slugify_title`, `vault.resolve_filename_slug`,
  `vault.slugify_with_truncation_check`, the `_Schema/project-keys.yaml` reader
  in `project_keys.py`, the entity writer in `link.py`, and the tool surfaces that
  accept a `slug` argument.
- No migration. No existing file changes name, and a link that resolves today
  keeps resolving. A vault that sets no key names its new compiled notes and
  entities by title from this release; a vault that wants the old names sets
  `filename_style: slug`.
- Cross-platform: the reserved-character set is the union across Windows, macOS
  and Linux, applied everywhere, so a vault authored on one and opened on another
  cannot contain a name the other cannot represent.
