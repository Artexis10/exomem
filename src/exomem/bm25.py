"""BM25Okapi over compiled KB pages, with incremental per-process caches.

The assembled corpus is retained with a freshness checkpoint. A complete,
small change delta repairs only the changed/deleted paths; cold starts, policy
changes, incomplete histories, and large deltas fall back to a full walk. The
per-doc token cache remains keyed by path + mtime, mirroring
`find.FrontmatterCache`, so unchanged documents are not re-tokenized on either
path. `BM25Okapi` itself is still reconstructed from the retained token lists
whenever the corpus changes (`rank_bm25` has no incremental add/remove API),
but that in-memory global-stat step is cheap relative to walking, admitting,
and reading the vault.

Tokenizer v2 (`tokenize`, `token_units`) reads every script and is
byte-identical to v1 on ASCII text:

- ASCII text keeps the v1 fast path: lowercase, `[a-z0-9]+`, English Snowball,
  so "regulation" matches a page with "regulator" exactly as before.
- Other text is NFKC-normalised and casefolded, then split into maximal runs
  of letters, digits and combining marks. A run splits again wherever it
  crosses between an unspaced script (Han, kana, Hangul, Thai, Lao, Khmer,
  Myanmar; declared in `text_scripts`) and a spaced one.
- An unspaced run emits overlapping two-character bigrams (a character is a
  base plus its combining marks); a one-character run emits that character.
- A spaced word is stemmed by its script, never by a guessed language: ASCII
  by English Snowball, all-Cyrillic by Russian, all-Greek by Greek,
  all-Armenian by Armenian; anything else is left as written.
- On the INDEX side only, a Latin word whose accents fold away also emits the
  folded form, so "zolvarn" finds "Zölvarn" while "Zölvarn" still matches its
  exact surface. The query side emits surface forms only, so no query term is
  counted twice.

The same stemmer is exposed to find.py for its stem-aware gates.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from . import find as find_module
from . import freshness, recall_policy, text_scripts
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

#: Version of the token contract. Persisted token stores (the lexical
#: catalogue) carry it inside their own schema version.
TOKENIZER_VERSION = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STEMMER_LOCAL = threading.local()

#: The kanji that turns a counter into a question word (何度, 何時, 何月).
_QUESTION_KANJI = "\u4f55"

#: Snowball stemmer for a word whose letters are all in one of these scripts.
_SCRIPT_STEMMERS = {"cyrillic": "russian", "greek": "greek", "armenian": "armenian"}

#: Unicode planes searched when the character tables are built. Every
#: combining mark, symbol and variation selector in Unicode sits in the Basic
#: or Supplementary Multilingual Plane or in plane 14.
_MARK_PLANES = ((0x0000, 0x1FFFF), (0xE0000, 0xEFFFF))

#: Variation selectors choose a glyph (text or emoji presentation, an
#: ideographic variant); they carry no letter, so they are dropped before
#: tokenizing rather than kept as marks that would glue a keycap to its digit.
_VARIATION_SELECTORS = ((0x180B, 0x180D), (0x180F, 0x180F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))

# Above this fraction of the retained corpus, bounded per-path repair gives way
# to the existing full walk. The measurement supporting the value lives in the
# worker result for the change that introduced incremental corpus repair.
MAX_INCREMENTAL_REPAIR_FRACTION = 0.10


class _CorpusCacheEntry(NamedTuple):
    """One assembled corpus plus the provenance needed for safe delta repair.

    Keep the first three fields in the historical positional order: residency
    diagnostics intentionally read ``entry[2]`` without allocating projections.
    """

    cache_identity: tuple
    bm25: Any
    paths: list[str]
    tokens_by_path: dict[str, list[str]]
    checkpoint: freshness.RecallFreshnessCheckpoint
    policy_identity: tuple[str, str]


def _get_stemmer(language: str = "english"):
    """This thread's Snowball stemmer for `language` (they are not thread-safe)."""
    stemmers = getattr(_STEMMER_LOCAL, "stemmers", None)
    if stemmers is None:
        stemmers = _STEMMER_LOCAL.stemmers = {}
    stemmer = stemmers.get(language)
    if stemmer is None:
        import snowballstemmer

        stemmer = stemmers[language] = snowballstemmer.stemmer(language)
    return stemmer


@lru_cache(maxsize=16384)
def stem_word(word: str) -> str:
    """Memoized single-word stem, chosen by the word's script.

    ASCII words get English Snowball, exactly as tokenizer v1 did. A word whose
    letters are all Cyrillic, all Greek or all Armenian gets that script's
    stemmer. Anything else is returned unchanged: stemming by a guessed
    language could give a query and a page two different stems of one word.
    """
    if word.isascii():
        return _get_stemmer().stemWord(word)
    language = _SCRIPT_STEMMERS.get(text_scripts.uniform_letter_script(word) or "")
    if language is None:
        return word
    return _get_stemmer(language).stemWord(word)


class TokenUnit(NamedTuple):
    """The stems one spaced word or one unspaced run contributed.

    Corroboration and pairing rules count units, not stems: an accented word's
    folded variant and a run's many bigrams are one piece of evidence.
    """

    stems: tuple[str, ...]
    run: bool


def _in_mark_planes(code_point: int) -> bool:
    return any(start <= code_point <= end for start, end in _MARK_PLANES)


@lru_cache(maxsize=1)
def _character_tables() -> tuple[str, dict[int, str | None]]:
    """(regex class body of every combining mark, raw-text translation table).

    The table runs before NFKC on non-ASCII text. It maps every non-ASCII
    symbol (S*) and enclosing mark (Me) to a space, so NFKC can never turn one
    into letters that join the word beside it ("Zorblex™" would otherwise
    become `zorblextm`, "20℃" `20c`), and it deletes variation selectors.
    Built once, from the running interpreter's Unicode data.
    """
    ranges: list[list[int]] = []
    table: dict[int, str | None] = {}
    for start, end in _MARK_PLANES:
        for code_point in range(start, end + 1):
            category = unicodedata.category(chr(code_point))
            if category[0] == "M":
                if ranges and ranges[-1][1] == code_point - 1:
                    ranges[-1][1] = code_point
                else:
                    ranges.append([code_point, code_point])
                if category == "Me":
                    table[code_point] = " "
            elif category[0] == "S" and code_point > 0x7F:
                table[code_point] = " "
    for low, high in _VARIATION_SELECTORS:
        for code_point in range(low, high + 1):
            table[code_point] = None
    marks = "".join(f"\\U{low:08x}-\\U{high:08x}" for low, high in ranges)
    return marks, table


def _mark_class() -> str:
    """Regex class body of every combining mark (Unicode M*)."""
    return _character_tables()[0]


@lru_cache(maxsize=1)
def _scanner() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """(token runs, script parts of a run, characters of an unspaced part).

    A token starts with a letter or number (`[^\\W_]` is exactly Unicode L*
    and N*) and continues with letters, numbers and combining marks. A mark
    never starts a token: without a base it is a stray diacritic ("it´s",
    "‾" after NFKC), exactly as `working_set_index` rules. Underscore and
    every punctuation mark separate, as `[a-z0-9]+` always did.
    """
    marks = _mark_class()
    continua = text_scripts.continua_character_class()
    runs = re.compile(f"[^\\W_](?:[^\\W_]|[{marks}])*")
    parts = re.compile(
        f"(?P<run>(?![{marks}])[{continua}](?:[{continua}]|[{marks}])*)"
        f"|(?P<word>(?:[^{continua}]|[{marks}])+)"
    )
    characters = re.compile(f"[^{marks}][{marks}]*|[{marks}]+")
    return runs, parts, characters


def _is_token_character(character: str) -> bool:
    """Can `character` appear inside a token (after a letter)?"""
    return _scanner()[0].fullmatch("a" + character) is not None


def _latin_fold(word: str) -> str | None:
    """`word` with its combining marks removed, when that changes a Latin word.

    Restricted to Latin: dropping marks destroys an Indic word and conflates
    Cyrillic й with и.
    """
    if word.isascii() or text_scripts.uniform_letter_script(word) != "latin":
        return None
    folded = unicodedata.normalize(
        "NFC",
        "".join(
            character
            for character in unicodedata.normalize("NFD", word)
            if unicodedata.category(character) != "Mn"
        ),
    )
    return folded if folded != word else None


@lru_cache(maxsize=16384)
def _word_unit(word: str, query: bool) -> TokenUnit:
    surface = stem_word(word)
    folded = None if query else _latin_fold(word)
    if folded is None:
        return TokenUnit((surface,), False)
    variant = stem_word(folded)
    stems = (surface,) if variant == surface else (surface, variant)
    return TokenUnit(stems, False)


def _run_unit(run: str, characters: re.Pattern[str]) -> TokenUnit:
    parts = characters.findall(run)
    if len(parts) < 2:
        return TokenUnit((run,), True)
    return TokenUnit(tuple(left + right for left, right in zip(parts, parts[1:], strict=False)), True)


#: Stretches of raw text between ASCII separators. An all-ASCII stretch is
#: exactly one v1 word; only a stretch holding a non-ASCII character is
#: normalised and scanned, so English prose with a typographic dash pays the
#: scanner only around the dash.
_STRETCH_RE = re.compile("[A-Za-z0-9\u0080-\U0010ffff]+")


def _normalized_tokens(stretch: str) -> list[str]:
    """Letter/number tokens of one non-ASCII stretch, after the raw-text
    table, NFKC and casefolding."""
    table = _character_tables()[1]
    normalized = unicodedata.normalize("NFKC", stretch.translate(table)).casefold()
    return _scanner()[0].findall(normalized)


def _nonascii_units(token: str, query: bool) -> list[TokenUnit]:
    """Units of one normalised token that holds a non-ASCII character."""
    _runs, parts, characters = _scanner()
    return [
        _run_unit(part.group(), characters)
        if part.lastgroup == "run"
        else _word_unit(part.group(), query)
        for part in parts.finditer(token)
    ]


def _scan_units(text: str, query: bool) -> list[TokenUnit]:
    """Units of non-ASCII `text`."""
    units: list[TokenUnit] = []
    for stretch in _STRETCH_RE.findall(text):
        if stretch.isascii():
            units.append(TokenUnit((stem_word(stretch.lower()),), False))
            continue
        for token in _normalized_tokens(stretch):
            if token.isascii():
                units.append(TokenUnit((stem_word(token),), False))
            else:
                units.extend(_nonascii_units(token, query))
    return units


def _scan_stems(text: str, query: bool) -> list[str]:
    """`_scan_units` flattened, without building a unit per ASCII word."""
    stems: list[str] = []
    for stretch in _STRETCH_RE.findall(text):
        if stretch.isascii():
            stems.append(stem_word(stretch.lower()))
            continue
        for token in _normalized_tokens(stretch):
            if token.isascii():
                stems.append(stem_word(token))
            else:
                for unit in _nonascii_units(token, query):
                    stems.extend(unit.stems)
    return stems


def token_units(text: str, *, query: bool = False) -> list[TokenUnit]:
    """The stems of `text` grouped by the word or unspaced run they came from.

    `query=True` is the query side: surface forms only, no folded variants.
    Flattening the units gives `tokenize(text, query=query)`.
    """
    if text.isascii():
        return [TokenUnit((stem_word(w),), False) for w in _TOKEN_RE.findall(text.lower())]
    return _scan_units(text, query)


def tokenize(text: str, *, query: bool = False) -> list[str]:
    """The stems of `text`, in order: index side by default, query side on request.

    On ASCII text both sides equal tokenizer v1 (lowercase, `[a-z0-9]+`,
    English Snowball). See the module docstring for everything else.
    """
    if text.isascii():
        return [stem_word(w) for w in _TOKEN_RE.findall(text.lower())]
    return _scan_stems(text, query)


def word_forms(word: str) -> tuple[str, ...]:
    """Index-side stems of one word: its stem, plus the stem of its
    accent-folded form when it is a Latin word with marks. An ASCII word is
    stemmed whole, exactly as `stem_word` always stemmed it. Symbols separate
    before NFKC, as in `tokenize`, so "Zorblex™" is `zorblex`."""
    if word.isascii():
        return (stem_word(word),)
    table = _character_tables()[1]
    parts = unicodedata.normalize("NFKC", word.translate(table)).casefold().split()
    return tuple(
        dict.fromkeys(stem for part in parts for stem in _word_unit(part, False).stems)
    )


def first_stem_span(text: str, stems) -> tuple[int, int] | None:
    """`(start, length)` in `text` of the first word or run carrying one of
    `stems` on its index side, or None. Offsets are into `text` as given; inside
    an unspaced run the matching bigram itself is located when normalisation
    kept the run's length."""
    runs = _scanner()[0]
    for match in runs.finditer(text):
        word = match.group()
        for unit in token_units(word):
            hit = next((stem for stem in unit.stems if stem in stems), None)
            if hit is None:
                continue
            if unit.run:
                normalized = unicodedata.normalize("NFKC", word).casefold()
                offset = normalized.find(hit)
                if offset >= 0 and len(normalized) == len(word):
                    return match.start() + offset, len(hit)
            return match.start(), len(word)
    return None


def run_content_stems(stems) -> tuple[str, ...]:
    """The distinct bigrams of an unspaced run that carry its content.

    Japanese writes particles and inflections in hiragana, so a bigram that
    touches hiragana mostly records grammar: "会議の議事録はいつ共有" shares
    議事, 事録 and 共有 with "議事録は翌日までに共有します" and almost none of
    its particle bigrams. The content is the bigrams without hiragana and
    without the question kanji 何; a run with no such bigram keeps its
    hiragana-free ones, and a run whose every bigram holds hiragana keeps them
    all. Runs in other scripts have no hiragana and keep every bigram.
    """
    distinct = tuple(dict.fromkeys(stems))
    content = tuple(
        stem for stem in distinct if not any(text_scripts.is_hiragana(ch) for ch in stem)
    )
    # 何 (what) builds question words with a counter (何度, 何時, 何月): like a
    # particle it asks rather than names, so "パンは何度で焼きますか" is about パン.
    named = tuple(stem for stem in content if _QUESTION_KANJI not in stem)
    return named or content or distinct


def unit_present(unit: TokenUnit, stems) -> bool:
    """Does text holding `stems` contain this unit?

    A word is present when any of its forms is. An unspaced run is present
    when a strict majority of its content bigrams are (`run_content_stems`):
    requiring all of them would demand the query's exact phrasing, while any
    one of them would accept a page sharing a single particle bigram.
    """
    if not unit.run:
        return any(stem in stems for stem in unit.stems)
    content = run_content_stems(unit.stems)
    return 2 * sum(1 for stem in content if stem in stems) > len(content)


# Back-compat alias for callers that still import _tokenize.
_tokenize = tokenize


class BM25Index:
    """Per-process BM25 corpus over KB markdown files.

    Lazy: nothing happens until `search()` is called. Caches the built
    index keyed by (vault_root, max_mtime, scope). Rebuilds when the
    vault has any file newer than the cached max mtime.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[Path, str], _CorpusCacheEntry] = {}
        #: Searches served: the use signal the idle reaper watches.
        self._hits = 0
        # Per-doc token cache, shared across scopes (a file's tokens don't depend
        # on scope; KB ⊆ vault). Mirrors find.FrontmatterCache's mtime
        # invalidation: a doc is Snowball-tokenized once and reused until its
        # mtime advances, so a rebuild only re-stems the docs that changed.
        # Stale entries for deleted files linger harmlessly — the corpus is
        # assembled only from currently-walked paths; clear() flushes them.
        self._tokens: dict[Path, tuple[float, list[str]]] = {}
        # Diagnostics for the most recent _build(): how many docs were actually
        # (re)tokenized vs reused from cache. Lets tests assert incrementality
        # without timing the wall clock.
        self.last_tokenized: int = 0
        self.last_reused: int = 0
        # Serializes corpus builds: the background warm thread and a racing
        # request must produce ONE build (the loser waits, then reuses).
        self._build_lock = threading.Lock()

    def _doc_tokens(self, path: Path, page) -> list[str]:
        """Tokens for `page`, reusing the cache while the file's mtime is unchanged."""
        cached = self._tokens.get(path)
        if cached is not None and cached[0] == page.mtime:
            self.last_reused += 1
            return cached[1]
        tokens = _tokenize(page.title + " " + page.body)
        self._tokens[path] = (page.mtime, tokens)
        self.last_tokenized += 1
        return tokens

    @staticmethod
    def _derive_bm25(tokens_by_path: dict[str, list[str]]) -> tuple[Any, list[str]]:
        """Recompute corpus-global BM25 maths without touching the filesystem."""
        if not tokens_by_path:
            # rank_bm25 chokes on empty corpora; return a sentinel.
            return None, []
        from rank_bm25 import BM25Okapi

        paths = list(tokens_by_path)
        return BM25Okapi([tokens_by_path[path] for path in paths]), paths

    def _build(
        self, vault_root: Path, scope: str
    ) -> tuple[Any, list[str], dict[str, list[str]]]:
        """Walk the KB (or full vault), tokenize each file, build BM25Okapi.

        Returns the BM25 index, its parallel paths, and the retained per-path
        token corpus. Reuses cached per-doc tokens for unchanged files (see
        `_doc_tokens`), so only changed docs are re-tokenized.
        """
        walk = _recall_walk(vault_root, scope)

        self.last_tokenized = 0
        self.last_reused = 0
        tokens_by_path: dict[str, list[str]] = {}
        for md in walk:
            page = find_module._CACHE.get(md, vault_root)
            if page is None:
                continue
            tokens = self._doc_tokens(md, page)
            if not tokens:
                continue
            tokens_by_path[page.rel_path] = tokens
        bm25, paths = self._derive_bm25(tokens_by_path)
        return bm25, paths, tokens_by_path

    @staticmethod
    def _relative_path(vault_root: Path, path: str) -> str | None:
        """Return a delta path's vault spelling without resolving or stat'ing it."""
        try:
            return Path(path).relative_to(vault_root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _repair_limit(document_count: int) -> int:
        """Maximum delta size worth repairing for the retained corpus."""
        return max(1, int(document_count * MAX_INCREMENTAL_REPAIR_FRACTION))

    def _repair(
        self,
        vault_root: Path,
        scope: str,
        cached: _CorpusCacheEntry,
        target_freshness: tuple,
        policy_identity: tuple[str, str],
    ) -> _CorpusCacheEntry | None:
        """Repair one cached corpus, or return ``None`` when proof is absent."""
        if cached.policy_identity != policy_identity:
            return None

        delta = freshness.recall_delta_since(vault_root, scope, cached.checkpoint)
        delta_policy_identity = (
            delta.to.policy_version,
            delta.to.access_policy_fingerprint,
        )
        if (
            not delta.complete
            or delta.to.triple != target_freshness
            or delta_policy_identity != policy_identity
        ):
            return None

        changed_count = len(delta.changed) + len(delta.deleted)
        if changed_count > self._repair_limit(len(cached.tokens_by_path)):
            return None

        self.last_tokenized = 0
        self.last_reused = 0
        tokens_by_path = dict(cached.tokens_by_path)

        for raw_path in delta.deleted:
            rel_path = self._relative_path(vault_root, raw_path)
            if rel_path is None:
                return None
            tokens_by_path.pop(rel_path, None)

        for raw_path in delta.changed:
            path = Path(raw_path)
            rel_path = self._relative_path(vault_root, raw_path)
            if rel_path is None:
                return None
            if not recall_policy.is_recall_candidate(vault_root, path):
                tokens_by_path.pop(rel_path, None)
                continue
            page = find_module._CACHE.get(path, vault_root)
            if page is None:
                tokens_by_path.pop(rel_path, None)
                continue
            tokens = self._doc_tokens(path, page)
            if not tokens:
                tokens_by_path.pop(rel_path, None)
                tokens_by_path.pop(page.rel_path, None)
                continue
            if page.rel_path != rel_path:
                tokens_by_path.pop(rel_path, None)
            tokens_by_path[page.rel_path] = tokens

        repaired_bm25, repaired_paths = self._derive_bm25(tokens_by_path)
        return _CorpusCacheEntry(
            (*delta.to.triple, *policy_identity),
            repaired_bm25,
            repaired_paths,
            tokens_by_path,
            delta.to,
            policy_identity,
        )

    def _fresh_corpus(
        self, vault_root: Path, scope: str, freshness_key: tuple | None
    ) -> tuple[Any, list[str]]:
        """Return cached corpus state, repaired or rebuilt when identity moves.

        The key is find's digest-strength `_walk_freshness_key` triple — the
        historical `current_max > cached_max` comparison missed deletes,
        renames, and replacements carrying an older mtime. Callers inside a
        `find` request pass the request snapshot's key; out-of-request callers
        compute it from the live registry. A small complete delta repairs only
        its paths. Every unprovable case retains the full-build behavior.
        """
        if freshness_key is None:
            freshness_key = corpus_key(vault_root, scope)
        policy_identity = recall_policy.recall_policy_identity(vault_root)
        cache_identity = (*freshness_key, *policy_identity)
        cache_key = (vault_root, scope)
        cached = self._cache.get(cache_key)
        if cached is None or cached.cache_identity != cache_identity:
            with self._build_lock:
                # Double-check: a concurrent builder may have stored a fresh
                # corpus while this thread waited on the lock.
                cached = self._cache.get(cache_key)
                if cached is None or cached.cache_identity != cache_identity:
                    repaired = None
                    if cached is not None:
                        repaired = self._repair(
                            vault_root,
                            scope,
                            cached,
                            freshness_key,
                            policy_identity,
                        )
                    if repaired is not None:
                        log.debug(
                            "bm25: repaired index for %s scope=%s", vault_root, scope
                        )
                        cached = repaired
                    else:
                        log.debug(
                            "bm25: rebuilding index for %s scope=%s", vault_root, scope
                        )
                        checkpoint = freshness.recall_checkpoint(vault_root, scope)
                        build_policy_identity = (
                            checkpoint.policy_version,
                            checkpoint.access_policy_fingerprint,
                        )
                        bm25, paths, tokens_by_path = self._build(vault_root, scope)
                        cached = _CorpusCacheEntry(
                            (*checkpoint.triple, *build_policy_identity),
                            bm25,
                            paths,
                            tokens_by_path,
                            checkpoint,
                            build_policy_identity,
                        )
                    self._cache[cache_key] = cached
        return cached.bm25, cached.paths

    def search(
        self,
        vault_root: Path,
        query: str,
        k: int,
        *,
        scope: str = "kb",
        freshness: tuple | None = None,
        allowed_paths: set[str] | None = None,
        repair: bool = True,
    ) -> list[tuple[str, float]]:
        """Return top-k `(rel_path, bm25_score)` for `query`. Empty query → [].

        Backend ladder: the FTS5 lexical sidecar serves the lane when
        available (posting-list cost instead of scoring all N docs); any
        unavailability — kill switch, FTS5 absent, sidecar failure — falls
        through to the in-process BM25Okapi rung below, which remains the
        reference implementation and the `EXOMEM_LEXICAL_BACKEND=python`
        target. Interface identical either way.
        """
        if not query.strip():
            return []
        from . import lexstore

        indexed = lexstore.search_bm25(
            vault_root,
            query,
            k,
            scope=scope,
            freshness=freshness,
            allowed_paths=allowed_paths,
            repair=repair,
        )
        if indexed is not None:
            return indexed
        # Counted only where the python corpus actually serves: a sidecar-served
        # query touches nothing the reaper could reclaim here.
        self._hits += 1
        bm25, paths = self._fresh_corpus(vault_root, scope, freshness)
        if bm25 is None or not paths:
            return []
        tokens = tokenize(query, query=True)
        if not tokens:
            return []
        scores = bm25.get_scores(tokens)
        ranked = sorted(
            (
                (path, score)
                for path, score in zip(paths, scores, strict=True)
                if (allowed_paths is None or path in allowed_paths)
                and bool(
                    set(tokens)
                    & set(self._tokens[vault_root / path][1])
                )
            ),
            key=lambda item: (-item[1], item[0]),
        )[:k]
        # Rank-BM25's epsilon fallback can make every matching score
        # non-positive in a tiny corpus. Token overlap, not score sign, is the
        # relevance proof; otherwise a valid lone structured manifest vanishes.
        return [(p, float(s)) for p, s in ranked]

    def warm(self, vault_root: Path, scope: str = "kb") -> None:
        """Build (or freshness-check) whichever backend serves this lane —
        the startup warm-up hook, so the first hybrid find doesn't pay the
        first-build cliff (sidecar sync/population under FTS5; the corpus
        stemming build on the in-process rung)."""
        from . import lexstore

        if lexstore.search_bm25(vault_root, "warm", 1, scope=scope) is not None:
            # FTS5 serves: the probe query ran the sync check and faulted the
            # index in. The rank-bm25 corpus stays cold on purpose — not
            # holding N token lists resident is part of the backend's win;
            # a mid-process FTS5 retirement pays one rebuild, lazily.
            return
        self._fresh_corpus(vault_root, scope, None)

    def unload_cache(self) -> bool:
        """Drop rebuildable in-process BM25 corpus/token caches."""
        with self._build_lock:
            loaded = bool(self._cache or self._tokens)
            self._cache.clear()
            self._tokens.clear()
            self.last_tokenized = 0
            self.last_reused = 0
            return loaded

    def cache_status(self) -> dict:
        """No-allocation residency status for the Python BM25 rung."""
        with self._build_lock:
            doc_count = sum(len(entry[2]) for entry in self._cache.values())
            token_count = sum(len(tokens) for _mtime, tokens in self._tokens.values())
            return {
                "loaded": bool(self._cache or self._tokens),
                "hits": self._hits,
                "corpora": len(self._cache),
                "documents": doc_count,
                "tokenized_documents": len(self._tokens),
                "tokens": token_count,
            }

    def clear(self) -> None:
        self.unload_cache()


def corpus_key(vault_root: Path, scope: str) -> tuple:
    """Projected three-field recall triple for lexical sidecars."""
    return freshness.recall_triple(vault_root, scope)


def _recall_walk(vault_root: Path, scope: str):
    if scope == "vault":
        from .vault import walk_vault_md

        walk = walk_vault_md(vault_root)
    else:
        kb = vault_root / kb_dirname()
        if not kb.is_dir():
            return ()
        walk = find_module._walk_md(kb)
    return recall_policy.iter_recall_markdown(vault_root, walk)


_INDEX = BM25Index()


def search(
    vault_root: Path,
    query: str,
    k: int,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
    allowed_paths: set[str] | None = None,
    repair: bool = True,
) -> list[tuple[str, float]]:
    """Module-level convenience using the per-process singleton."""
    return _INDEX.search(
        vault_root,
        query,
        k,
        scope=scope,
        freshness=freshness,
        allowed_paths=allowed_paths,
        repair=repair,
    )


def warm(vault_root: Path, scope: str = "kb") -> None:
    """Module-level warm-up hook using the per-process singleton."""
    _INDEX.warm(vault_root, scope)


def unload_cache() -> bool:
    """Evict the singleton BM25 corpus/token cache without touching vault files."""
    return _INDEX.unload_cache()


def cache_status() -> dict:
    """No-allocation residency status for the singleton BM25 cache."""
    return _INDEX.cache_status()


def clear_cache() -> None:
    """Test hook: flush the singleton cache between tests."""
    _INDEX.clear()
