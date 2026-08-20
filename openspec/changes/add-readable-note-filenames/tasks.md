## 1. Naming Primitives

- [x] 1.1 Add a pure `sanitize_title_filename(title)` in `vault.py`: strip
      `< > : " / \ | ? *` and control characters, collapse the whitespace they
      leave, drop trailing dots and spaces, rename Windows reserved device names,
      NFC-normalise, and return `""` when nothing survives.
- [x] 1.2 Unit-test it before any wiring, over the union-of-platforms table:
      each reserved character, each reserved device name, a title that sanitises
      to empty, a title already clean, and an NFD input normalising to NFC.
- [x] 1.3 Make the derived-name cap a parameter rather than the module constant,
      defaulting to `SLUG_MAX_LENGTH`, and keep truncation on a word boundary.
- [x] 1.4 Pin that `sanitize_title_filename` output is byte-identical on Windows,
      macOS and Linux for the same input — the property the union exists for.

## 2. Style Resolution

- [x] 2.1 Add `resolve_filename_style(vault_root)` with the documented
      precedence: explicit call argument, `EXOMEM_FILENAME_STYLE`,
      `_Schema/project-keys.yaml`, built-in default `slug`.
- [x] 2.2 Test each precedence step in isolation, including an unset vault
      returning `slug`, and an invalid value being refused rather than silently
      falling back.
- [x] 2.3 Read the key through `project_keys.py`'s existing reader; do not add a
      second YAML path.
- [x] 2.4 Document the key in the `_Schema` scaffold, shipped **unset** and with
      no personal or product-specific example — `tests/test_scaffold_no_leak.py`
      fails otherwise.

## 3. Derivation

- [x] 3.1 Route `resolve_filename_slug` through the resolved style, keeping the
      explicit-`slug` branch and its strict ASCII kebab validation untouched.
- [x] 3.2 Fall back to slug style for a single note whose title sanitises empty,
      rather than failing the write.
- [x] 3.3 Replace the existence check with a case-insensitive one on every
      platform, and route a collision into the existing disambiguation.
- [x] 3.4 Test that a `title`-style write into a directory holding a name
      differing only by case leaves the existing file's bytes unchanged.

## 4. Compatibility

- [x] 4.1 Assert the default path is byte-identical to today: a vault with no
      configuration derives the same names it derived before this change, over
      the existing naming test corpus.
- [x] 4.2 Assert no code path renames, moves, or rewrites an existing file when
      the style changes.
- [x] 4.3 Assert a wikilink written as a frontmatter title resolves in a vault
      containing files named under both styles.
- [x] 4.4 Grep every consumer of `slugify_title`, `slugify_with_truncation_check`
      and `resolve_filename_slug` before finishing, and record the list in the
      PR — the working agreement, and the two misses this repo has already had
      were both inventory defects.

## 5. Surfaces And Evidence

- [ ] 5.1 Surface the resolved style in `exomem doctor` so a user can see which
      one a vault is on without reading YAML.
- [ ] 5.2 Extend the tool-argument documentation for `slug` to say what it now
      overrides.
- [x] 5.3 Write a real vault under each style and record the resulting `ls`
      side by side in the PR, the way #598 states the problem.
- [x] 5.4 Validate the OpenSpec change artifacts.
