## MODIFIED Requirements

### Requirement: Disclosure ladder and single per-level projector

The release plane SHALL support the ordered disclosure ladder L0 none, L1 notice, L2
constraint, L3 abstract, L4 bridge-approved abstraction, L5 fixed bounded excerpt, and
L6 full permitted representation. L4 SHALL NOT use the legacy redacted-excerpt
projector. It SHALL emit only the canonical abstraction carried by an exact approved,
non-stale bridge whose bound dependency identity/content hash verifies; missing,
conflicting, or stale bridge authority lowers/fails closed and MUST NOT fall back to a
source excerpt. L5 SHALL use only the canonical fixed `_excerpt_of` transform defined by
the active projector schema. L6 SHALL expose the full governance-permitted
representation, subject to the mandatory terminal secret scrubber.

A single registered per-level projector SHALL be the only path from any candidate (hit,
page, pack element, unit, or Markdown companion) to a wire representation. Ranking
signals, graph seed provenance, relation-match annotations, matched units, supersession
pointers, and parent references SHALL appear only at L5-L6 and SHALL be stripped at
every level when they name a sub-notice item. Lower-level textual projections apply only
to Markdown/page or explicitly registered companion representations. Dataset rows/
aggregates/profile, Records values/reductions, media bytes, and frame pixels remain
L6-or-missing because this change registers no lower structured projector. Any content-
returning representation without an exact registered projector SHALL fail closed.

#### Scenario: L4 is an approved abstraction and never an excerpt

- **WHEN** a Markdown item resolves at L4 with an exact current bridge approval
- **THEN** the projector emits only the bridge-approved canonical abstraction and no
  bounded/raw source excerpt, path, provenance, or hidden dependency field

#### Scenario: Stale L4 bridge does not fall back to redaction

- **WHEN** the L4 bridge or any bound dependency identity/hash is missing, conflicted, or
  stale
- **THEN** release lowers/fails closed and MUST NOT invoke the former redacted-excerpt
  projector or borrow L5/source text

#### Scenario: Low levels strip metadata oracles

- **WHEN** an item is released at L1
- **THEN** the response carries only its canonical notice/rule/scope projection and no
  score, signal, graph seed, relation match, matched unit, supersession pointer, or path

#### Scenario: Scores only at high levels

- **WHEN** an item is released below L5
- **THEN** no ranking signal or similarity score for it crosses the boundary

#### Scenario: Structured direct representations have no lower projector

- **WHEN** dataset, Records, media, or frame content resolves at L1-L5
- **THEN** that direct representation is the ordinary missing envelope; only a separately
  requested bound Markdown companion may receive its Markdown level projection

#### Scenario: Unregistered surface fails closed

- **WHEN** a content-returning command or representation has no registered projector
- **THEN** it emits no path, title, excerpt, row, byte, or frame, and coverage fails

### Requirement: Decision annotation and request-deterministic counts

Release decisions SHALL be computed over the complete indexed artifact catalog before
any caller-visible selection or reduction, per item, keyed on the caller's canonical
principal, declared purpose, verified authorization session, and active grants. For a
fixed caller, request, policy snapshot, and deterministic runtime configuration, every
public result SHALL be observationally equivalent to running the request against the
caller-visible projected corpus: L0 items and disallowed edges are absent, while L1-L6
items contribute only fields authorized by their level.

Governed retrieval SHALL NOT acquire candidates from a capped raw BM25, vector, rerank,
CLIP, or graph result and filter afterward. It SHALL use an authorization-projection
namespace whose complete key is exactly `(policy_fingerprint,
projector_schema_version, catalog_generation)`. Immutable content identity SHALL be a
row key inside that namespace. Extractor/model versions SHALL be per-lane measurement
subkeys beneath a projection row and MUST NOT alter or alias the namespace key. Each
request SHALL evaluate query-time membership and the per-scope decision over that exact
catalog snapshot and build a request-local authorization map selecting exactly one fixed
canonical projection variant or L0 for every artifact; principal, purpose, session, and
grant identity MUST NOT enter a persistent index or shared hot-cache key.

For each item, namespace construction SHALL enumerate only unique decision outputs
reachable from the finite compiled domain of audience equivalence class, declared/
undeclared purpose class, scope membership, and standing/session-grant levels. It SHALL
materialize at most `MAX_PROJECTION_VARIANTS_PER_ITEM = 256` non-L0 variants. L0 SHALL
have no row and consume no cap. More than 256 unique reachable outputs for any item SHALL
block policy activation with an owner-only diagnostic; no variant may be dropped,
permissively merged, lazily query-generated, or selected by authoring order.

Every row SHALL use
`projection_variant_id = SHA-256("exomem.authorization-projection.v1\\0" || JCS(value))`,
where RFC 8785 `value` contains exactly immutable item identity and content hash,
decision level, canonical closed-registry options, canonical release-strip set,
canonical bridge id plus approved bridge-dependency content hash or `null`, and projector
schema version. Its fixed canonical searchable representation SHALL be L0 absent; L1
notice only; L2 constraint only; L3 abstract only; L4 bridge-approved abstraction only;
L5 `_excerpt_of` the post-provenance/post-release-strip body; and L6 all and only full
permitted search fields after strip policy. A decision missing content required for its
level SHALL lower/fail closed before enumeration and SHALL NOT borrow sibling content.

L5 `_excerpt_of` SHALL compute `text = " ".join(body.split())`; return `text` when it has
at most 600 Unicode code points; otherwise set `prefix = text[:600]`, replace it with the
substring before its last U+0020 when one exists, and return
`prefix + U+0020 + U+2026`. It SHALL be fixed for the item/decision and independent of
query, scorer, requested limit, and text beyond the prefix. An L5 term outside that
excerpt SHALL not acquire the item. Lexical matching,
one fixed vector keyed by `(projection_variant_id, model_version)`, reranking, and search
snippets SHALL consume only that fixed variant; snippets MUST NOT open a query-centered
window over hidden source.

Lexical postings SHALL include every projection variant. BM25 candidate matching,
document frequency, IDF, scoring, and exact top-k SHALL intersect those postings with the
authorization map before any cap. Vector acquisition SHALL score the selected projected-
text embedding rows before its cap; a missing/stale selected embedding SHALL warm or
disable that visible lane and MUST NOT fall back to a raw embedding. Reranking SHALL
receive only selected projected fields and SHALL precede final top-k. CLIP pixels and
keyframes SHALL participate only for artifacts selected at L6, with the authorization
map applied inside the lane before its cap. Each L6 media projection SHALL bind one
immutable measurement row: exactly one untimestamped vector for an image, or one through
forty vectors for a video in strict canonical `frame_timestamp_ms` order. The forty-sample
ceiling SHALL NOT be configurable. The lane SHALL score every authorized sample, return
the parent media item once at its best score, and bind the earliest best frame timestamp.
Below L6, image/video recall SHALL use only
an authorized textual companion projection, or exclude the binary lane when none is
available. Graph vertices and edges SHALL be admitted before expansion and every graph
reduction SHALL run over the selected projected graph.

Fusion, ordering, snippets, public rank fields, top-k, pagination, cursors, totals,
facets, ambiguity, continuations, diagnostics, and error reduction SHALL consume the
complete outputs of those projected lanes. L0 artifacts SHALL NOT consume a posting,
lane, over-fetch, graph-frontier, top-k, or pagination slot. Hidden text, provenance,
pixels, vertices, edges, raw-corpus IDF, and raw candidate position MUST NOT affect a
released item's acquisition or position. An error belonging only to an L0 artifact SHALL
be absorbed as absence.

The only active authority SHALL be the transactional
`(policy_generation/fingerprint, projector_schema_version, catalog_generation)` tuple.
The required namespace SHALL be built and validated before that tuple activates. Policy
commits SHALL compare-and-swap the complete expected tuple including catalog generation;
content create/edit/delete and companion writers SHALL stage a new immutable catalog and
all required lanes, then compare-and-swap the complete expected tuple including policy/
projector. A race SHALL have one winner and one stale retry, never independently current
policy, catalog, graph, or projection pointers. Readers SHALL snapshot the tuple once.
Every catalog row SHALL bind immutable source identity and content hash; a held source or
companion snapshot that differs before its new tuple commits SHALL be content-free stale/
warming state and MUST NOT use the old projection or raw lane.
Item writes SHALL reuse only verified content-addressed unchanged rows rather than
mutating the prior namespace. Policy-fingerprint/projector changes SHALL build a fresh namespace tuple; model/extractor
changes SHALL version only their measurement subkeys. Initial migration SHALL build
exactly `(active_policy_fingerprint, projector_schema_version, catalog_generation)` from
the complete catalog while non-owner governed retrieval returns one stable content-free
warming response. Incomplete required lexical rows, duplicate identity, variant-id
mismatch, per-item variant overflow, or namespace overflow SHALL block activation; a raw
or prior-policy index MUST NOT be used as fallback. Old namespaces MAY be collected only
after no active request or cursor binds their exact tuple.

When the active tuple requires a CLIP measurement family, a content or companion
publication SHALL verify the complete active image/video family before canonical bytes
change. The successor SHALL carry only content-identical image/video rows, require an
exact target item identity and target content hash for each changed visual-media
replacement, and bind the complete successor family to the target projection namespace.
An image SHALL retain exactly one untimestamped sample and a video one through forty
strictly timestamp-ordered canonical samples. A derived frame companion carrying
`parent_media` SHALL remain a textual catalog artifact and SHALL NOT become an
independent CLIP measurement owner. Missing, stale, duplicate, mismatched, or dimension-
incompatible CLIP state SHALL refuse before canonical bytes change. The successor catalog,
projection namespace, vector/CLIP measurement roots, receipt, and active tuple SHALL
publish atomically; graph or another unsupported required family SHALL remain blocked.

#### Scenario: Visual successor publication is complete and atomic

- **WHEN** an exact-v4 catalog edit carries an unchanged image or video, replaces a
  changed video's canonical samples, or adds a derived frame companion
- **THEN** the next namespace carries or replaces only exact target-bound CLIP rows,
  excludes the frame companion from binary ownership, and activates its complete vector
  and CLIP roots in the same catalog transaction

#### Scenario: Scene sampling publishes the parent row without a second model pass

- **WHEN** the live media worker or bulk backfill computes video scene vectors and
  persists the corresponding frame companions
- **THEN** it canonicalizes those same vectors once to bounded integer milliseconds,
  binds them to the guarded parent sidecar, and publishes the parent CLIP replacement,
  companion catalog rows, measurement roots, and active tuple together without
  re-running CLIP or creating pixel rows for the companions

#### Scenario: Incomplete visual state refuses before bytes

- **WHEN** the active CLIP family is incomplete or a replacement has a stale item,
  content hash, sample shape, timestamp order, or vector dimension
- **THEN** catalog preparation refuses before canonical content or the active tuple
  changes and does not fall back to a raw or prior CLIP family

When the projected source is exhausted, L1-and-above items SHALL still emit the
projection their policy authorizes while L0 items SHALL produce a silently shorter list,
identical to physical absence. The canonical governed envelope for the same input SHALL
be byte-identical with an L0 item/edge present or physically absent. That normative
envelope includes application status, code, message, remediation, data/content, ranks,
top-k, pagination/cursors, order, counts, graph fields, warnings, diagnostics,
application timing fields, and any application request/correlation id after projection,
error normalization, canonical serialization, and terminal secret scrubbing. A
registered transport normalization MAY exclude only framing created outside that
envelope: an echoed JSON-RPC request id, HTTP `Date`/outer trace headers, TLS/chunk/
compression framing, and physical network arrival time. It MUST NOT delete arbitrary
application fields to obtain equality.

Governed find SHALL expose one optional input/output `continuation` with exact grammar
`pc1.` followed by 64 lowercase hexadecimal SHA-256 characters. The token SHALL be the
SHA-256 digest of NUL-terminated ASCII domain
`exomem.projected-find-continuation.v1\0` followed by RFC 8785 JCS containing only
canonical principal id, verified authorization-session id or null, declared purpose or
null, the SHA-256 request digest, next visible offset, and one caller-visible snapshot
digest. The request digest SHALL be SHA-256 over RFC 8785 JCS of the closed typed object
`{auto_rerank, graph, limit, mode, prefer_active, prefer_compiled, query, rerank,
scope}` after public defaults are applied. The
snapshot digest SHALL stream the ordered item/variant-id pairs under NUL-terminated ASCII
domain `exomem.projected-visible-snapshot.v1\0`, followed by a big-endian u32 pair count and, for
each UTF-8 field, a big-endian u32 byte length plus its bytes. It SHALL exclude
vault/root identity, L0 identities, catalog count/generation, issuance
time, randomness, and server-only authorization facts. A bounded process-local record
SHALL retain the exact immutable runtime, authorization-map/selected-projection/request/
principal/visible-snapshot digests, next offset, and the first page's repository-derived
candidate depth for 15 monotonic minutes, with a hard 4,096-record process cap and
expired-record collection before admission. The token is a lookup key, not authority.

Every continuation SHALL be exact-vault/principal/session/purpose/request-bound. The
server SHALL require current policy fingerprint and projector schema parity, re-run
current authorization against the retained namespace, compare the current namespace's
selected-projection digest while excluding L0 rows, and compare the complete
authorization-map and visible-snapshot digests before returning the next slice. Policy,
projector, session, grant, revocation, or visible-result drift SHALL return the same
content-free `INVALID_CONTINUATION` refusal; hidden-only catalog change SHALL continue
over the retained namespace. Invalid, malformed, expired, evicted, restart-lost, cross-
binding, and registry-capacity outcomes SHALL share that refusal under the fixed public
completion class. Retry SHALL be deterministic, read-only, non-consuming, and SHALL NOT
extend expiry. Old runtimes SHALL remain strongly referenced only by live continuation
records and become collectible when the last record expires or is removed.
A separately issued first page MAY refresh a byte-identical token to the current
verified runtime and fixed expiry; a continuation replay MUST NOT replace that newer
record with prior state.

Later pages SHALL reuse the first page's retained candidate depth exactly. Advancing an
offset MUST NOT widen a primary vector/BM25 prefix, reclassify a graph-only hit, or
recompute a different ordered window; exhaustion of that bounded window SHALL omit the
continuation.

Serialized timing suppression alone SHALL NOT satisfy timing non-interference. Governed
non-owner payloads SHALL use one stable `timings_suppressed` shape chosen from public
policy/principal state. The repository SHALL normatively define
`MAX_HIDDEN_CORPUS_WIRE_DELTA_MS = 25`,
`MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO = 0.10`,
`MAX_GOVERNED_CATALOG_ITEMS = 16_384`,
`MAX_GOVERNED_SEARCH_BYTES_PER_ITEM = 1_048_576`, and
`MAX_GOVERNED_GRAPH_EDGES = 262_144`. A release manifest MAY lower the 25 ms
`hidden_corpus_wire_delta_ms` but MUST NOT raise it. Raising any timing or supported-
capacity constant SHALL require a reviewed spec change and MUST NOT be possible through
a manifest, environment, or operator override.

`capacity` SHALL mean a present replica with exactly 16,384 governed catalog identities,
at least one item at exactly 1,048,576 searchable bytes, and exactly 262,144 indexed
graph edges. Larger binary artifacts MAY be supported only through separately bounded
extracted text/visual measurements and MUST NOT increase query-time searchable bytes.
An actual-wire gate SHALL fix at least 200 samples per condition, the hardware/runtime
profile, and one repository-registered public deadline/padding class before sampling,
then randomize and interleave warmed, quiescent hidden-present and physically-absent
replicas for zero, one, and the exact maximum capacity across lexical, vector, rerank,
CLIP, graph, error, and pagination routes. Scheduler tolerance MAY be reported but MUST
NOT be subtracted from observations or added to a ceiling.

Each model execution profile SHALL have a closed literal identity, exact device/backend/
hard-off configuration, exact required measurement families, exact route set, and exactly
one repository-owned completion class. Evidence or a manifest for one profile MUST NOT
activate another profile. `vectors-cpu-torch-v1` SHALL mean text embeddings enabled,
`EXOMEM_DEVICE=cpu`, `EXOMEM_EMBED_BACKEND=torch`, absent per-model and legacy device
overrides for the embedding lane, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, CLIP
hard-off, and reranking hard-off. It SHALL require the exact active
`projected-text-v1`/`BAAI/bge-base-en-v1.5` vector family and SHALL use only
`projected-find-vector-cpu-v1` with 1,000 ms padding and a 1,500 ms deadline. Its vector
model input SHALL normalize whitespace with `" ".join(query.split())`, retain at most
the first 600 Unicode code points, and, when truncated and a U+0020 exists, retreat to
the preceding complete token. This model-only transform MUST NOT alter lexical input or
the full-query request/continuation binding. Every unregistered, mixed, override-bearing,
GPU, ONNX, reranker-active, or CLIP-active profile SHALL remain non-serving.

For every route/public request class, the 99% bootstrap upper confidence bounds for
absolute median and p95 completion-time differences SHALL each be no greater than the
manifest differential, 25 ms, and 10% of the physically-absent p95. Both conditions
SHALL use the same fixed public deadline and padding based only on registered request
shape/runtime configuration. Failure to complete maximum capacity under that class or
to meet either absolute or relative ceiling SHALL fail the release; capacity, lanes,
padding, or ceilings MUST NOT be adapted after observation. This is a bounded empirical
threat/test contract, not a cryptographic constant-time or arbitrary-Internet-jitter
claim. Operational server logs MAY retain timings but SHALL NOT contain hidden item
identity or authorization bearers.

#### Scenario: L0 presence cannot displace top-k

- **WHEN** the same query is run with a highly ranked L0 item present and then physically
  absent
- **THEN** the serialized top-k permitted results, their order, ranks, and shown count
  are byte-identical

#### Scenario: Vector evidence cannot be borrowed by another runtime

- **WHEN** an operator relabels hard-off evidence, supplies a device override, omits the
  exact vector family, or enables reranking or CLIP under the vector CPU profile
- **THEN** projected serving refuses before model invocation or content disclosure

#### Scenario: Maximum public query has a fixed vector cost

- **WHEN** a valid 4,096-code-point query reaches `vectors-cpu-torch-v1`
- **THEN** lexical lanes and request binding use that complete query while the vector
  model receives only the fixed canonical at-most-600-code-point projection used by the
  release gate

#### Scenario: Projection-only lexical match is acquired

- **WHEN** an authorized L2-L5 projection contains a query term absent from the stored raw
  candidate fields used by the legacy BM25 source
- **THEN** the projection posting acquires and scores the item with projected-corpus IDF,
  even when raw candidates ahead of it exceed every legacy lane cap

#### Scenario: Projection-only vector and rerank use selected text

- **WHEN** a lower-level projected abstraction is semantically relevant but the hidden
  body is not, or vice versa
- **THEN** vector acquisition and reranking use only the selected projection embedding/text
  and return the exact projected-corpus top-k without raw fallback

#### Scenario: Namespace and row identities do not drift across lanes

- **WHEN** one catalog generation has multiple item identities and model/extractor
  versions
- **THEN** its namespace key remains exactly the policy fingerprint, projector schema
  version, and catalog generation; item identity is a row key and lane versions are
  measurement subkeys

#### Scenario: Reachable variants are finite and capped

- **WHEN** the compiled audience/purpose/scope/grant domain yields duplicate outputs or
  more than 256 unique non-L0 outputs for one item
- **THEN** identical canonical variant ids deduplicate, but overflow blocks policy
  activation without dropping or query-generating a variant

#### Scenario: L5 find uses one fixed excerpt

- **WHEN** an L5 item's first canonical 600-code-point whole-token excerpt omits a term
  that occurs later in the hidden body
- **THEN** that term does not acquire, embed, rerank, or snippet the item, while a term in
  the fixed excerpt uses the same variant vector and may acquire it

#### Scenario: Snippet cannot reopen hidden context

- **WHEN** a query matches near the boundary of an L1-L5 fixed projection
- **THEN** its snippet is derived solely from the selected fixed variant and never from a
  query-centered source-body window

#### Scenario: CLIP is full-only and pre-cap authorized

- **WHEN** a hidden image or video would be the highest raw CLIP match
- **THEN** it is excluded inside the CLIP lane before the cap, cannot displace an L6 visual
  hit, and below L6 participates only through an authorized textual sidecar projection

#### Scenario: Pagination is computed after authorization

- **WHEN** L0 items sort before, between, or after permitted items across multiple pages
- **THEN** page membership, cursor/continuation, total, ordering, and exhaustion are
  identical to a corpus where the L0 items do not exist

#### Scenario: Hidden-only change preserves a live continuation

- **WHEN** a caller obtains a continuation and only L0 catalog items change before the
  next page
- **THEN** the retained projected snapshot returns the identical next page and token
  behavior while current authorization is revalidated

#### Scenario: Authority drift invalidates rather than broadens a cursor

- **WHEN** policy, projector, principal, verified session, grant, revocation, purpose,
  request shape, or any caller-visible selected variant differs from the cursor record
- **THEN** the request returns the same content-free `INVALID_CONTINUATION` refusal and
  emits no stale page, count, path, or drift detail

#### Scenario: Ranking uses only projected fields

- **WHEN** an L1-L5 item contains query terms or provenance outside its permitted
  projection
- **THEN** those hidden fields do not improve its candidate selection, fusion score,
  rerank, snippet, graph rank, or order

#### Scenario: Hidden graph structure has no public effect

- **WHEN** an L0 vertex or edge would change degree, reachability, relation matching,
  shortest path, or graph-assisted rank between two visible vertices
- **THEN** the visible graph and ordered result are identical to the graph with that
  vertex or edge absent

#### Scenario: Hidden malformed item produces no error oracle

- **WHEN** an L0 candidate is malformed, stale, duplicated, or fails a sidecar parse
- **THEN** the success/error envelope, diagnostics, counts, and remediation are identical
  to the same request with the candidate absent

#### Scenario: Governed timings do not reveal corpus size

- **WHEN** the actual-wire paired gate interleaves a governed non-owner request first with
  zero/one/exact-maximum-capacity L0 artifacts and then with them physically absent
- **THEN** payload timing shapes are identical and both 99% upper bounds stay within the
  checked manifest value, 25 ms, and 10% of absent p95

#### Scenario: Release cannot self-waive timing closure

- **WHEN** a manifest raises the normative differential, reduces capacity or a lane,
  adapts padding after observation, or maximum capacity misses the fixed deadline
- **THEN** the gate refuses the release and only a reviewed specification change may
  alter the repository-owned limits

#### Scenario: Cache stays principal-free

- **WHEN** two different principals run the same query and the second reuses retrieval
  measurements from the hot cache
- **THEN** the second response carries its own decisions and reductions and no cached
  candidate copy carries the first principal's decision, purpose, session, or projection

#### Scenario: Namespace activation is complete or unavailable

- **WHEN** the policy fingerprint, projector schema version, or catalog generation changes
  while its exact namespace tuple is rebuilding, or a required lexical row is missing
- **THEN** the old namespace is not relabeled, raw indexes are not used as governed
  fallback, and non-owner retrieval receives the same content-free warming/unavailable
  envelope until the complete generation activates

#### Scenario: Policy and content races cannot make a stale namespace active

- **WHEN** policy publication races create/edit/delete or companion publication from one
  expected active tuple
- **THEN** only one complete tuple CAS wins, the loser rebuilds/reviews against it, and a
  reader never joins the winner's policy to the loser's catalog/index generation

#### Scenario: Out-of-band content cannot reuse a stale projection

- **WHEN** a file or companion's held identity/hash differs from the active catalog row
  before its watcher publishes a new complete tuple
- **THEN** direct and retrieval routes return the registered content-free stale/warming
  outcome and never emit the changed bytes through the prior raw/projected lane

#### Scenario: Canonical envelope has an exact transport boundary

- **WHEN** hidden-present and absent requests use different outer JSON-RPC ids, HTTP dates,
  or trace headers but the same application input
- **THEN** one registered transport normalizer removes only those enumerated outer fields
  and the entire remaining governed envelope is byte-identical

#### Scenario: Explicit notices remain deliberate projections

- **WHEN** a policy permits an item at L1 or above
- **THEN** that projected item may participate in public count and order using only its
  authorized fields, while an L0 item remains counterfactually absent

#### Scenario: Withholding does not change the visible count

- **WHEN** a query would return N permitted items and some candidates are withheld
  while the over-fetch pool can still fill N
- **THEN** exactly N items are returned and the count does not reveal that any
  were withheld

### Requirement: Terminal secret scrubber at the shared dispatcher

An always-on deterministic secret and authorization-bearer scrubber SHALL run at the
dispatcher shared by MCP, REST, Hosted, CLI, and retrieve/inject hooks over the final
result of every command. It SHALL cover successful content, raised/returned errors,
governance inspection and authoring, authorization-session lifecycle, receipts and
mutation previews, diagnostics, warming/unavailable responses, and every other control
envelope before serialization. It SHALL run when no governance policy exists and SHALL
be non-disableable whenever governance is active. No rule option, scope, grant, purpose,
surface, owner status, error handler, or control route MAY bypass, weaken, or disable it.

The closed governance option registry SHALL NOT contain `credential_scrubber`. Every
authored occurrence or legacy spelling/value, including `false`/`off` and `true`/`on`,
SHALL be a compile ERROR that leaves the immutable active tuple unchanged, blocks
ordinary content serving while the enrolled workspace is invalid, and requires a
reviewed owner migration removing the field. It MUST NOT be normalized into permission
to emit a secret. The scrubber MAY structurally allowlist registered identifier fields
such as content hashes, refs, and policy fingerprints only when their values do not
match an authorization bearer or registered secret form.

The authorization-bearer matcher SHALL be generated from the canonical bounded bearer
parser: exact 70-byte ASCII `as1.` plus 22-character canonical unpadded base64url locator,
`.` plus 43-character canonical unpadded base64url 32-byte secret. Candidate scanning
SHALL call the same decode/re-encode validation and MUST NOT use an independently edited
regex, alphabet, padding rule, or length. Thus every credential the parser accepts is
also scrubbed wherever it is not the exact typed issuance exception.

The only bearer exception SHALL be the exact `issued_credential.bearer` occurrence in a
successful authorization-session `open` or `rotate` response after route-variant/schema
validation and equality with the just-minted non-serializable request-context value.
That exception SHALL remain non-disableable and non-extensible; any duplicate, wrong
field, malformed response, error, receipt, log copy, or other envelope SHALL be scrubbed
and malformed issuance SHALL refuse.

#### Scenario: Credential blocked on an ungoverned vault

- **WHEN** any content or control result contains a private key, API-key-shaped token, or
  authorization-session bearer and the protected registry proves never-enrolled state
- **THEN** the value does not cross the boundary and a content-safe notice reports the
  block

#### Scenario: Structural identifiers are not false positives

- **WHEN** a result contains registered content hashes, fingerprints, and `exomem://`
  refs that are not bearer or secret forms
- **THEN** the scrubber leaves them intact

#### Scenario: Every surface and envelope family is covered

- **WHEN** the same secret-shaped value appears through MCP, REST, Hosted, CLI, or the
  retrieve/inject hook in content, error, governance, session, receipt, diagnostic,
  warming, or other control output
- **THEN** every copy is scrubbed at the shared terminal boundary

#### Scenario: Active policy cannot disable scrubbing

- **WHEN** one or multiple scopes/items attempt to author any `credential_scrubber`
  option, including a legacy off value
- **THEN** compilation fails with an owner migration finding, no new generation
  activates, enrolled content serving fails closed until repair, and all emitted
  error/control envelopes remain scrubbed

#### Scenario: Mixed decisions cannot weaken the terminal boundary

- **WHEN** an output aggregates items with different scopes, purposes, grants, levels,
  or option conflicts and one contains a secret
- **THEN** the final scrubber removes the secret independently of every per-item decision
  and aggregation order

#### Scenario: Issuance exception remains exact

- **WHEN** successful open/rotate carries its just-issued value once in the typed field
  and the same bearer also appears in another field or envelope
- **THEN** only the exact typed occurrence may cross; every other copy is scrubbed and an
  invalid issuance shape refuses

#### Scenario: L6 raw content remains exact only when scrub-safe

- **WHEN** an L6 governed or registry-proven never-enrolled raw read contains no
  registered secret or canonical authorization bearer
- **THEN** `content` is byte-for-byte the immutable file snapshot and `content_hash` is
  the hash of those exact raw bytes

#### Scenario: L6 secret hit refuses raw without changing hash semantics

- **WHEN** the same L6 raw snapshot contains a registered secret or canonical bearer
- **THEN** the terminal boundary omits `content` and returns deterministic content-free
  `SECRET_BLOCKED`; it does not label a redacted rendering as raw
- **AND** any otherwise-authorized `content_hash` and every stale-edit comparison remain
  computed over the complete unmodified raw file bytes

#### Scenario: Every surface is covered

- **WHEN** the same restricted query is issued over MCP, REST, and CLI (including
  the retrieve-inject hook path)
- **THEN** all three responses carry field-identical projections with no
  sub-notice paths or excerpts

### Requirement: Empty-policy fast path

The release plane MAY short-circuit to OPEN only when a fresh authenticated protected
external cell-registry record proves `governance_enrolled=false`, carries a null expected
activation tuple, and agrees with absence of `_Governance` plus every registered
activation-store artifact. Absence of `_Governance`, `.governance.sqlite`, an in-process
policy object, or a compiled generation by itself SHALL NOT prove never-governed state.
Missing, stale, corrupt, unreachable, or contradictory external state SHALL return the
content-safe BLOCKED floor.

For a registry-proven never-enrolled vault, ordinary user-content behavior and latency
SHALL remain baseline except for two permanent structural boundaries: the always-on
terminal secret scrubber, and exclusion of the closed internal-state/reserved-path
registry from ordinary enumeration, retrieval, export, dataset/media, transfer,
recovery, and mutation. The obsolete claim that scrubbing is the *only* behavioral
difference SHALL NOT survive. The fast path SHALL not open or allocate the governance
activation sidecar.

Once `governance_enrolled` becomes true it SHALL never become false. A reviewed empty
policy remains an enrolled active policy/projector/catalog tuple; deletion/corruption of
workspace/store/generation/tuple or registry mismatch fails closed and MUST NOT recreate
OPEN.

#### Scenario: Proven never-enrolled recall is baseline

- **WHEN** the external registry proves never-enrolled state and a query targets ordinary
  user content with no registered internal path
- **THEN** results match baseline except for terminal secret scrubbing, and the existing
  ungoverned latency budget remains satisfied without opening the activation store

#### Scenario: Internal state is reserved even before enrollment

- **WHEN** a never-enrolled caller lists, searches, reads, downloads, exports, queries as
  dataset/media, transfers, recovers, or mutates a registered internal-state name/alias
- **THEN** reads treat it as structurally absent and generic writes refuse before
  existence or membership; owner/L6 cannot turn it into ordinary content

#### Scenario: Enrolled deletion is blocked rather than empty

- **WHEN** an enrolled vault loses its workspace, activation store, active tuple, named
  generation/catalog, projection namespace, or trusted registry parity while stopped or
  running
- **THEN** warm and restart requests both receive the BLOCKED floor and no cached OPEN or
  last-good policy is served

#### Scenario: Ungoverned recall is baseline

- **WHEN** a query runs on a vault with no `_Governance/` directory
- **THEN** results match baseline except that credential-shaped strings are
  blocked, and the latency gate is unchanged

### Requirement: Canonical audience resolution, threaded and fail-closed

Every content-returning read SHALL resolve a canonical principal at its surface boundary
— MCP OAuth principal, REST key/identity scope, hosted gateway principal, or explicit
local owner for stdio/CLI — normalized into one comparable audience space, and SHALL
thread it to the release decision. A standing policy grant authored against one surface
SHALL match the same canonical principal on another surface.

Session-scoped authority SHALL additionally require a verified server-issued
authorization-session capability bound to that canonical principal, its trusted issuer/
surface family, and expiry. Canonical-principal equivalence alone MUST NOT move an
ephemeral grant, purpose, token, or revocation between issuer families or authorization
sessions. When identity or required session capability should resolve but cannot, the
release decision SHALL fail closed to the most restrictive outcome, never to full
disclosure or an implicit owner.

For every session-aware content route—find/search/ask, get/fetch/read, browse/list,
graph/link suggestions, review/audit/provenance, Records/Planning, dataset, media/frame,
retrieve/inject hooks, and content-bearing writer results—the protected credential SHALL
be optional with absent meaning standing-only, present-valid installing the session, and
present-invalid rejecting the complete request. Extraction/redaction, trusted principal
resolution, and capability verification SHALL happen before ordinary validation, cache
key/lookup, idempotency lookup, membership/decision, or content/state work. No route may
silently ignore an invalid credential or reuse a standing-only cached decision for a
verified session.

#### Scenario: Same principal across surfaces keeps standing authority

- **WHEN** the same human queries through two surfaces that normalize to one canonical
  principal
- **THEN** standing rules and standing grants authored for that principal apply on both

#### Scenario: Session authority also requires its issuer binding

- **WHEN** the same canonical principal has a session grant in issuer family A and calls
  through issuer family B without opening a B session
- **THEN** the standing policy still applies but A's session grant, purpose, and tokens do
  not

#### Scenario: Verified session resumes across transport reconnect

- **WHEN** the caller presents the valid capability after reconnect or replica routing
  within the bound issuer family
- **THEN** the same internal session state participates without using process-local
  transport identity

#### Scenario: Unresolved identity denies

- **WHEN** an authenticated surface cannot resolve the expected principal
- **THEN** the decision is most-restrictive, not OPEN

#### Scenario: Unresolved required session does not fall back

- **WHEN** a request attempts session-scoped grant or purpose authority without a valid
  principal/issuer/session binding
- **THEN** that authority is absent and the operation fails closed rather than treating
  the caller as owner

#### Scenario: Invalid optional credential does not degrade to standing policy

- **WHEN** a caller presents an invalid credential to any session-aware content route
  that could otherwise return standing-policy content
- **THEN** the whole request receives the common credential refusal before validation,
  cache, idempotency, membership, decision, or content work

#### Scenario: Same principal across surfaces

- **WHEN** the same human queries via MCP and via REST
- **THEN** a grant authored for that principal applies on both

### Requirement: Error Payloads Cross The Same Terminal Boundary

The terminal filter at the shared dispatcher is the last thing between a command result
and the wire, and an error is a result. Every payload leaving a content-returning command
at the shared dispatcher SHALL pass the same terminal filtering as a successful return
value, whether the command returned or raised. An error raised inside a governed command
MUST NOT reach a caller carrying a vault path, title, reference, count, or diagnostic
derived from an item the caller is not permitted to see.

Errors that name colliding or ambiguous stored items SHALL carry a content-free code and
only ambiguity information computed from caller-visible projected candidates. An L0
candidate SHALL be absent before ambiguity is decided; adding or removing it MUST NOT
change code, text, count, remediation, or timing shape. The identifying detail an owner
needs to repair a collision SHALL be reachable only through an owner-authorized surface
that applies a disclosure decision.

#### Scenario: An identity collision does not name hidden pages

- **WHEN** a caller-visible identifier matches one released item and any number of L0
  stored items
- **THEN** resolution is identical to the vault containing only the released item and no
  ambiguity count or identity from an L0 item crosses the boundary

#### Scenario: Visible ambiguity remains content-free

- **WHEN** an identifier matches more than one caller-visible projected item
- **THEN** the error carries the stable ambiguity code and a count derived only from
  those visible candidates
- **AND** the error carries no vault path, title, or reference beyond the permitted
  projection

#### Scenario: A raised error is filtered like a returned result

- **WHEN** a governed content-returning command raises an error whose payload names a
  vault item withheld from the caller
- **THEN** the reference and every derived count are removed before the error crosses the
  dispatcher boundary
- **AND** the caller cannot distinguish the withheld item from one that does not exist

#### Scenario: An ungoverned vault keeps its error text

- **WHEN** the protected external registry proves the vault was never governance-enrolled
  and a command raises
- **THEN** the error text is unchanged apart from the always-on secret scrubbing that
  already applies

#### Scenario: an identity collision does not name the colliding pages

- **WHEN** a caller resolves an identifier that matches more than one stored item
- **THEN** the error carries the ambiguity code and the number of matches
- **AND** the error carries no vault path, title, or reference of any match

#### Scenario: a raised error is filtered like a returned result

- **WHEN** a governed content-returning command raises an error whose payload names a vault
  item withheld from the caller
- **THEN** the reference is removed before the error crosses the dispatcher boundary
- **AND** the caller cannot distinguish the withheld item from one that does not exist

#### Scenario: an ungoverned vault keeps its error text

- **WHEN** a vault has no governance configured and a command raises
- **THEN** the error text is unchanged apart from the always-on secret scrubbing that
  already applies

### Requirement: Reverse Provenance Is Stripped Below Full Release

Provenance runs in both directions: a compiled item records what it cites, and a cited
item records what compiled from it. Both directions name items that may be withheld. The
frontmatter provenance fields stripped below full release SHALL include the reverse
citation field that records which compiled items ingested a source, alongside forward
citation, evidence, supersession, parent-media, history, link, and relation fields.

This requirement applies to every representation of a page, including opt-in raw bytes,
frontmatter-only reads, parsed payloads, history, and links. Exact stored bytes and exact
provenance SHALL be available only at L6. At L1-L5, `include_raw=true` SHALL NOT create a
second unprojected representation; at L0, the entire read remains identical to missing.

#### Scenario: A released source does not enumerate the compiled items that cited it

- **WHEN** a source item is released to an audience below full level and a compiled item
  withheld from that audience cites it
- **THEN** the reverse citation field is absent from every released representation
- **AND** the withheld compiled item's path, title and reference do not appear anywhere
  in the response

#### Scenario: Full release to a permitted audience is unchanged

- **WHEN** the same source item is released at full level to a permitted audience
- **THEN** the reverse citation field is present and complete

#### Scenario: Raw opt-in cannot restore provenance below full

- **WHEN** a caller below L6 requests the raw stored bytes of a page alongside its
  projection
- **THEN** no raw `content` field or exact provenance is returned and the response is the
  same level projection as if raw had not been requested

#### Scenario: a released source does not enumerate the compiled items that cited it

- **WHEN** a source item is released to an audience below full level and a compiled item
  withheld from that audience cites it
- **THEN** the reverse citation field is absent from the released representation
- **AND** the withheld compiled item's path, title and reference do not appear anywhere in
  the response

#### Scenario: full release to a permitted audience is unchanged

- **WHEN** the same source item is released at full level to a permitted audience
- **THEN** the reverse citation field is present and complete

#### Scenario: the guarantee is stated over the projected representation

- **WHEN** a caller requests the raw stored bytes of a page alongside its projection
- **THEN** the projected representation still omits the reverse citation field
- **AND** the raw-bytes surface is out of scope for this requirement and is governed
  separately

### Requirement: Operational Run State Is Not Released As Knowledge

Durable run state and administration state for governed multi-step operations record
paths, targets, content hashes, policy bytes, receipts, and authorization identities. It
is operational state, not knowledge, and it SHALL NOT be indexed into the content corpus
or surfaced by ordinary recall, get/fetch, browse/list, dataset, media, graph, export,
transfer, or recovery operations. `_Governance` SHALL remain reachable only through the
bounded `govern_memory` lifecycle. `_Consolidation` SHALL remain reachable only through
its owning consolidation command; until that command exists it SHALL have no public
reader. The closed internal-state registry—including governance/session DB and journal
family, raw lexical/vector/CLIP/reference/graph indexes, projected namespaces, catalog
descriptors, locks, temps, WAL/SHM, and retained/published physical identities—SHALL be
structurally absent from every ordinary Exomem operation. This is not a claim that direct
filesystem or block access as the OS vault owner cannot disclose, corrupt, move, or delete
state; that access is owner-equivalent and outside the command boundary. Owner or L6
status SHALL NOT turn a reserved tree or internal-state file into ordinary knowledge or
non-Markdown membership.

Text released from any surface MUST NOT embed a machine-readable enumeration of item
paths, targets, content hashes, policy documents, session capabilities, or per-item run
state. Where a run records a summary in released text, that summary SHALL carry counts
and the run reference only, with per-item detail confined to the run object behind the
governed owning command.

#### Scenario: Run and administration state do not appear in recall

- **WHEN** a run or administration tree has state naming vault items and a caller issues
  a recall query whose terms match that state
- **THEN** no operational-state item is returned
- **AND** result counts and order are the same as for a vault with that state absent

#### Scenario: Reserved trees have no ordinary projection

- **WHEN** any ordinary Exomem content, dataset, media, graph, export, transfer, list,
  or direct-read command targets `_Governance`, `_Consolidation`, a registered
  internal-state family, or a retained/published physical alias resolving into one
- **THEN** the tree is absent from the public corpus and no path, bytes, metadata, count,
  or existence signal is returned through that command boundary

#### Scenario: A released run summary carries no per-item detail

- **WHEN** a run records a summary into a released page
- **THEN** the summary carries counts and the run reference
- **AND** it carries no source path, target path, content hash, or session capability of
  any individual item

#### Scenario: The owner still reads full run detail through the governed command

- **WHEN** the owner requests the status of a run through the command that owns it
- **THEN** the full per-item detail is returned without exposing an authorization bearer
- **AND** the response is subject to the same disclosure decision as any other governed
  content-returning result

#### Scenario: run state does not appear in recall

- **WHEN** a run has recorded state naming items in the vault and a caller issues a recall
  query whose terms match that state
- **THEN** no run-state item is returned
- **AND** the result counts are the same as for a vault with no run present

#### Scenario: a released run summary carries no per-item detail

- **WHEN** a run records a summary into a released page
- **THEN** the summary carries counts and the run reference
- **AND** it carries no source path, target path, or content hash of any individual item

#### Scenario: the owner still reads full run detail through the governed command

- **WHEN** the owner requests the status of a run through the command that owns it
- **THEN** the full per-item detail is returned
- **AND** the response is subject to the same disclosure decision as any other governed
  content-returning result
