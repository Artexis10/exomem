## ADDED Requirements

### Requirement: Semantic-Unit Recall Queries Derived Rows First

Semantic-unit recall SHALL query current-generation lexical/vector unit rows before hydrating Markdown. The optimized unit lane MUST NOT build an all-parent or all-unit in-memory projection as a prerequisite to searching. Page-level eligibility work performed by mixed recall is outside this requirement.

#### Scenario: Keyword mixed recall touches only selected parents

- **WHEN** a warm keyword mixed-level query matches a bounded set of semantic units
- **THEN** ranking is computed from current derived rows
- **AND** only selected parents needed for returned context are opened

#### Scenario: Indexed category and kind filters remain exact

- **WHEN** category or kind filters are supplied
- **THEN** the returned units are identical to the full Markdown oracle
- **AND** the implementation does not parse every eligible parent to evaluate the filters

#### Scenario: A post-hydration filter exhausts the bounded window

- **WHEN** a tags, context, lifecycle, project, or other structured predicate rejects every indexed candidate in the bounded window
- **THEN** the response reports `semantic_units_candidate_window` when it cannot fill the requested result count
- **AND** it does not silently run an unbounded whole-vault parse

### Requirement: Outside-KB Widening Uses The Maintained Lexical Store

`scope="kb"` auto-widen SHALL use current vault-scope lexical sidecar rows for outside-KB candidates. It MUST preserve the relaxed any-stem gate, reserved result slots, exclusions, and `outside_kb` markers without rebuilding an in-memory vault BM25 corpus in the request.

#### Scenario: Empty KB result widens without a vault rebuild

- **WHEN** a warm keyword query has no KB hit and auto-widens outside Knowledge Base
- **THEN** outside candidates come from the maintained vault lexical store
- **AND** the request performs no vault-wide Markdown walk, parse, or BM25 reconstruction

### Requirement: Missing Derived State Never Triggers An Unbounded Foreground Fallback

When semantic-unit or outside-KB derived state is missing, schema-stale, or reconciling, `find`/`ask_memory` SHALL report the affected lane as warming or unavailable and trigger/join repair. It MUST NOT silently fall back to a whole-vault foreground parse or tokenization pass.

#### Scenario: Lexical sidecar is rebuilding

- **WHEN** outside-KB lexical state is not current during an auto-widen request
- **THEN** the response identifies outside-KB recall as warming or unavailable
- **AND** other current lanes still return bounded results
- **AND** no unbounded filesystem fallback runs
