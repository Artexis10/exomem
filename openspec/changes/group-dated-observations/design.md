# Design

## Context

The writer uses the same Markdown grammar as the semantic reader. The reader already recognises a `### Record` within `## Observations`. In a parser probe, it returned the dated record and the preceding compact line as distinct units and stopped at the next level-two section.

## Decisions

- Use the writer's fence-aware scan of `## Observations` for both placements. Insert compact lines before the first child heading. Use the repository's CommonMark parser to confirm the section's end before placing a dated block there. The next heading at level one or two can end the section.
- Preserve the heading level on subsequent content, assessment and link edits that keep a rich block in its observation section. If a nested rich unit becomes compact, move its line to the compact part of that section. Each move preserves the anchor.
- Check every new or edited nested block for headings and fences that prevent nesting. If the destination is uncertain, try the end of the note and name that placement on success. The existing round-trip check refuses without writing if the body cannot be stored whole there either.
- Check the original span before replacing, moving or removing a nested record. If the CommonMark outline puts a peer or parent heading inside that span, refuse without writing. A page edit can separate the heading before a caller retries.
- Use the current guarded preview and commit path. The guard binds the proposed bytes, so a token for the previous placement cannot commit the new one. A fresh preview binds the new placement.
- A compact correction that names no kind uses the shipped kind with the `replacement` trait. The pack must name exactly one such kind. A kind named by the caller or by a rich target takes its existing path. This keeps custom kinds available and avoids making every caller disambiguate an ordinary compact correction.

The tool chooses placement from the note and the supplied Markdown. It needs no new caller argument or model decision. This follows the guidance on making a tool serve the whole workflow in [Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents).

## Rollout

Apply the placement to new implicitly dated blocks and compact units that gain a date. Blocks in the observation section keep their heading level when the edited body still fits. The release needs no migration.
