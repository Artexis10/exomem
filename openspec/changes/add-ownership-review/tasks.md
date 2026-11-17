## 1. Family and guidance

- [x] 1.1 Add the ownership test and the deferral rule to the semantic authoring guidance routes beside `destination_fitness`, and serve them in bootstrap guidance. Test: the route text equals the evaluated text and appears in the rendered contract.
- [x] 1.2 Add the `ownership_review` audit category and family module beside `note_cohesion`: eligibility, the item detail, and the signal version over project key, home folder and title. Test: a page created in a project opens an item; a dismissal holds while the body grows; a project change reopens it; hubs and Sources are never asked.

## 2. Due-state backlog and rotation

- [x] 2.1 Add the category to the due-state projection (schema version bump), with write-time settlement, served order after dated obligations and before `note_cohesion`, exact counts, and the never-asked count. Test: a due prediction sorts first; ownership sorts before cohesion; counts are exact.
- [x] 2.2 Build the bulk distance refresh off the write path: per-project centres from the paginated sweep read, refreshed only for projects whose membership changed. Test: the write path reads no other page's vectors (query count); a membership change refreshes only that project.
- [x] 2.3 Build the daily rotation with `rotation_daily` (default 3), farthest first within a project, projects in turn, authored-date order below three pages. Test: the farthest group is asked first; one large project cannot take the whole slice.

## 3. Delivery

- [x] 3.1 Add the `ownership` structure suggestion on writes to a page with an open item, with its terminal projector and bounds (question at most 400 tokens), behind the `structural_suggestions` envelope. Test: an open page's write carries it through the MCP door; an answered page writes quietly.
- [x] 3.2 Add the once-per-plan ownership question to curation previews with `create-note` or `move` steps, without changing the plan fingerprint. Rebase over `add-curation-relocation-protocol` first. Test: a split's children are each named; an edit-only plan carries none; the fingerprint is unchanged.
      Evidence, 2026-11-17: `ownership_review.plan_question` builds the question from the sealed plan's steps; `commands._with_ownership_question` adds it to the preview reply only, so the sealed plan and its fingerprint are untouched. Three tests in `tests/test_ownership_review.py`: a split's two children are each named and the fingerprint equals the proposal's; an edit-only plan carries none; a `move` that assigns a project is named. The split and move tests fail with the preview wiring removed.
- [x] 3.3 Add handling text in `commands.py` and the scaffold references for the family and its answers, keeping the scaffold generic. Test: `tests/test_scaffold_no_leak.py` passes.

## 4. Proof before release

- [x] 4.1 Gate 5: on a generated 20,000-page vault, measure write-path p95 before and after on the same instrument (budget: current p95 plus 25 ms) and the question's token size (at most 400). Record the commands and numbers.
      Evidence, 2026-11-17: generated 20,000-page vault in 40 projects; before and after measured on the same harness and page states, timing the added post-commit work directly (200 samples for the structure question, 40 for the due-state delta, two repeats). Worst after-minus-before p95: create question +3.1 ms, edit question +2.1 ms, due-state delta +6.8 ms, create plus delta +9.6 ms, all with every page asked (budget +25 ms). Question text 287 tokens (cl100k_base); the whole suggestion object 342 tokens.
- [ ] 4.2 Gate 6: a real-model cross-client run on a disposable vault with invented names and a seeded misfiled group: Claude headless over stdio and an OpenAI Responses actor over HTTP, both on the Exomem MCP. Record the surfacing rate per client and whether each answer used the spelled-out call or the curation route.

## 5. Closure

- [ ] 5.1 Run `openspec validate --all --strict` before and after each artifact change and at archive.
- [ ] 5.2 After this change ships, archive it with `openspec archive`.
