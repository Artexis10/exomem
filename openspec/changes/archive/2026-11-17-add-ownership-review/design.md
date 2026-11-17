## Context

A page's project is an agent's choice that nothing re-examines. The evaluation behind this change (2026-11-16, protocol frozen before its one held-out run) tested a closed-label sensor on a small hosted model, local CPU models, a model-free selector feeding a judge, and the connected agent answering a spelled-out question. The agent question won and was re-derived by a second evaluator. Its gain came almost entirely from existing pages (10 of 11 points); at write moments it broke even. Local CPU models took one to thirteen minutes per page on a loaded host, and the hosted sensor's content filter refused two benign private pages, so neither is a fit for this job.

Two findings shaped the question text. Showing the home's nearest pages added nothing, because misfiled pages that already sit in a home read as evidence that they belong. An explicit, general ownership test and a deferral rule were what moved the result.

## Goals / Non-Goals

**Goals:**
- Ask about every eligible page eventually, without the user's prompt, at a bounded cost per session.
- Ask the most likely misfilings first.
- Keep every change to a page behind the existing review and curation surfaces.

**Non-Goals:**
- No server-side model call. The connected agent judges.
- No automatic move. An answer recommends; the user confirms any change.
- No nesting of projects. Related projects are linked by typed relations between their notes.

## Decisions

### 1. One family on the note-cohesion carrier

`ownership_review` follows `note_cohesion`: an audit category, a review item per page with the standard fingerprint, dismiss, snooze and reopen semantics, a due-state backlog category, and a `structure_suggestion` kind on the write path. Reusing the carrier keeps one review model for agents and one projector pattern in `mutation_terminal`. The kind is a closed protocol token that code implements; it joins the existing kind ladder with a one-line reason.

### 2. The question text lives in the guidance routes

The ownership test and the deferral rule are guidance that may change as models change, so they live beside `destination_fitness` in the semantic authoring routes, not as a code constant. The item's detail and the write-path question both read the same route. The text is the evaluated text, with the call spelled for the serving surface through `capabilities.active_call_text`.

### 3. Eligibility

A page is eligible when its note type has role `compiled`, its status is live, it lies in a read-write tree, it is recall-eligible, and it carries a project key. Hubs and snapshots (the breadth tags) are not asked, because they span projects by design. Sources and Evidence are not asked; their project pointer follows their notes through a curation `edit` step.

### 4. When an item opens

An item opens when:
- a page is created with a project key;
- a page's project key or title changes;
- the rotation reaches a page that was never asked.

A page that was never asked and that the rotation has not reached has no item. This keeps the served counts to asked-and-open items. The projection also reports how many eligible pages were never asked, as the rotation's progress.

### 5. Signal version: project, folder and title

A decision binds to the page's project key, its home folder and its title. A dismissal holds while all three stay the same; a change to any reopens the item with a new fingerprint. Body growth alone does not reopen it: that is the deferral rule in data form, and `note_cohesion` already asks about growth. Cost when this fires wrongly: a page that drifts to a new subject under the same title is not re-asked until a rotation pass or a retitle.

### 6. The rotation

Each day the projection opens items for at most `rotation_daily` never-asked pages (default 3). Within each project it orders pages by distance from the project's centre, farthest first, and it takes projects in turn so that one large project cannot hold the whole slice.

The distance is the cosine distance between a page's mean semantic-unit vector and the mean of its project's other pages. A bulk refresh computes it per project with the paginated vector read that exists for sweeps, off the write path, and only for projects whose membership changed since the last refresh. A project with fewer than three pages has no distance; its pages are taken in authored-date order.

The distance orders the rotation and never filters it, so a page the distance ranks low is still asked in time. As a filter in the evaluation, the same signal missed half of the misfiled backlog pages.

### 7. Serving

Open items join the due-state backlog after every dated obligation and before `note_cohesion`, because a wrong home misroutes everything written after it. Counts are exact. A session sees them through the existing served block (top rows) and through `list_review_items` with the new queue.

The write response of a page with an open item carries the question as a `structure_suggestion` of kind `ownership`. A curation preview whose steps create or move notes carries one ownership question that names each such step by its step id.

### 8. Answers

- **Fits:** one `decide_review_item` dismissal with an `intentional:` reason that names the project goal the page serves.
- **Abstain:** a dismissal with a `deferred:` reason, or no call.
- **Move, new project or split:** a curation plan, which the user confirms by its exact id and fingerprint. A new project is registered through the existing project-key route before the move. A move uses the `move` step's `fields` patch to set `project`. Related projects get a typed relation between their notes.

The question spells out the dismissal call and names the curation route. It never changes a page itself.

## Risks / Trade-offs

- [Token cost per write] → The question rides only writes to a page whose item is open, and an answered item stays closed until project, folder or title changes. The question block is bounded to 400 tokens.
- [An agent skims past the question] → The question spells out the exact call, which earlier evidence showed agents need before they act. A cross-client real-model run checks the surfacing rate on both MCP transports before release.
- [Weaker client models] → The evaluated clients were frontier models. A weaker client may answer worse; the user's confirmation still gates every change.
- [Rotation coverage on a large vault] → At 3 pages a day, 20,000 pages take years to cover by rotation alone. Ordering by distance from the project centre puts distinct sub-subjects first, and creation and project changes open items at once. The daily slice is a setting.
- [Bulk refresh cost] → Only projects whose membership changed are refreshed, and the refresh never runs on the write path.

## Migration Plan

Additive. A new due-state category bumps the projection's schema version, so the projection rebuilds once. The family ships on; the existing `structural_suggestions` envelope turns its write-path delivery off.

## Open Questions

None.
