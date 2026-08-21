# Design: add-referent-resolution

## Context

The recall cache stores principal-free `Hit` objects. Per-audience release
decisions are attached later in `commands.op_find`, so the resolver must consume
released hits without entering the cache or changing rank.

## Alternatives considered and rejected

- Data maintenance alone improves specific entities but still gives agents no
  count, candidate, or abstention semantics.
- Scoring graph degree or adding a referent intent would reorder every query and
  require ranking-contract migration before a benchmark exists.
- New social/geographic predicates duplicate information already authored in
  person attributes, tags, prose, and `about_entity`/`relates_to` edges.
- A model-backed coreference pass violates the measure-and-surface boundary and
  introduces latency and nondeterminism.

## The predicate

`detect_cue` recognizes a closed set of person nouns plus aliases from the
existing entity-type registry. Count words or integers apply only within the
three tokens preceding the cue noun. The runtime is eligible only for hybrid or
vector recall and is skipped under `EXOMEM_DISABLE_REFERENTS`.

Active entities produce categorical evidence:

- `exact_name`: normalized title/alias occurs contiguously in the query;
- `fuzzy_name`: bounded Levenshtein over prefiltered identity/query tokens;
- `retrieval`: the entity itself occurs in a released hit lane;
- `graph`: a one-hop typed or `links_to` edge joins the entity to one of the
  first ten non-superseded released anchors;
- `attribute`: cue descriptors match tags, relationship, or affiliation by
  stem equality or a four-character-or-longer prefix.

Exact name resolves alone. Otherwise the entity type must match the cue and at
least two distinct evidence kinds are required. One kind remains a candidate.
Inactive entities and type mismatches are dropped and counted. Every tie sorts
by path. Evidence and output contain no confidence floats.

Expected count N yields `resolved` at exactly N, `partial` plus
`unresolved_count` below N, and `ambiguous` above N. Without a count, zero
resolved entities is `unresolved` and any nonzero set is `resolved`.

## Runtime and cache boundary

The new stage runs after `egress.annotate_hits` and before serialization inside
the optional `referents` timing span. The entity registry is immutable and
cached by `FreshnessSnapshot.projection_key("kb")`. Graph evidence performs one
bounded `neighbors_for(anchors[:10])` call only when the sidecar is available.
The resulting block is never placed on `Hit` and never cached. Any exception
omits the block and preserves the response.

## Governance

`guard_referents` applies the active audience/purpose decision to entity paths,
drops evidence naming withheld anchor seeds, and returns no block for a blocked
audience. It also observes tombstoned/withheld paths already present on the
release result. This is required because registry/name/graph candidates may not
have passed through `annotate_hits` as hits.

## Validation against the real case

For the synthetic equivalent of the product sentence, the travel topic is a
released anchor. Its inbound `relates_to` edge supplies graph evidence for the
represented person; `relationship: friend` and the location tag supply one
attribute evidence kind. With an expected count of two, exactly one represented
person produces `partial` and `unresolved_count: 1`. A noise person retrieved by
wording alone remains in candidates, so the agent names the represented person
and states that one identity remains unresolved.

## Performance

Non-cues and keyword mode open no stage. Cue queries reuse the entity registry
until the KB checkpoint changes, scan at most the bounded hit prefix for graph
anchors, and do no write-path work. The scale gate holds the warm stage below
1000 ms and prevents linear 2k-to-8k growth beyond the existing ratio shape.
