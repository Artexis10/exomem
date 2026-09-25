"""Task 4a — words in every script.

The activation index's tokeniser was ASCII-only (`[a-z0-9][a-z0-9'’\\-]*`):
it fragmented every accented word ("Ausrüstung" -> "ausr", "stung") and
dropped every non-Latin script entirely ("Привет мир" -> nothing), which in
turn let two unrelated titles "overlap" on nothing but a shared fragment
tail. These tests are RED on the pre-4a tokeniser and pin the fix: a term is
a maximal run of letters, digits and combining marks in any script (via an
explicit `unicodedata.category` scan), with the original ASCII regex kept,
unchanged, as a fast path so Basic Latin text tokenises exactly as before.

Found while implementing 4a: the same tokeniser also let a typographic right
single quote (`’`, U+2019) and the plain apostrophe (`'`) tokenise as
different words — NFKC does not fold one to the other.

Correction round: folding the quote inside `tokens_of` alone fixed only the
TURN's side of a comparison. An anchor's title or stored alias goes through
`normalize()` directly (never through `tokens_of`), so a title or alias
itself AUTHORED with a typographic apostrophe (phone autocorrect, smart
quotes) could never earn `exact_alias` — the turn's side folded, the
anchor's side did not. The fold now lives in `normalize()` itself, the one
normalisation both sides share, and `tokens_of` calls it rather than folding
separately. The tests below pin both directions.
"""

from __future__ import annotations

import random
import re
import string
import unicodedata

from exomem import working_set_index as wsi
from exomem import working_set_resolve as resolve_module


def _row(
    anchor_id: str, title: str, *, aliases: tuple[str, ...] = ()
) -> resolve_module.AnchorFacts:
    """One anchor row, its terms computed the way the real index computes
    them — `terms_of` over title and aliases — not hand-typed, so these
    tests exercise the same tokeniser the index itself calls. Aliases are
    `normalize`d before storage, exactly as `working_set_index` normalises
    frontmatter `aliases` when it builds the real index (line ~639), since
    `candidates_for`'s `exact_alias` check reads `row.aliases` as already
    normalised strings, never re-normalising them itself."""
    normalized_aliases = tuple(dict.fromkeys(wsi.normalize(alias) for alias in aliases))
    return resolve_module.AnchorFacts(
        anchor_id=anchor_id,
        path=anchor_id,
        ref=None,
        title=title,
        kind="hub",
        lifecycle="active",
        aliases=normalized_aliases,
        terms=wsi.terms_of(" ".join((title, *normalized_aliases))),
        categories=(),
        neighbourhood=frozenset(),
    )


# --------------------------------------------------------------------------- #
# `tokens_of` itself
# --------------------------------------------------------------------------- #


def test_accented_words_tokenise_whole_not_fragmented() -> None:
    """Spec scenario "An accented word is one term", plus the exact German
    and Spanish fragment lists the orchestrator measured on the old
    tokeniser — none of those fragments may appear."""
    german = wsi.tokens_of("Ausrüstung für die Reise")
    assert german == ("ausrüstung", "für", "die", "reise")
    assert german != ("ausr", "stung", "f", "r", "die", "reise")

    spanish = wsi.tokens_of("sesión de grabación")
    assert spanish == ("sesión", "de", "grabación")
    assert spanish != ("sesi", "n", "de", "grabaci", "n")


def test_non_latin_scripts_yield_terms() -> None:
    """Spec scenario "A turn in a non-Latin script can reach an anchor":
    Cyrillic and Greek must produce terms, not vanish the way the old
    ASCII-only tokeniser dropped them entirely ("Привет мир" -> [])."""
    assert wsi.tokens_of("Привет мир") != ()
    assert wsi.tokens_of("Привет мир") == ("привет", "мир")
    assert wsi.tokens_of("Καλημέρα κόσμε") == ("καλημέρα", "κόσμε")


def test_devanagari_vowel_sign_stays_with_its_base_letter() -> None:
    """A Devanagari vowel sign is a SPACING combining mark (Unicode category
    `Mc`) — exactly what `\\w` misses, since `\\w` excludes spacing
    combining marks. A whole word carrying one must still be one term."""
    assert wsi.tokens_of("किताब") == ("किताब",)


def test_cjk_unbroken_run_is_one_term() -> None:
    """Accepted limit, documented by the spec: a script written without word
    separators (Chinese, Japanese, Thai) yields one term per unbroken run;
    the product does not segment it."""
    assert wsi.tokens_of("你好世界") == ("你好世界",)


_REFERENCE_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9'’\-]*")


def _reference_tokens(text: str) -> tuple[str, ...]:
    """The tokeniser exactly as it was before task 4a, independently
    re-stated here (not imported from `working_set_index`) so this test is a
    real oracle, not a tautology against the production fast path."""
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    return tuple(match.group(0) for match in _REFERENCE_ASCII_TOKEN.finditer(normalized))


def test_ascii_text_tokenises_exactly_as_the_old_regex_did() -> None:
    """Spec scenario "Basic Latin text is unchanged", as a property test:
    for a few hundred generated ASCII strings — including apostrophes,
    hyphens, digits, underscores and punctuation — `tokens_of` must equal
    the old ASCII-only regex's `findall`, byte for byte.

    The property with teeth is the SECOND assertion: `_unicode_tokens`, the
    slow-path scanner that only ever runs on text containing a non-ASCII
    character, must tokenise a pure-ASCII string identically to the old
    regex too. `tokens_of` alone only ever exercises the slow path on
    non-ASCII input, so it cannot by itself rule out the slow path reading
    the ASCII portion of a MIXED-script string differently from how the
    fast path would — this pins that it does not.
    """
    # `'` (ASCII apostrophe) is in the pool; `’` (U+2019) is deliberately not —
    # that curly quote is itself outside `str.isascii()`, so a string carrying
    # one takes the slow path in production and is not this test's concern.
    pool = string.ascii_letters + string.digits + "'-_ .,!?@#$%^&*()[]{}:;\"/\\|~`+=<>\t"
    rng = random.Random(20260919)
    cases = 0
    for _ in range(300):
        candidate = "".join(rng.choice(pool) for _ in range(rng.randint(0, 60)))
        assert candidate.isascii()  # the pool is ASCII-only; stay honest about it
        cases += 1
        reference = _reference_tokens(candidate)
        assert wsi.tokens_of(candidate) == reference
        # `_unicode_tokens` is only ever called on ALREADY-normalised text in
        # production (`tokens_of` normalises, then dispatches); feeding it
        # the raw candidate here would just be checking that it does not
        # casefold, which is not the property under test.
        assert wsi._unicode_tokens(wsi.normalize(candidate)) == reference
    assert cases == 300


def test_a_curly_apostrophe_is_the_same_word_as_a_plain_one() -> None:
    """New spec scenario "A curly apostrophe is the same word": the
    typographic right single quote (U+2019) and the plain ASCII apostrophe
    must fold to the same term. NFKC does not merge the two codepoints, and
    the old `_TOKEN` regex accepted both in its continuation class without
    unifying them, so "i'd" and "i’d" tokenised as different words."""
    assert wsi.tokens_of("i’d rather") == wsi.tokens_of("i'd rather")


# --------------------------------------------------------------------------- #
# Through the real resolver
# --------------------------------------------------------------------------- #


def test_an_accented_anchor_earns_lexical_overlap_from_an_accented_turn() -> None:
    """Spec scenario "An accented word is one term", exercised end to end
    through `candidates_for`, not just `tokens_of` in isolation.

    The `lexical_overlap` assertion alone does not pin the fix: the OLD
    tokeniser fragments "Ausrüstung" into "ausr" and "stung" on BOTH the
    title and the turn consistently, so the fragments still overlap each
    other and `lexical_overlap` is granted either way. The two extra
    assertions below are the ones only the fix satisfies — the whole word
    as one token, and the fragment gone from the anchor's own terms."""
    rows = (_row("a.md", "Ausrüstung Lager"),)
    analysis = resolve_module.analyze_turn("wo ist die Ausrüstung im Lager")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "lexical_overlap" in candidates[0].evidence
    assert "ausrüstung" in analysis.tokens
    assert "ausr" not in wsi.terms_of("Ausrüstung Lager")


def test_word_fragments_never_make_two_unrelated_pages_overlap() -> None:
    """Spec scenario "Word fragments never make two unrelated pages
    overlap". RED today: the old tokeniser fragmented both nouns down to a
    bare "n" and left "de" whole, so this wholly unrelated anchor earned
    `lexical_overlap` from nothing but a shared word ending plus "de"."""
    rows = (_row("b.md", "Declaración de Impuestos"),)
    analysis = resolve_module.analyze_turn("la duración de la sesión")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert candidates == ()


def test_a_cyrillic_alias_is_reached_by_a_cyrillic_turn() -> None:
    """Spec scenario "A turn in a non-Latin script can reach an anchor": a
    two-word Cyrillic alias contained in a Cyrillic turn earns
    `exact_alias`, the same as a basic-Latin alias always has."""
    rows = (_row("c.md", "Склад ресурсов", aliases=("привет мир",)),)
    analysis = resolve_module.analyze_turn("привет мир, есть вопрос")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "exact_alias" in candidates[0].evidence


def test_a_curly_apostrophe_turn_reaches_a_plain_apostrophe_alias() -> None:
    """New spec scenario "A curly apostrophe is the same word", exercised
    end to end: an anchor alias written with a plain apostrophe, reached by
    a turn writing the same words with a typographic apostrophe, earns
    `exact_alias` exactly as it would if the turn had used the plain
    apostrophe too."""
    rows = (_row("f.md", "Plan", aliases=("i'd rather",)),)
    analysis = resolve_module.analyze_turn("well, I’d rather not")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "exact_alias" in candidates[0].evidence


def test_basic_latin_text_is_unchanged_through_the_resolver() -> None:
    """Spec scenario "Basic Latin text is unchanged": an unaccented turn and
    anchor must resolve exactly as before non-Latin and accented terms were
    recognised."""
    rows = (_row("d.md", "Northern Freight Corridor"),)
    analysis = resolve_module.analyze_turn("the northern freight corridor is jammed")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "lexical_overlap" in candidates[0].evidence
    assert wsi.tokens_of("the northern freight corridor is jammed") == (
        "the",
        "northern",
        "freight",
        "corridor",
        "is",
        "jammed",
    )


# --------------------------------------------------------------------------- #
# Sites that agree: derived short names, `fold_plural`
# --------------------------------------------------------------------------- #


def test_a_derived_short_name_from_an_accented_title_is_the_accented_lead() -> None:
    """`derived_short_name`'s "at least two letters per token" gate already
    used `str.isalpha()`, which is Unicode-aware — but it read those letters
    through the old ASCII-only `tokens_of`, which fragmented an accented
    lead into single letters before this gate ever saw it. With the fix, an
    accented lead is a name just as much as an ASCII one, while it stays
    unique in the catalogue (uniqueness itself is `_finalize_anchor_aliases`'s
    job, not this pure function's)."""
    assert wsi.derived_short_name("Ausrüstung — Inventar") == "ausrüstung"


def test_fold_plural_never_raises_and_folds_by_ascii_suffix_regardless_of_script() -> None:
    """`fold_plural` has no non-Latin plural rule, but it is NOT a no-op on
    non-ASCII input in general: its suffix checks look only at a word's
    TRAILING characters, so a non-ASCII word that happens to end in an
    ASCII plural tail folds exactly as an ASCII one would ("cafés" ->
    "café"; the Spanish "país"/"países" pair meet at the same folded form,
    even though neither is the linguistically correct singular). A word
    with no such tail (Cyrillic, Devanagari, CJK, Greek — none of these end
    in an ASCII "s") is untouched, and no input may ever raise."""
    assert wsi.fold_plural("cafés") == "café"
    assert wsi.fold_plural("país") == wsi.fold_plural("países")
    for word in ("ausrüstung", "лагерь", "किताबें", "你好世界", "καλημέρα"):
        folded = wsi.fold_plural(word)
        assert folded == word


# --------------------------------------------------------------------------- #
# Correction round: the fold has to be in `normalize()`, not `tokens_of`
# alone, or an anchor's OWN authored apostrophe never earns `exact_alias`.
# --------------------------------------------------------------------------- #


def test_an_authored_typographic_alias_is_reached_by_either_apostrophe_style() -> None:
    """RED before the correction: `candidates_for`'s `names` set is built
    from `normalize(row.title)` and the anchor's already-`normalize`d stored
    aliases, never from `tokens_of`. When only `tokens_of` folded the quote,
    an alias itself AUTHORED with a typographic apostrophe stayed curly in
    `names` while `phrases` (built from the turn via `tokens_of`) was always
    plain — so this alias could never earn `exact_alias`, from a turn typed
    either way."""
    rows = (_row("i.md", "Plan", aliases=("i’d rather",)),)

    plain_turn = resolve_module.analyze_turn("well, I'd rather not")
    typographic_turn = resolve_module.analyze_turn("well, I’d rather not")

    plain_candidates = resolve_module.candidates_for(plain_turn, rows)
    typographic_candidates = resolve_module.candidates_for(typographic_turn, rows)

    assert len(plain_candidates) == 1
    assert "exact_alias" in plain_candidates[0].evidence
    assert len(typographic_candidates) == 1
    assert "exact_alias" in typographic_candidates[0].evidence


def test_an_authored_typographic_title_is_reached_by_either_apostrophe_style() -> None:
    """Same defect, title side: `normalize(row.title)` alone built `names`
    for a title with no alias at all, so the same asymmetry applied to a
    title itself authored with a typographic apostrophe."""
    rows = (_row("j.md", "I’d Rather Not"),)

    plain_turn = resolve_module.analyze_turn("I'd rather not go")
    typographic_turn = resolve_module.analyze_turn("I’d rather not go")

    plain_candidates = resolve_module.candidates_for(plain_turn, rows)
    typographic_candidates = resolve_module.candidates_for(typographic_turn, rows)

    assert len(plain_candidates) == 1
    assert "exact_alias" in plain_candidates[0].evidence
    assert len(typographic_candidates) == 1
    assert "exact_alias" in typographic_candidates[0].evidence


def test_derived_short_name_is_the_same_whichever_apostrophe_the_title_used() -> None:
    """A derived short name must not depend on which apostrophe style the
    title's own qualifier was typed with — both must derive the identical
    plain-apostrophe name."""
    typographic = wsi.derived_short_name("Don’t Panic — field guide")
    plain = wsi.derived_short_name("Don't Panic — field guide")

    assert typographic == plain == "don't panic"


def test_analyze_turn_text_folds_the_typographic_apostrophe_too() -> None:
    """RED before the correction: `analyze_turn`'s `.text` field re-stated
    `normalize()`'s formula inline rather than calling it, so it never
    folded the quote either. `.text` is what `CUE_PATTERNS` substring
    matching reads directly (never through `tokens_of`), so a turn typed
    with a typographic apostrophe matched none of a cue's plain-apostrophe
    substrings, such as "i'm planning"."""
    analysis = resolve_module.analyze_turn("I’m planning a trip")
    assert "i'm planning" in analysis.text


# --------------------------------------------------------------------------- #
# Correction round 2: hyphens, and the derived-name length ceiling
# --------------------------------------------------------------------------- #


def test_a_non_breaking_hyphen_tokenises_like_a_plain_one() -> None:
    """Same argument as the apostrophe: `normalize()` folds a typographic
    HYPHEN (U+2010) to the plain ASCII hyphen. NFKC already maps NON-
    BREAKING HYPHEN (U+2011) to U+2010, so folding U+2010 alone covers a
    word typed with either."""
    assert wsi.tokens_of("well‑known plan") == wsi.tokens_of("well-known plan")
    assert wsi.tokens_of("well‑known plan") == ("well-known", "plan")


def test_a_soft_hyphen_is_dropped_not_tokenised_as_a_break() -> None:
    """A SOFT HYPHEN (U+00AD) is a hint for where a renderer MAY break a
    line, not a character a person typed on purpose; `normalize()` drops it
    entirely rather than let it split a word the way a real hyphen does."""
    assert wsi.tokens_of("co­operate") == ("cooperate",)


def test_en_dash_and_em_dash_are_not_folded() -> None:
    """Titles use an en dash or an em dash as the qualifier separator
    `derived_short_name`'s `_TRAILING_DASH` matches on; folding either into
    a hyphen would make a qualifier separator indistinguishable from a
    hyphenated word inside the lead itself."""
    assert wsi.normalize("a–b") == "a–b"
    assert wsi.normalize("a—b") == "a—b"
    assert wsi.derived_short_name("Bike – Trek 520") == "bike"
    assert wsi.derived_short_name("Bike — Trek 520") == "bike"


def test_a_non_breaking_hyphen_reaches_an_anchor_through_the_real_resolver() -> None:
    """Spec scenario "A non-breaking hyphen is the same word", exercised end
    to end: an anchor titled "Well-Known Plan" (plain hyphen) and a turn
    writing "well‑known plan" with a non-breaking hyphen must share terms
    and earn `lexical_overlap`."""
    rows = (_row("k.md", "Well-Known Plan"),)
    analysis = resolve_module.analyze_turn("is the well‑known plan still active")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "lexical_overlap" in candidates[0].evidence
    assert wsi.terms_of("well‑known plan") == wsi.terms_of("well-known plan")


def test_a_derived_name_containing_a_word_longer_than_48_code_points_is_not_admitted() -> None:
    """Spec scenario "A sentence is not a name": a script without word
    separators caps a "word count" of one at three TOKENS (the existing
    `len(tokens) > 3` check), never at any length, so an unbroken CJK run
    the length of a whole sentence would otherwise read as a valid
    "three-or-fewer-word" name. A 60-code-point unbroken lead before a
    parenthetical must be refused; a 16-code-point one is still a name and
    stays admitted. (Correction round 3: the ceiling is per WORD, not on
    the joined name — see
    `test_three_long_compound_words_are_still_a_name` for why that
    distinction matters.)"""
    too_long = "你" * 60
    assert wsi.derived_short_name(f"{too_long} (note)") is None

    fine = "你" * 16
    assert wsi.derived_short_name(f"{fine} (note)") == fine


def test_a_single_word_at_exactly_48_code_points_is_the_ceiling() -> None:
    """The ceiling is inclusive: a single word of exactly 48 code points is
    still admitted, 49 is not."""
    at_ceiling = "你" * 48
    over_ceiling = "你" * 49
    assert wsi.derived_short_name(f"{at_ceiling} (note)") == at_ceiling
    assert wsi.derived_short_name(f"{over_ceiling} (note)") is None


def test_three_long_compound_words_are_still_a_name() -> None:
    """Spec scenario "Long compound words are still a name": RED before
    correction round 3, when the ceiling capped the JOINED name at 48 code
    points rather than each WORD. "Ausrüstungsverwaltungssystem
    Lagerverwaltung Übersicht" is an ordinary three-word German lead — 28,
    15 and 9 code points, none over the per-word ceiling — but joins to 54,
    over the old joined-name cap, which refused it for the same reason a
    60-code-point CJK sentence is refused: exactly the false positive this
    round's fix exists to remove."""
    title = "Ausrüstungsverwaltungssystem Lagerverwaltung Übersicht (2026)"

    name = wsi.derived_short_name(title)

    assert name == "ausrüstungsverwaltungssystem lagerverwaltung übersicht"
    assert len(name) == 54
    tokens = wsi.tokens_of(name)
    assert [len(token) for token in tokens] == [28, 15, 9]


def test_a_two_character_cjk_name_still_earns_rare_term() -> None:
    """The `rare_term` length floor counts CODE POINTS, and two of them is an
    ordinary-length word in CJK — a city, a company, a person.

    The floor exists to stop an everyday two-letter English word ("go",
    "it") being read as a lead. That reasoning is about an alphabet where a
    word is several letters long; applying the same count to a script where
    it is not turns a real name into a non-name. It is applied only where it
    means something: a term written entirely in ASCII letters.
    """
    rows = (_row("cjk.md", "東京 プロジェクト"),)
    analysis = resolve_module.analyze_turn("東京 の状況")
    candidates = resolve_module.candidates_for(
        analysis, rows, term_anchor_counts={"東京": 1}
    )

    assert len(candidates) == 1, candidates
    assert "rare_term" in candidates[0].evidence


def test_a_two_letter_ascii_term_is_still_refused() -> None:
    """The half the floor exists for, unchanged."""
    rows = (_row("go.md", "Release Go Checklist"),)
    analysis = resolve_module.analyze_turn("so should i go with the first option")
    candidates = resolve_module.candidates_for(
        analysis, rows, term_anchor_counts={"go": 1}
    )

    assert candidates == ()


# --------------------------------------------------------------------------- #
# CJK containment contact (step 4, T5; design §6.3)
# --------------------------------------------------------------------------- #
#
# Japanese, Chinese and Thai write words without spaces, so a whole run is one
# turn token and an anchor's name inside it is never a token of its own. An
# anchor whose name is a contiguous substring of such a run earns `rare_term`,
# the weak worded kind, which resolves only with a second, independent contact.

_HUT = "白樺"
_TURN = "来月の合宿、山小屋の白樺をまた借りられるか確認してくれる？"
_COMPOUND_TURN = "駅前の白樺並木、今年は紅葉がきれいだったね。"


def _evidence(turn: str, rows, *, counts=None, bands=None) -> dict[str, frozenset[str]]:
    rows = tuple(rows)
    counts = counts if counts is not None else _counts(rows)
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn(turn), rows, term_anchor_counts=counts, bands=bands
    )
    return {candidate.anchor_id: candidate.evidence for candidate in candidates}


def _counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for term in set(row.terms):
            counts[term] = counts.get(term, 0) + 1
    return counts


def test_a_cjk_name_inside_a_run_earns_rare_term_never_exact_alias() -> None:
    evidence = _evidence(_TURN, [_row("hut.md", _HUT)])

    assert evidence == {"hut.md": frozenset({"rare_term"})}


def test_containment_resolves_only_with_a_second_contact() -> None:
    rows = (_row("hut.md", _HUT),)
    alone = resolve_module.candidates_for(
        resolve_module.analyze_turn(_TURN), rows, term_anchor_counts=_counts(rows)
    )
    banded = resolve_module.candidates_for(
        resolve_module.analyze_turn(_TURN), rows, term_anchor_counts=_counts(rows), bands={"hut.md": True}
    )
    tokens = resolve_module.analyze_turn(_TURN).tokens

    assert resolve_module.resolve(alone, turn_tokens=tokens).anchors[0].status == "partial"
    assert resolve_module.resolve(banded, turn_tokens=tokens).anchors[0].status == "resolved"


def test_the_name_inside_an_unrelated_compound_stays_partial() -> None:
    """Twin 5: 白樺並木 (a birch avenue) contains the hut's name. Containment is
    real contact, but without the band it cannot resolve."""
    rows = (_row("hut.md", _HUT),)
    candidates = resolve_module.candidates_for(
        resolve_module.analyze_turn(_COMPOUND_TURN), rows, term_anchor_counts=_counts(rows), bands={"hut.md": False}
    )

    assert {c.anchor_id: c.evidence for c in candidates} == {"hut.md": frozenset({"rare_term"})}
    tokens = resolve_module.analyze_turn(_COMPOUND_TURN).tokens
    assert resolve_module.resolve(candidates, turn_tokens=tokens).anchors[0].status == "partial"


def test_a_name_inside_a_longer_contained_name_is_consumed() -> None:
    """The turn names the avenue (白樺並木); the hut's shorter name inside it is
    part of spelling the avenue, not a lead to the hut."""
    rows = (_row("hut.md", _HUT), _row("avenue.md", "白樺並木"))

    evidence = _evidence(_COMPOUND_TURN, rows)

    assert evidence == {"avenue.md": frozenset({"rare_term"})}


def test_a_name_used_by_more_than_three_anchors_is_not_rare() -> None:
    rows = [_row(f"hut-{i}.md", _HUT) for i in range(4)]

    assert _evidence(_TURN, rows) == {}


def test_a_one_code_point_name_never_qualifies() -> None:
    assert _evidence(_TURN, [_row("mountain.md", "山")]) == {}


def test_a_name_must_share_the_run_s_script() -> None:
    """Hangul and Latin names are words of their own and are never contained;
    a Thai name inside a Thai run is."""
    assert _evidence(_TURN, [_row("latin.md", "Cedar")]) == {}
    thai = "ริมทะเล"
    assert _evidence("จองโรงแรมริมทะเลให้หน่อย", [_row("thai.md", thai)]) == {"thai.md": frozenset({"rare_term"})}
    assert _evidence("백화점에 가자", [_row("korean.md", "백화")]) == {}


def test_a_cjk_name_inside_a_token_that_mixes_scripts_is_contained() -> None:
    """A Latin word glued to a Japanese phrase is one turn token. Its Japanese
    run is read like any other: 予算 sits inside `quillmereの予算を確認`, and a
    run that is exactly the name still counts, since the token is not the name.
    The Latin part stays a fragment, never a name."""
    budget = [_row("budget.md", "予算")]

    assert _evidence("quillmereの予算を確認して", budget) == {"budget.md": frozenset({"rare_term"})}
    assert _evidence("quillmere予算", budget) == {"budget.md": frozenset({"rare_term"})}
    assert _evidence("quillmereの予算を確認して", [_row("latin.md", "Quill")]) == {}


def test_latin_and_cyrillic_tokens_are_unaffected() -> None:
    """A Latin or Cyrillic name inside a longer word is a fragment, not a name."""
    assert _evidence("the cedarwood trailer", [_row("cedar.md", "Cedar")]) == {}
    assert _evidence("медведица", [_row("bear.md", "Медведь")]) == {}
    assert _evidence("медведица", [_row("bear.md", "медв")]) == {}
