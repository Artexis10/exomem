> **Sections 1–5 and 7.1 are a record, not a plan.** They were written after the fact, from an
> approved implementation plan rather than from this change. The TDD ordering rule in
> `openspec/config.yaml` was followed during implementation — `temporal`'s pure-logic tests and
> the filter regression test were both red before their code existed — but the OpenSpec artifacts
> themselves trail the code, which is the wrong order for this repo. Section 6 was specified
> before implementation and is the intended order.

## 1. Baseline And Red Tests

- [x] 1.1 Full-suite baseline from the clean worktree before any edit (1 failed, 6792 passed; the
  single failure is `test_governance_overhead` overhead budget, flaking under concurrent load).
- [x] 1.2 Pure-logic unit tests for `temporal` first, per the `openspec/config.yaml` TDD rule:
  `compare` against every row of the handoff's table including both indeterminate cases, `parse`
  over each spelling the vault can produce, `stamp`/`render_date` round-trips.
- [x] 1.3 Failing regression test proving a page is dropped from date filters when its `updated`
  is a quoted date, a bare timestamp, or a quoted timestamp. Verified red on `main`: only the
  bare unquoted date matched.

## 2. Shared Temporal Module

- [x] 2.1 Add `src/exomem/temporal.py`: `Moment`, `Order`, `now`, `stamp`, `render_date`,
  `parse`, `compare`, `sort_key`.
- [x] 2.2 Second-granularity UTC `Z` output, matching the existing `adoption_run._now_iso` and
  `review_state` precedents.
- [x] 2.3 `render_date` reads the day as given so note paths keep the local month.

## 3. Read And Comparison Paths

- [x] 3.1 `structured_filters.page_view` settles precision so all four YAML spellings answer
  day-granular filters; unparseable values pass through and still fail them.
- [x] 3.2 `find_types.ParsedPage.updated` renders through `temporal` instead of `str()`, ending
  the space-separated form and matching `semantic_unit_read`.
- [x] 3.3 `audit._parse_fm_date` and `find_policy.parse_date` delegate to `temporal`; day-collapse
  behaviour deliberately unchanged.
- [x] 3.4 `audit_fix._as_iso_date` preserves recorded precision on copy-forward and no longer
  emits `str(datetime)`; the experiment `duration` computation survives a timestamped `started`.
- [x] 3.5 Widen `vault._LOG_ENTRY_HEADER_RE` to accept both precisions — before anything can
  write the longer form.

## 4. Write Paths

- [x] 4.1 `note` (legacy and modern), `edit`, `replace` (legacy and modern).
- [x] 4.2 `link`, `create_file`, `multi_edit`, `set_frontmatter_field`, `observe_memory`.
- [x] 4.3 `add` and `preserve` for `captured`, the source-page analogue of `created`.
- [x] 4.4 `log.md` headings via `prepend_log_entry` and its inline duplicates; `indexes.compute_updates`
  gains a distinct `stamp_iso` so index bullets stay day-granular while the log heading does not.
- [x] 4.5 `commit_edit` derives the day for its idempotency key rather than using the stamp.

## 5. Documentation

- [x] 5.1 `_scaffold/_Schema/references/frontmatter.md` documents both forms, why date-only is
  never rewritten, and that same-day ordering may be undecidable.
- [x] 5.2 `edit.py` module docstring: `updated` is bumped to the write instant, not "to today".

## 6. Sub-Day Retrieval

- [x] 6.1 `updated_after` / `updated_before` accept an instant. `page.updated` already admitted
  RFC 3339 date-times as operands, so no new queryable field was needed: `page_view` presents the
  most precise recorded value and `_evaluate_operator` compares at the granularity of the *bound*.
- [x] 6.2 A date-only page on the boundary day is returned and reported by `indeterminate_bounds`
  rather than silently dropped or silently included.
- [x] 6.3 `Hit.order_indeterminate` is emitted from `as_dict` only when non-empty and added to
  the `governance/egress.py` `_HIT_FIELDS` allowlist. The MCP tool-schema fixture needed no
  change: the flag lives on the hit payload, not the tool signature.
- [x] 6.4 `find` marks hits after every filtering lane. The bounds are captured before
  `find.py:630` clears the legacy arguments — once a typed plan exists the shortcuts are folded
  into it and `_filter_by_date` becomes a no-op, so marking from the legacy path alone was
  silently dead.

## 6b. Determinism (found by the full suite)

- [x] 6b.1 `DraftToken` freezes `render_stamp` alongside `render_date`; `_TOKEN_VERSION` = 2.
- [x] 6b.2 `render_stamp` declared after `registrations` and passed by keyword — inserting it
  beside `render_date` silently rebound the fifth positional argument at four call sites.
- [x] 6b.3 `note` and `create_file` take the stamp from the token when one is supplied.
- [x] 6b.4 `test_relation_queue_commands` byte-identity test pins the clock via `temporal.now`.

## 7. Validation

- [x] 7.1 `uvx ruff check . --select F` clean.
- [ ] 7.2 Full suite green, compared against the 1.1 baseline.
- [ ] 7.3 Latency gate and semantic-write latency check, since `find_policy.parse_date` sits on a
  hot retrieval path.
- [x] 7.4 `openspec validate record-note-knowledge-time-to-the-second --strict`.
