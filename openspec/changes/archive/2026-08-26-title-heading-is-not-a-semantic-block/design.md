# Design: title-heading-is-not-a-semantic-block

## Context

Two rules that are individually reasonable compose into a destructive one.

Blocks are typed from any ATX heading whose normalized text matches the
vocabulary, at any level. A block ends at the next heading whose level is less
than or equal to its own, and deeper headings stay in its body — which is the
right rule for `## Claim` containing a `### Detail`.

At level 1 the closing condition can essentially never fire, because a page has
one title heading. So a matching H1 opens a block that runs to end-of-file and
absorbs every `##` block in the page.

## Why the fix is the level, not the vocabulary

Three alternatives were considered.

**Rename or narrow the vocabulary** so page-shaped words like `source` are not
block types. Rejected: the vocabulary is specified, shared with the graph and the
scaffold, and `source` is a legitimate block kind inside a page. The collision is
with the *position*, not the word.

**Parse nested blocks instead of swallowing them**, so a level-1 block would
contain level-2 blocks as siblings in the output. Rejected as a much larger
change to block identity and containment for a defect whose whole cause is that
one specific level has no closer.

**Treat only the first heading as a title.** Nearly equivalent in practice and
harder to state: it makes the meaning of a heading depend on what preceded it,
and gives a page with two H1s two different answers for the same text.

Scoping blocks to level 2 and deeper is the smallest rule that removes the
failure, and it is the rule the vault already follows everywhere else. Every
page-type template in `references/page-types.md` opens `# <Title>`; `add.py`,
`note.py`, `link.py` and `preserve.py` all emit `# {title}` and then `##`
sections. The parser was the only component reading an H1 as content.

## What this does not change

The `## Evidence: shot.png` class of title is unaffected either way — a title
carrying a colon and a filename never normalized to a bare block name, so pages
written by the preserve path were never at risk. That is worth stating because it
bounds the correction: this changes pages whose title is *exactly* a block-type
name, and nothing else.

Deeper nesting keeps its current meaning. A `### Decision` inside a `## Claim`
remains part of the claim's body, which is the documented and intended rule.

## Risks

1. **Block keys move for affected pages.** A page previously parsed as one
   swallowing block now yields its real blocks, so both the key set and the text
   identity change. That is the correction landing, but it means the graph will
   re-index those pages and any recorded reference to the old swallowing block's
   key stops resolving. Such a key only ever described a mis-parse.
2. **A page that genuinely wanted a level-1 block loses it.** No writer in the
   codebase emits one and no template documents one, so this is theoretical; it
   is stated because the parser is shared and vault content is user-authored.
