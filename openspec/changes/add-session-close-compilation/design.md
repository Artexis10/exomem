## Context

`episode_memory` `record` writes a bounded recap as an `episode` Source and
binds it to the caller's own episode ledger. The recap is an agent-authored log.
Nothing in it points at the pages that the session changed, so a page can stay
stale while the recap says the work happened.

Three facts bound the design:

- The episode ledger binds recap inputs only. Ordinary writes (`edit_note`,
  `change_observations`, `supersede_note`) carry no episode key.
- So the server cannot know when a session started, or which writes belong to
  it.
- A client may have no hook, no skill and no second call. The tool response is
  the one channel that every client reads.

## Decisions

### Compile first, then record, with one extra step

The agent updates the changed pages with the writers it already uses, then
records the recap once. The close step adds one step to a flow that already
ends with one record call.

The typed candidate workflow (`EXOMEM_EPISODE_WORKFLOW`, off on the live
service) covers the same need with `prepare`, `disposition`, `resume` and
`coverage` calls for each candidate. It needs many calls, and agents skip
second calls. It also needs the service to enable it, and a client cannot
detect that without extra reads. Compile-first needs none of that, so it is the
default close step. The candidate workflow stays for the clients and services
that enable it.

### The server reports the pages, and the agent judges

The `compile` block lists pages the recap names. It makes no "written this
session" claim, and it holds no time-window constant. Each page carries its last
update time (frontmatter `updated`, else the file modification time), and the
agent compares it with the session it remembers.

### What the block lists

- The recap's `about` pages that the caller may see. These are the `about_paths`
  that the writer already proves visible.
- The pages that the `[[...]]` links in the Decided items resolve to. The
  writers' own resolver (`find.writer_resolver_snapshot`) resolves them over the
  pages the caller may see (`writer_link_visibility`).
- A page appears by path and title, so each listed page also passes the release
  filter. A stricter filter than `about_paths` costs nothing: a page at a
  notice level is omitted, never counted.
- A page the caller cannot see, or a link that resolves to no page, is omitted
  and never counted. `unlinked_decided` counts every Decided item with no
  resolvable visible link, so a withheld link and a missing link give the same
  block, byte for byte. The egress twin test pins this.
- With no listed page and no Decided item, the response has no `compile` key.

### Resolution runs before the write, and never walks the vault

The write leaves the resolver cache behind its own change until the terminal's
fanout catches it up. So `record` resolves the links before it writes. It takes
the writers' resolver snapshot with `allow_disk=False`. On a cold cache the
disk fallback walks the whole vault, which measured 3.9 to 5.9 seconds on a
5,000-page vault, and every record would pay it, not only the first. So the
snapshot declines and the block reports its links as unchecked, before any
`normalize_wikilink` call can build a resolver of its own. A resolution failure
or a declined snapshot never refuses a recap: the block says that its links were
not checked and omits `unlinked_decided`. Unknown is never zero. The state
follows the resolver cache, not the pages, so it leaks nothing about a withheld
page.

### The outcome states the result, and one conditional step follows it

The `outcome` sentence states the result only, for example "This recap names 2
pages; 1 decided item names no page." It never orders a write and never asks for
a second record: a recap request alone does not authorize page edits at `off`,
and a second revision would double the recap.

When the block lists a page and live policy permits proactive capture (the check
that `episode_due` already uses), a separate `next_step` says: "If a decided item
changes a listed page that this session has not updated, update that page." It is
a condition the agent judges, because the server cannot know which listed pages
the session updated. The Stop hook ask follows the same rule: its page step
begins "If live policy permits capture".

### Diagnostics stay out of the sentence

The time source of each page and the link-check state sit in the block's own
`diagnostics` part. The block is one entry of the compact envelope's episode
fields, so it reaches every client, and `user_summary` still leads the response.
A listed page that cannot be read keeps its place with `updated: unknown` and the
source `unreadable`: an unreadable page is not a page the recap fails to name.

### Spec placement

No canonical spec holds the `episode_memory` record contract. It lives in the
`memory-loop` and `episode-compilation` deltas of the still-active
`close-memory-loop` change. This change writes its delta against
`agent-bootstrap-contract`, the canonical spec that already holds the client
episode ask, the Stop-hook block limits and the scaffold's episode teaching.
Writing it there keeps this change independent of the open tasks of
`close-memory-loop`. The envelope behaviour reuses the compact projection of
`mutation-terminal-contract` and changes none of its requirements.

## Risks

- An agent can ignore the close step. The block and the Stop hook repeat it, and
  the real-model task measures how often agents follow it.
- A cold resolver cache makes the block report its links as unchecked, with no
  page counted from the Decided items. The `about` pages still list. The
  `compile` block is advisory, so the cost is a weaker hint, not a slower or
  refused record.
- A recap is an immutable Source and may now carry `[[...]]` links. A link whose
  target is missing or ambiguous stays in the recap, and no agent can repair it.
  The audit's link check does not exclude `Sources/Episodes/`. It reports such a
  link as `forward_reference` or `broken_wikilink` at `info` severity, marked
  immutable and not repairable in place. This change leaves the audit as it is.
- A link to a page that the session did not change appears in the block.
  Linking is the agent's claim that the item belongs to that page, and the
  agent judges whether the page needs an update.

## Proof plan

- Unit and integration tests in `tests/test_episode_compile.py` cover the
  listing, the egress twin, the unlinked count, the omitted block, the failed
  and the declined link check, the unreadable page, the conditional next step
  and the compact envelope of a real MCP call.
- The real-model task runs the close step on the real models with realistic and
  hostile sessions, and adds each failure as a permanent regression case.
