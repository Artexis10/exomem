## ADDED Requirements

### Requirement: Find corroborates a BM25-only candidate by word majority

When no semantic, graph or keyword lane corroborates a find candidate, the
candidate SHALL be retained only when a strict majority of the query's
whitespace words is present in the page — a word counting as present when
every one of its required stems occurs — and at least one present word is
a content word. This gate SHALL apply whether or not the semantic lanes
produce other candidates; the absence of a semantic hit for one candidate
SHALL not by itself impose a stricter gate on that candidate.

#### Scenario: A natural question keeps its page while lanes are live

- **WHEN** the semantic lanes produce candidates for a natural-language
  question and a page ranked only by BM25 holds a strict majority of the
  query's words, including at least one content word, but lacks some of
  the question's phrasing words
- **THEN** the page is retained

#### Scenario: Half the words still vetoes under live lanes

- **WHEN** the semantic lanes produce candidates and a BM25-only page
  holds exactly half of the query's words
- **THEN** the page is vetoed

#### Scenario: A function-word majority still vetoes under live lanes

- **WHEN** the semantic lanes produce candidates and a BM25-only page
  holds a majority of the query's words only among function words
- **THEN** the page is vetoed
