# Review: Japanese readiness (PR #1434, `feat/cjk-readiness` @ 5c9a313f)

**Verdict: REQUEST_CHANGES.** The tokeniser and English behaviour hold up. The
alias-collision guard, though, covers a narrower set of names than the resolver
does, so a capture-time alias can resolve the wrong page.

Probes: the PR's `ja_vault` fixture driving `op_connect_memory` and
`op_activate_context`.

## Findings

### HIGH 1: the alias guard checks entities only; the resolver matches every anchor

`src/exomem/link.py:751-762` checks each alias with
`entity_candidates.resolve_entity_candidate`. That function walks
`Entities/` only and compares by `identity_key` (NFKC + casefold + collapsed
spaces). Activation, wikilinks and the egress name map match every indexed
page's title and aliases by `working_set_index.normalize`, which also folds
typographic apostrophes and hyphens and drops soft hyphens.

- `create-entity name="Corvane Motors" aliases=["ハヤブサ号"]` is accepted,
  even though `Products/ハヤブサ号.md` is a note. After that,
  `ハヤブサ号の油圧センサーは…` resolves **both** pages, each with
  `exact_alias`.
- With `Dana's Garage` already an entity, the alias `Dana’s Garage`
  (U+2019) is accepted. `when does Dana's Garage open` then resolves both.
- With `テッサリー` already an alias, `テッ` + U+00AD + `サリー` is accepted.
  The index normalises it to the same key.

**Minimal fix:** before writing, also look each alias up with
`WorkingSetIndex(vault).resolve_names(aliases)`. Keep only the paths that
`egress.restricted_release_filter` passes, so a withheld page still reads as
absent. Refuse `ENTITY_EXISTS` on any hit, red-first.

### MEDIUM 2: `edit_memory` aliases skip the guard, but the guidance says they are refused

`src/exomem/_scaffold/_Schema/references/writing.md:37-38` says an alias
"another active entity already answers to is refused". The PR names
`edit_memory patch_frontmatter aliases` as the second route. That path does
no collision check.

- Repro: patch `aliases: ["青木陽介"]` onto `Tessary Works`.
  `青木陽介の仕事は何だっけ` then resolves Tessary Works and 青木陽介 together.
- The gap predates this PR, but the PR now steers agents to it.

**Fix:** run the check from finding 1 in `edit_memory` whenever
`field == "aliases"`. Otherwise, drop the refusal sentence from the guidance.

### MEDIUM 3: a name that starts with hiragana hands resolution to its kanji tail

`embedded_words` (`src/exomem/working_set_resolve.py:638-645`) treats every
hiragana run as a word boundary. That creates the same compound failure as the
ruled-out `ハヤブサ号線` case, just from the other side.

- Setup: a note `Products/銀行.md` and an entity `ねこやなぎ銀行`. The turn `ねこやなぎ銀行の口座を解約したい` resolves `銀行` by
  `exact_alias` and demotes the named entity to `partial`.
- On main this turn resolves neither page.

**Minimal fix:** treat a hiragana run as a boundary only when the whole run is
a declared particle or filler (`の`, `を`, `に`, `は`, `で`, `から`, …), or
when it sits at the token's edge. Otherwise, leave the neighbouring stretch
contained. Add the twin above as a red test.

### LOW 4: create-entity walks the Entities tree once per alias

`link.py:751-762` calls `resolve_entity_candidate` once per alias. With the
name check, that is up to 9 full walks and parses of `Entities/` per write.
**Fix:** one pass over a key set (finding 1's lookup does this).

## Hypotheses cleared

- **Width and case identity.** `ﾊﾔﾌﾞｻ号の車検` resolves `ハヤブサ号`; the
  alias `青木陽介` on another entity is refused. Neither side folds kana, so
  `はやぶさ号` beside `ハヤブサ号` is not a collision.
- **Withheld = absent.** An external caller's create-entity whose alias
  collides with a withheld entity succeeds without naming that entity. Only
  the owner's later write lists both. `test_a_withheld_japanese_page_never_leaks`
  passes.
- **YAML injection.** Hostile aliases (`a], status: archived, x: [b`) are
  written as quoted scalars.
- **Tokenisation.**
  - `ハヤブサ号線` and `新ハヤブサ号` are only `partial`.
  - `々` (`佐々木`, `人々`), `ー`/`ｰ`, small kana and full-width digits split
    correctly.
  - Chinese stays one contained token; a glued Latin name resolves.
  - Korean yields no words and does not crash.
- **English unchanged.**
  - I ran 57 activation, working-set, claims, structure-promotion, recall,
    lexical and benchmark modules on both trees: main gives 1959 passed, the
    branch 1960 passed (the one new test). Both have 23 skips and the same
    single environment failure (`test_activation_lexical_term_budget`,
    SQLite plan).
  - A differential over 36 English turns on a 2,000-page synthetic vault
    showed **0 packet differences**, including typographic-apostrophe,
    accented and em-dash turns.
- **Latency is bounded.** `working_set_index.MAX_ANCHORS = 2000`
  (`working_set_index.py:134`) caps the rows a turn scans, so a 5,000-page
  vault scans the same 2,000 anchors.
  - `analyze_turn` is vocabulary-independent (p95 ≤ 0.16 ms);
    `matched_claims` scales with claims, not titles.
  - At the cap, over two runs: Japanese turns added +20 ms at p50 and
    +40 ms at p95, all from 15 of 27 turns now resolving. English stayed
    within noise (±35 ms at p95, in both directions).
- **Guidance and pins.**
  - Scaffold no-leak, plugin sync, workflow skills, compact budget, schema
    fidelity, tool-surface and connector-guardrail suites: 120 passed. The
    CJK, link, unicode-terms and capture-scaffold suites: 121 passed.
  - The pending digest follows the two-phase rollout in
    `docs/remote-quickstart.md:303-313`, with `registered` and
    `refresh_required` untouched. `CONTRIBUTING.md` has no rollout rule to
    contradict it.
- **Edited tests.** Retargeted turns (`test_working_set_unicode_terms.py`,
  benchmark `M5-ja`) keep their compound twins; none is weakened.

## Recheck at 89c0df5e (51f06734 red, 89c0df5e green)

**Verdict: REQUEST_CHANGES.** HIGH 1, MEDIUM 2 and LOW 4 are fixed. The new
word-edge rule fixes MEDIUM 3 but loses a very common Japanese phrasing. There
is one fix, verified below. CI has not finished.

### Probes, re-run on the PR head

| probe | result |
|---|---|
| `create-entity` with alias `ハヤブサ号` (a note's title) | refused, `ENTITY_EXISTS` |
| alias `Dana’s Garage` (U+2019) | refused |
| alias `テッ` + U+00AD + `サリー` | refused |
| alias `Products/ハヤブサ号` (a path spelling) | refused |
| `edit_memory` aliases `[…, "青木陽介"]` (list or bare string) | refused |
| `edit_memory` rewriting the page's own title and aliases | accepted |
| `ねこやなぎ銀行の口座を解約したい` | the bank resolves; `銀行` is absent |

`replace_string` cannot reach frontmatter (`STRING_NOT_FOUND`), so
`patch_frontmatter` is the only route to `aliases`, and it is now guarded.

**Withheld twin** (`月影プロジェクト` withheld at ceiling 0; its absent twin
is `星影プロジェクト`):

| caller | route | withheld title | absent twin |
|---|---|---|---|
| restricted | create | accepted, `warnings=[]` | accepted, `warnings=[]` |
| restricted | edit | committed | committed |
| owner | create | refused, names the page | accepted |
| owner | edit | refused | accepted |

A restricted caller gets identical outcomes, and the refusal never names the
withheld page. `claimed_names` (`entity_candidates.py:128-203`) filters out
anything `restricted_release_filter` hides before it decides.

### MEDIUM 5 (new): a name followed by a particle and then more hiragana is no longer a word

The whole-run rule at `working_set_resolve.py:646-652` only splits a hiragana
run that is entirely a particle. Japanese often glues a particle to the next
kana word, so these turns now stop at `partial`:

- `ハヤブサ号はどう？`
- `ハヤブサ号はもう車検に出した？`
- `青木陽介はいつ来る？`
- `ハヤブサ号をまた洗車した`
- `ハヤブサ号がまだ故障中`

All five resolved at 5c9a313f. On main they are `partial`, so they are no
worse than main, but the PR's headline case (a name followed by a particle)
now depends on what comes after the particle.
`test_a_hiragana_run_that_is_not_wholly_a_particle_is_not_a_word_edge` pins
this loss as intended.

**Minimal fix (verified):**

- When a stretch is open, a hiragana run that starts with a declared particle
  ends that stretch, matching the longest particle.
- The rest of the run joins the next stretch.
- The left edge keeps the whole-run rule.

With this change, all five turns above resolve. `ねこやなぎ銀行` still
resolves and `銀行` stays absent, and `ハヤブサ号線` and `新ハヤブサ号` stay
`partial`. `駅前のねこやなぎ銀行` also works, because the rest of the run,
`ねこやなぎ`, joins `銀行`. Of the 91 tests in `test_cjk_readiness.py` and
`test_working_set_unicode_terms.py`, the only one that fails is the test that
pins this loss; it should be inverted.

### LOW (not blocking)

- **`learned_aliases` skips the guard.** Patching `learned_aliases: ["青木陽介"]`
  onto Tessary Works makes the 青木陽介 turn resolve both pages. The field
  predates this PR, and learning it is task 6.12. Give 6.12 the same
  `claimed_names` check.
- **The owner's edit refusal names a visible page as `([withheld])`.** In the
  same situation, the create refusal gives the path. This is cosmetic and
  leaks nothing.
- **The PR body is stale.** Item 2 still says the guard covers "another
  active entity". It now covers any page.

### Everything else

- **Tests and gates:** 738 passed across the CJK, unicode-terms, link,
  resolve, learning, edit-operations, validate-only, governance-egress,
  scaffold no-leak, plugin-sync, workflow-skills, compact-budget,
  schema-fidelity, tool-surface and connector-guardrail suites. The privacy
  gate, `ruff --select F` and the capabilities check are clean.
- **Scaffold:** `writing.md` now matches the guard ("any other page …, at
  capture and on edit"), and the plugin mirror and skill stamps were
  regenerated consistently.
- **English and latency:** the round changed only continua-run handling and
  the write-path guard. ASCII turns never reach either, so the round-one
  differential still holds. `claimed_names` does one index lookup plus one
  Entities walk per write, and `test_one_create_walks_the_entities_once_however_many_aliases`
  pins that.
- **CI:** run 36496302763 on 89c0df5e was still queued at recheck time: core
  shards 1-12, harness 1-4, lint, OpenSpec, Windows NTFS and E2E. The combined
  status is `pending`, and mergeable state is `blocked`. The last completed
  run, 36451406312 on 5c9a313f, was green.
