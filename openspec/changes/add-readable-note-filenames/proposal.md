## Why

The filename is product surface here, not an internal key. The pitch is "plain
Markdown in a vault you own — edit it anywhere, forever", and Obsidian's quick
switcher, file explorer, graph labels and hand-typed wikilinks all display and
target the **filename**, not the frontmatter title. `ls` and `grep -l` print
filenames too.

Today every derived filename is the whole title kebab-slugged, capped at
`SLUG_MAX_LENGTH = 100` (`vault.py:61`). Observed on a real vault (#598):

```
96  yadm-dotfiles-repo-clone-it-rather-than-worktree-it-and-diff-test-failures-against-a-baseline.md
70  zellij-session-serialization-what-cold-recovery-can-and-cannot-know.md
```

against a second tool writing the same titles into the same vault:

```
91  Non-billed GGR — the missing-brand-deal control and the BO-2697 sub-product blind spot.md
58  Exomem first-run defect inventory — issues 477 to 485.md
```

Both are long. Only one is scannable. The pair with identical content makes it
plainest — same title, same day, same vault:

```
exomem: exomem-first-run-defect-inventory-issues-477-to-485.md
other : Exomem first-run defect inventory — issues 477 to 485.md
```

A 96-character kebab string is a fine permalink and a poor thing to pick out of a
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
  `[[North-Led Content Manual]]` resolves to a file whose stem does not match its
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
- **Default stays `slug`.** Changing the default would change the identity of
  every note written by an existing install on upgrade, which is not a default's
  job to do. New vaults may opt in at setup; existing vaults opt in deliberately.
- Existing files are **never renamed** by this change. The style governs
  derivation of new names only, so no link that resolves today stops resolving.
- Cap derived names on a word boundary at a configurable length, defaulting to
  the current 100 so behaviour is unchanged unless asked for. #598 suggests 60;
  that is offered, not imposed, because truncation is exactly what produces the
  non-re-derivable slug the warning above describes — shortening the cap makes
  that warning fire more often, so it is a tradeoff rather than free.
- Collision handling is style-independent: a derived name that already exists
  gets the existing disambiguation, and two titles differing only by case collide
  on case-insensitive filesystems and must be detected before the write, not
  after.

## Impact

- Affected specs: `vault-file-naming` (new capability).
- Affected code: `vault.slugify_title`, `vault.resolve_filename_slug`,
  `vault.slugify_with_truncation_check`, the `_Schema/project-keys.yaml` reader
  in `project_keys.py`, and the tool surfaces that accept a `slug` argument.
- No migration. No existing file changes name. A vault that never sets the key
  behaves exactly as it does today, which is the property that makes this safe to
  land before anyone decides what the default should eventually be.
- Cross-platform: the reserved-character set is the union across Windows, macOS
  and Linux, applied everywhere, so a vault authored on one and opened on another
  cannot contain a name the other cannot represent.
