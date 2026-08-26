# Proposal: title-heading-is-not-a-semantic-block

## Why

A page whose title happens to be a block-type name loses every semantic block it
contains.

The parser types any ATX heading whose normalized text matches a supported block
type, at any level, and a block runs until the next heading at the same level or
shallower. A level-1 heading has almost never has a closer, so it runs to the end
of the file — swallowing every `##` block inside it.

Measured, with the same body under different titles:

```
# Evidence: shot.png   -> [('decision', 2)]     the real block is parsed
# Riverside minutes    -> [('decision', 2)]     the real block is parsed
# Source               -> [('source', 1)]       the whole page, one block
# Decision             -> [('decision', 1)]     the whole page, one block
# Open Question        -> [('open_question', 1)]the whole page, one block
```

The failure is silent and total: no finding, no warning, and the page's genuine
`## Decision` never reaches the graph, the index, or recall. It needs no unusual
authoring — a note titled `Decision`, `Source`, `Claim`, `Definition`, or
`Procedure` is ordinary, and `source` is exactly what a source page would be
called.

The vault's own conventions already settle what a level-1 heading means. Every
page-type template in `references/page-types.md` opens with `# <Title>`, and
every writer in the codebase emits `# {title}` followed by `##` sections. The
parser is the only component that reads an H1 as anything else.

## What Changes

- A level-1 heading SHALL NOT start a semantic block. Blocks begin at level 2 and
  deeper, which is where every writer and every documented template puts them.
- Nesting behaviour is otherwise unchanged: a block still ends at the next
  heading at its own level or shallower, and deeper headings still remain part of
  its body.

Deliberately unchanged: the supported block-type vocabulary, normalization,
validation findings, and the treatment of unrecognized headings as ordinary
Markdown.

## Capabilities

### Modified Capabilities

- `semantic-block-schema` — block parsing is scoped to level 2 and deeper, so a
  title heading cannot swallow the page.

## Impact

- `src/exomem/semantic_blocks.py`, the single parser.
- Any page currently mis-parsed gains its real blocks on the next read. Block
  keys for those pages change, because the block text identity changes — that is
  the correction, not a regression.
