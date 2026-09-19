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
    the old ASCII-only regex's `findall`, byte for byte."""
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
        assert wsi.tokens_of(candidate) == _reference_tokens(candidate)
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
    through `candidates_for`, not just `tokens_of` in isolation."""
    rows = (_row("a.md", "Ausrüstung Lager"),)
    analysis = resolve_module.analyze_turn("wo ist die Ausrüstung im Lager")

    candidates = resolve_module.candidates_for(analysis, rows)

    assert len(candidates) == 1
    assert "lexical_overlap" in candidates[0].evidence


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


def test_fold_plural_on_non_ascii_input_never_raises_and_is_a_no_op() -> None:
    """`fold_plural` has no non-Latin plural rule: it must leave a non-ASCII
    word untouched rather than raise or mangle it, since none of its suffix
    checks (`-s`, `-es`, `-ies`) are meant to fire outside ASCII spellings
    it does not end in."""
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
