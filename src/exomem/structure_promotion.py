"""Term helpers and the breadth tags that other modules share.

This module once held the lexical `scope_divergence` detector, which compared a
page's unit tags with its declared identity. It was measured against owner-labelled
notes, found to track page size rather than meaning, and removed. Whether a page
answers more than one question is now asked of the connected agent (see
`note_cohesion`), because that is a judgement about meaning that a word list cannot
make.

What remains is vocabulary-free plumbing that other modules import:

- `_terms` and `_is_term` normalise tags, titles and project keys into one
  comparable set of words, script-aware (`text_scripts`);
- `FUNCTION_WORDS` and `_STOPWORDS` keep glue words out of that set;
- `BREADTH_TAGS` names the tags that declare a page broad on purpose.

The collection and record-governance matchers import these. A change to them changes
every one of those comparisons, so none of them belongs to a single caller.
"""

from __future__ import annotations

from collections.abc import Iterable

from .text_scripts import comparison_words, is_scriptio_continua

#: Pages that deliberately announce breadth. Same convention that already exempts
#: these tags from staleness pressure and semantic-unit requirements elsewhere.
BREADTH_TAGS = frozenset({"hub", "snapshot"})

#: Closed-class English function words: prepositions, conjunctions, auxiliaries,
#: pronouns, determiners and a few ubiquitous verbs. Never a content word, however
#: common. Shared by every comparison built on `_terms` (scope divergence here,
#: collection claims routing), so declared prose such as "not because after first
#: use" contributes no term that an unrelated page could share.
FUNCTION_WORDS = frozenset(
    {
        "a", "about", "across", "after", "again", "against", "all", "also", "am",
        "among", "an", "and", "any", "are", "around", "as", "at", "be", "because",
        "been", "before", "being", "between", "both", "but", "by", "can", "could",
        "did", "do", "does", "done", "down", "during", "each", "either", "else",
        "every", "first", "for", "from", "had", "has", "have", "he", "her", "here",
        "his", "how", "if", "in", "into", "is", "it", "its", "just", "last", "may",
        "might", "more", "most", "must", "neither", "never", "no", "nor", "not",
        "now", "of", "off", "on", "once", "only", "onto", "or", "other", "our",
        "out", "over", "per", "same", "shall", "she", "should", "since", "so",
        "some", "still", "such", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "thus", "too",
        "toward", "towards", "under", "until", "upon", "use", "used", "using",
        "very", "via", "was", "we", "were", "what", "when", "where", "whether",
        "which", "while", "who", "whom", "whose", "why", "will", "with", "within",
        "without", "would", "yet", "you", "your",
    }
)

# Title words that carry no subject: the function words plus navigation glue.
# Deliberately small: this is a stop list for glue, not an attempt to model English.
_STOPWORDS = FUNCTION_WORDS | frozenset(
    {"note", "notes", "plan", "plans", "draft", "overview", "summary"}
)


def _is_term(token: str) -> bool:
    """Long enough to be a word, and not a function word or a number.

    Three characters, except in an unspaced script, where one character is a
    syllable or a morpheme and two are an ordinary word (故障, 体重).
    """
    return (
        (len(token) > 2 or (len(token) == 2 and all(map(is_scriptio_continua, token))))
        and token not in _STOPWORDS
        and not token.isdigit()
    )


def _terms(values: Iterable[str]) -> frozenset[str]:
    """Normalise tags, title words, and project keys into one comparable vocabulary.

    Words are `text_scripts.comparison_words`: ASCII splits exactly as it
    always has, and a Japanese title yields its words instead of nothing.
    """
    out: set[str] = set()
    for value in values:
        out.update(token for token in comparison_words(str(value).casefold()) if _is_term(token))
    return frozenset(out)
