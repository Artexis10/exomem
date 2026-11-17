# Proposal

## Why

A user often returns to a subject they researched in depth before. They want the whole body of compiled notes back: the conclusions, the sources, the open questions, and what changed since they last read it. Today an agent searches the store and writes a fresh synthesis on every return. The result is slow, and its coverage changes from one attempt to the next. A deep dive usually spans several linked research notes, their cited Sources and episode captures, so a single search hit rarely brings back the whole subject.

The existing topic profile (`exomem://profile/topic/<key>`) does not serve this need. It is keyed by a registry domain, which is much wider than one subject. Its lanes are shaped by roles such as preferences, recent changes and identity. It returns compiled pages only as pointers, and it is recompiled on every read.

## What Changes

- Add a **topic brief**: `read_memory(path=<topic text, page title or ref>, brief=true)`, exposed as `read_page(part="brief")` on MCP. It returns one assembled brief for the subject.
- Resolve the subject to a **seed page**: either the named page or the top compiled-note hit of the existing hybrid search. Up to five other candidate seeds come back as `alternatives`.
- Gather **members** deterministically from the seed. The walk follows the relation families of a new shipped traversal profile, `brief`, and adds the cited Sources. The member caps are fixed. A capped or unconnected candidate stays visible as `missing` or `related`.
- Assemble four sections from the registries' closed traits:
  - `conclusions`: the assertion-trait units and the categorised observations of each member;
  - `sources`: the cited Source pages and the external links in member bodies;
  - `open_questions`: the question-trait units that no relation closes;
  - `changed_since`: the members and units that changed since this principal's last complete read.
- Serve the brief as a new derived view, `exomem://brief/<seed id>`, with the existing pagination, continuation and terminal egress guard.
- Cache a compiled brief until a member page or a registry changes, with the input-digest pattern of the stance block.
- Keep a bounded, per-principal read watermark in a private sidecar.
- Add the heading aliases `open_threads` and `open_thread` to the `open_question` kind.

## Capabilities

### New Capabilities

- `topic-brief`: one bounded, cached, paged brief of a researched subject, with per-principal change tracking, on every read surface.

### Modified Capabilities

None. The `read_memory` signature gains the optional `brief` input under the existing surface-parity contract.

## Impact

- New module `src/exomem/topic_brief.py` and a watermark sidecar in the state root.
- Changes to the view ref parser, the `read_memory` dispatch, the `read_page` wire model, the terminal egress guard, `kinds.yaml`, `traversal-profiles.yaml` and the scaffold operations reference.
- No new public tool and no change to activation.
- The server makes no model calls. The client still writes the prose, from a complete and cited brief.
