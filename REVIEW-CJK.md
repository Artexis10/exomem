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

## Recheck at f1dacb91 (4d89f7fa red, f1dacb91 green)

**Verdict: REQUEST_CHANGES.** MEDIUM 5 is fixed for the phrasings it names.
The fix splits a name that has hiragana inside it wherever that hiragana
starts with a particle's character, and the name's kanji or katakana head then
wins by `exact_alias`. This is MEDIUM 3 again, from the other side.

### Finding status

| finding | status | evidence |
|---|---|---|
| HIGH 1 alias guard | FIXED | probes below; guard unchanged since 89c0df5e |
| MEDIUM 2 `edit_memory` aliases | FIXED | restricted edit probe below |
| MEDIUM 3 hiragana-first name | FIXED | `ねこやなぎ銀行` stays one word, also after `駅前の` (`test_cjk_readiness.py:704`) |
| LOW 4 one walk per write | FIXED | `test_one_create_walks_the_entities_once_however_many_aliases` passes |
| MEDIUM 5 particle glued to more kana | FIXED | `working_set_resolve.py:655-659`; 4d89f7fa records 7 red before f1dacb91 |
| LOW `learned_aliases` unguarded | OPEN, tracked | task 6.12 now requires `claimed_names` |
| LOW refusal wording and stale PR body | not rechecked | cosmetic |

### Probes (throwaway tests over `ja_vault`, head f1dacb91)

- **A particle at the name's end.** `ハヤブサ号` + each of が, の, は, を, に,
  と, も, both bare and followed by `まだ話してない` or `もう一度見て`, resolves
  only the car. `青木陽介` + each particle + `まだ来ない` resolves only the
  person (21 + 7 cases pass).
- **Katakana names.** An entity `テッサリーワークス` resolves in
  `テッサリーワークスはもう閉まった？`, `…のもう一つの店` and
  `駅前のテッサリーワークスに行く`.
- **Mixed Latin and CJK.** `Harlow Wagonはもう車検に出した？`, `Exomemはどう？`,
  `Exomemのもう一つの問題` and `ExomemとHarlow Wagonの件` all resolve.
- **Withheld = absent.** With `月影プロジェクト` withheld at ceiling 0, a
  restricted caller's `edit_memory` alias `月影プロジェクト` commits, just as its
  absent twin `星影プロジェクト` does at create. The restricted activation
  `月影プロジェクトはもう終わった？` resolves only the caller's own page and
  never names the withheld one. The owner is refused with `ENTITY_EXISTS`,
  and the candidate named is the withheld page.

### NEW MEDIUM 6: a hiragana run inside a name is split at its first kana

`_leading_particle` (`working_set_resolve.py:681-684`) matches any run that
starts with a single-character particle: の, は, が, も, と, に, で, か, や, へ
or だ. Many hiragana words start with one of these. Once a kanji or katakana
stretch is open, the rule ends it at such a run, so a name written
kanji/katakana + hiragana + kanji loses its edge. Its head becomes a word.

Each row pairs an entity (the full name) with a note titled by the head:

| turn | 89c0df5e | f1dacb91 |
|---|---|---|
| `サクラもち本舗に行く` | full name resolved (`exact_alias`) | `サクラ` resolved (`exact_alias`), full name `partial` |
| `ミドリがめ商会の請求書` | full name resolved | `ミドリ` resolved, full name `partial` |
| `青空かえで銀行の口座` | full name resolved | `青空` resolved, full name `partial` |
| `木村はるかの予定を確認して` | full name `partial`, head absent | `木村` resolved, full name `partial` |

In every row the page the user named is `partial`, and a different page
resolves. At 89c0df5e, three of the four resolved the right page. Personal
names (surname + a hiragana given name) and shop names fall into this pattern
constantly. `embedded_words` also shows the greedy match at work: in
`ハヤブサ号のもう一台` the longest particle is `のも`, which leaves `う一台`.
That is harmless here, but it is the same mechanism.

**Direction (not verified).** A leading-particle split should not beat an
indexed name that continues through the run. Two options:
- emit the unsplit stretch too, and let an exact match of the longer name
  consume the head as containment already does;
- split only when the text after the particle does not continue a known
  name.

Add the four rows above as red twins beside
`test_a_name_followed_by_a_particle_glued_to_more_kana_resolves`.

### Tests and gates (head f1dacb91)

- `test_cjk_readiness`, `test_working_set_unicode_terms`,
  `test_working_set_resolve`, `test_working_set_learning`,
  `test_learning_fresh_session_journey`, `test_link`, `test_edit_operations`
  and `test_activation_lexical_term_budget`: **385 passed, 1 failed**. The
  failure is `test_the_bounded_query_corroborates_in_rank_order_and_stops_at_k`,
  and it fails the same way on untouched `origin/main`. This is the SQLite-plan
  environment failure from round one.
- `ruff --select F`: clean. `generate-capabilities.py --check`: current.
  Privacy gate: clean (4710 files). `openspec validate --all --strict`:
  217 passed.
- **CI:** run 36497983471 on the head: the required CI gate passed, and so did
  core shards 1-12, harness shards 1-4, lint, OpenSpec, Windows NTFS, E2E,
  package build, onboarding and the TUI. Conditional jobs were skipped.

## Recheck at 51299916 (37d47199 red, 51299916 green)

**Verdict: APPROVE, on one condition before merge:** sync the spec and design
(LOW 9). MEDIUM 6 is fixed, and every earlier control still holds. The two new
edge cases don't block.

### Finding status

- **HIGH 1, MEDIUM 2, MEDIUM 3, LOW 4 and MEDIUM 5 are still fixed.** The
  suite passes, including the five particle-glued turns and `ねこやなぎ銀行`,
  both alone and after `駅前の`.
- **MEDIUM 6 is FIXED.** The fix has two parts. First,
  `working_set_resolve.py:652-677` also emits the unsplit stretch and each
  cut before a particle. Second, `:1073-1089` then consumes the head.

Probes used an entity for each full name and a note for its head:

- The four rows from the previous recheck now resolve only the full name,
  and the head is `partial`.
- `木村はるか` resolves when it ends a sentence (`明日は木村はるか`,
  `予定は木村はるか。`), and before each of が, の, は, を, に, と and も.
- `松井ともこの電話番号` and `サクラもち本舗の新作` resolve.
- **Withheld = absent.** Take a restricted caller, with `木村はるか` withheld,
  and the turn `木村はるかの予定を確認して`. `木村` resolves with
  `exact_alias`, exactly as in the twin where `木村はるか` does not exist.
  `audience_view` filters rows before `candidates_for`, so a hidden longer
  name consumes nothing.

### NEW LOW 7: head consumption ignores position

`working_set_resolve.py:1083-1087` consumes a word that appears anywhere
inside a longer matched word. In `サクラとサクラもち本舗の違い`, the user
names both pages, yet `サクラ` is `partial`. **Fix:** consume only when
every occurrence lies inside the longer word's span, as `_contained_names`
does.

### NEW LOW 8: a name ending in hiragana absorbs the next kana word

The cut candidates are why. `サクラもちがうって言ってた` (サクラ + も + 違う)
and `サクラもちかくにある` resolve the entity `サクラもち`, and the note
`サクラ` is only `partial`. At f1dacb91 both resolved `サクラ`. Kana-only text
can be read either way (`木村はるかに遠い` has the same problem), and longest
match is defensible. Pin the chosen reading with a twin.

### NEW LOW 9: the contract text is stale

51299916 touches only `src/` and `tests/`. `spec.md:337-343` and
`design.md:191` still describe only the leading-particle split. They don't
cover the unsplit stretch, the cut candidates or head consumption. Under
CLAUDE.md, a contract change must update its spec. Add `サクラもち本舗` and
`木村はるか` to the scenario.

### Other files

- The fix commit edits a words-only pin (`test_cjk_readiness.py:728-742`,
  `ハヤブサ号のもう一台` → `う一台`). That is not a behavioural weakening.
- ASCII turns have no embedded words, so the new sets stay empty for
  English. The extra work is linear in rows, which are capped by
  `MAX_ANCHORS`.

### Tests and gates

- Scoped run: 326 passed and 1 failed, across `test_cjk_readiness`,
  `test_working_set_unicode_terms`, `test_working_set_resolve`,
  `test_working_set_learning`, `test_learning_fresh_session_journey` and
  `test_activation_lexical_term_budget`. The failure is the same SQLite-plan
  test that fails on `main`.
- `ruff --select F` is clean, and `generate-capabilities.py --check` reports
  current.
- CI: the required CI gate passed on run 36553480391.

## Final verify at 8fc4fb12 (fa9b0031 red, b65c6de0 fix, 8fc4fb12 spec)

**Verdict: APPROVE.**

- **LOW 7: FIXED.** `_inside_longer_words` (`working_set_resolve.py:549-565`)
  consumes a head only when every occurrence sits inside the longer name. In
  `サクラとサクラもち本舗の違い`, both pages resolve.
  `test_a_head_named_on_its_own_is_not_consumed_by_a_longer_name_in_the_turn`
  fails against fa9b0031's source and passes at the head.
- **LOW 8: FIXED (pinned).**
  `test_kana_only_text_after_a_name_reads_as_the_longest_indexed_name` pins the
  ruled reading with two twins: with the entity present, `サクラもち` wins; with
  it absent, `サクラ` resolves.
- **LOW 9: FIXED.** `spec.md:345-353`, the new scenario at `:522-529` and
  `design.md:191` now describe the unsplit stretch, the cuts, position-aware
  consumption and the kana-only ruling. The text matches the code.
- **No regressions.** The CJK, unicode-terms, resolve, learning and
  fresh-session suites all pass: 310 passed, 0 failed. They cover the MEDIUM 5
  controls, the MEDIUM 6 rows and `ねこやなぎ銀行`.
- **Gates:**
  - `ruff --select F` is clean.
  - `openspec validate --all --strict`: 217 passed.
  - CI: the required gate passed (run 36562754198).
