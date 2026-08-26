## Context

Exomem already has a compiled governance policy, a disclosure ladder, receipt-first
policy authoring, explicit authorization-session fields, canonical principals, and a
terminal release boundary. Those pieces are sufficient while unrelated confidential
compartments remain in separate vaults, but several composition seams are not strong
enough for a single item to belong to more than one compartment:

- the evaluator computes one item-wide standing floor and one item-wide grant maximum,
  so a grant touching one scope can lift a different default-deny scope;
- `get(include_raw=true)` bypasses the per-level projection that governs its parsed
  representation;
- generic file, dataset, media, alias, and recovery routes can address administration
  trees even though `govern_memory` is supposed to own policy mutation;
- mutable policy documents are both authoring input and live enforcement input, so a
  reviewed compile has no immutable generation or atomic activation point;
- an authorization-session handle can be compared at the leaf without first being
  established from a trusted surface principal;
- post-retrieval filtering can leave hidden items visible through displacement in
  rank, top-k, pagination, graph structure, counts, errors, and timing diagnostics; and
- non-Markdown membership treats a missing semantic companion as an empty membership,
  which can open a semantic-only compartment.

The Wave 0 egress fixes and Wave 1 default-deny scope behavior have already been synced
to the canonical specs and archived. This change builds on that post-archive contract.
It is a security prerequisite for vault consolidation, not the consolidation workflow
itself.

The implementation must preserve the pure-substrate boundary: all policy compilation,
membership classification, projection, ranking, and token verification remain
deterministic. It adds no reasoning model or heavy optional dependency. A vault remains
baseline-open only while its protected external cell-registry record proves it has never
been governance-enrolled and the vault has no governance workspace or activation-store
artifact. Even then, the terminal secret scrubber remains active and reserved
administration trees and private authority-store names are structurally excluded from
ordinary operations.

## Goals / Non-Goals

**Goals:**

- Make the disclosure lattice compartment-safe when an item belongs to overlapping
  scopes.
- Give exact stored bytes, hashes, and provenance only to an L6 decision.
- Establish one closed normalized, alias-safe administration/internal-state decision
  used by every public operation and every generated surface.
- Bind every meaningful prospective compile to a stable authoring snapshot and publish
  policy, projector, and catalog only through one atomic active tuple.
- Establish durable authorization sessions from trusted principals across MCP, REST,
  Hosted, and CLI without using caller-supplied identity.
- Make the public result a function of the caller-visible projected corpus, including
  order, count, graph, error, and timing fields.
- Distinguish an unclassified non-Markdown artifact from a proven member of no scope,
  and fail closed on the former.
- Land red-first proof, independent security review, and independent end-to-end
  verification before canonical sync or archive.

**Non-Goals:**

- Implement `consolidate_memory`, migrate separate vaults, or decide which existing
  pages belong to which future compartments.
- Introduce row-level secrecy inside a single canonical dataset or chronological log.
- Make a client assertion such as purpose, audience, session label, retrieved text, or
  natural-language instruction an authority source.
- Make policy YAML writable through generic file APIs, even for the owner.
- Build principal-specific persistent search indexes or put principal identity into the
  shared retrieval hot cache.
- Hide the deliberate existence notice that a policy explicitly permits at L1 or
  above; the counterfactual-absence guarantee applies to L0 items, while higher levels
  participate only through their permitted projection.

## Decisions

### 1. Evaluate the disclosure lattice independently for each matched scope

For each member scope `s`, the evaluator will select only standing rules, grants, and
organisation caps whose `scope_ids` explicitly contain `s` and compute:

```text
standing_s = min(matching standing ceilings, default_s)
default_s  = L0 when s.default_deny and caller is not owner, otherwise L6
granted_s  = max(standing_s, matching standing/session grant ceilings)
scope_s    = min(granted_s, matching organisation caps, default L6)
item       = min(scope_s for every member scope, default L6)
```

Purpose evaluation retains the existing monotonic rule, but numeric `min` is not enough.
The evaluator computes the complete declared and undeclared decisions, projects both to
their lower numeric level, and combines them with the same conservative decision meet
used across scopes. The result is no more permissive than either branch in level *or in
any emitted field*. Equal numeric ceilings therefore cannot select the declared branch's
notice, constraint, abstract, bridge, release identity, or other option. The owner
remains exempt only from a scope's default-deny default; an authored rule or
organisation cap can still restrict the owner.

A standing grant already names scopes. A session grant will persist the exact scope IDs
from the reviewed membership manifest and policy fingerprint. At decision time it may
raise only those scopes, paths, and fingerprints. A legacy session-grant row without an
exact scope binding is invalidated and requires fresh authorization; interpreting it as
"all matched scopes" would reproduce the vulnerability.

The explanation records each scope's standing value, grant contribution, organisation
cap, and final value before the item minimum. This makes overlap denials inspectable by
the owner without fabricating a global rule order.

Options are composed on the same per-scope lattice; they are not merged afterward by
sorted rule ID. Compilation uses a closed, versioned registry rather than accepting an
arbitrary YAML mapping. The initial authored rule registry is exactly:

| key | canonical type | composition |
| --- | --- | --- |
| `notice` | one bounded provenance-free string | retain only one identical value |
| `constraint` | one bounded provenance-free string | retain only one identical value |
| `abstract` | one bounded provenance-free string | retain only one identical value |
| `bridge` | one bounded opaque bridge id | retain only one identical value |
| `suspended` | boolean control | applied before matching; never copied into a decision projection |

The release-grant registry is exactly `strip_provenance: list[canonical ref-or-path]`,
whose meet is normalized set union. `constraint_source` and
`constraint_ambiguous` are compiler-owned derived fields and cannot be authored. There
is no registered numeric option in this version. Any other key, YAML tag/container,
wrong type, oversized string, non-canonical ref/path, or non-finite value is an ERROR
that refuses compilation rather than being preserved in `Rule.options`.
`credential_scrubber` is deliberately not registered: every authored spelling or value,
including legacy `false`/`off` and `true`/`on`, is a compile ERROR requiring the owner to
remove it through a reviewed migration. Policy cannot disable, scope, or weaken terminal
secret/bearer scrubbing under active governance.

For every registered key the compiler defines an associative, commutative, and
idempotent meet with a canonical identity/absence representation. Content-bearing
singletons survive only when every contributing tied branch supplies the same canonical
value; disagreement is an ambiguity, and absence in one branch cannot be filled by
another. Ambiguity lowers below the level that would emit it (for example, conflicting
L2 constraints lower to L1); conflicting L1 notices lower to L0. Restrictive strip sets
are unioned. Derived release identifiers/digests survive only when identical in both
operands. The fold covers tied rules inside a scope, the per-scope item minimum, and the
declared/undeclared purpose meet, and is invariant under every permutation and grouping.
A grant changes only a scope's ceiling and cannot carry or import options from a sibling
scope.

**Alternative considered:** retain the item-wide formula and suppress grants whenever
more than one scope matches. That fails safe but makes legitimate multi-compartment
authorization impossible and cannot explain which scope remains closed. Retaining the
existing rule-ID-ordered option update was also rejected because authoring an unrelated
rule could overwrite the effective constraint without changing the ceiling.

### 2. Represent non-Markdown membership as classified, excluded, or unresolved

Membership evaluation will return a typed outcome rather than overloading an empty set.
For each scope it first applies path/ref exclusions (proven excluded), then path/ref
positives (proven member, which is conservative even if an unavailable semantic
exclusion might have removed it), then semantic selectors from a valid companion. The
aggregate outcomes are:

- `CLASSIFIED(scope_ids)` means every applicable scope was decided from the artifact's
  normalized path/ref selectors or a valid semantic companion;
- a matching path/ref exclusion proves that scope excluded without semantic metadata;
  and
- `UNRESOLVED(reason)` means at least one still-undecided scope that can select by
  project, tag, type, or class could not be decided because the companion Markdown file
  was missing, malformed, unreadable, stale relative to the artifact, or escaped the
  vault.

In particular, a missing companion is not the same as an empty companion. If a
non-Markdown artifact has no positive path/ref match and any non-excluded semantic
scope could still contain it, release evaluation floors it to L0 and structured reads
behave as missing. A policy containing only path/ref selectors remains classifiable
without a companion, so an unmatched artifact keeps the existing open default. A
positive path/ref selector may prove membership, but it does not prove that unknown
semantic-only sibling scopes do not also apply; unresolved sibling membership therefore
still floors the item.

The same outcome is used by direct media reads, frame reads, dataset reads, corpus
walks, session-grant membership drift checks, proposal previews, and consolidation
preflight. No caller may convert `UNRESOLVED` to an empty scope set.

Companions use one closed locator/binding registry so a convenient adjacent Markdown
file cannot classify unrelated bytes:

| artifact class | canonical companion locator | mandatory binding tuple |
| --- | --- | --- |
| ordinary/media binary | the sibling `<artifact-leaf>.md` | class, normalized artifact path, SHA-256, byte size; media also binds media type and original filename |
| dataset | the unique canonical `type: dataset` page whose normalized `data_file` equals the artifact path | class, normalized data path, SHA-256, byte size, declared format |
| scene frame | the sibling `<frame-leaf>.md` inside the canonical `<parent-media>.frames/` directory | class, frame path/hash/size, parent path/hash, and bounded integer `frame_timestamp_ms` equal to filename/index |

The tuple lives in a versioned `governance_companion` mapping. It includes
`state: classified` and an exact `semantics` object whose only keys are canonical string
lists `projects`, `tags`, `types`, and `classes`; these values classify the artifact and
are distinct from fields describing the companion page itself. When all four lists are
deliberately empty, that explicit state plus a valid tuple is the only representation of
a valid explicitly empty companion. Missing semantic keys or an absent descriptor are
not an assertion of emptiness. The companion and artifact must be canonical regular files inside the vault,
must be read as immutable snapshots, and every tuple field must match those snapshots.
Zero or multiple dataset cards, a relocated/symlinked card, a changed artifact, a frame
whose parent/timestamp does not match, or any incomplete tuple is `UNRESOLVED` for every
still-undecided semantic scope.

Existing `preserve.py` media stubs and completed transcripts predate this tuple. They
remain usable under path/ref-only policy but are unresolved under an applicable semantic
selector until an explicit owner-reviewed, receipt-first
`govern_memory(operation="backfill_companion")` event supplies a version-1 input with
exactly `artifact_class`, canonical `artifact_path`, expected artifact SHA-256 and byte
size, expected companion path and SHA-256, the complete explicit `semantics` object, and
the class-specific binding fields from the registry. The trusted adapter must establish
the local owner; a model, companion page, artifact metadata, policy rule, dataset row, or
media job cannot authorize or populate that input. In particular, page-level
`projects`/`tags`/`type`/`classes` are never copied into artifact semantics. The preview
shows the exact descriptor and immutable byte identities; commit rechecks them under the
same no-follow mutation boundary, writes only the descriptor, and records proposal,
owner principal, prior/target hashes, and terminal outcome in the governance receipt.

For a scene frame the descriptor field is `frame_timestamp_ms`, an integer in
`[0, 4_294_967_295]`. New frame filename, CLIP measurement, and descriptor generation
derive it once as `int(round(binary64(frame_ts) * 1000))`, using IEEE-754/Python
round-to-nearest-ties-to-even semantics; the canonical filename is
`scene-<NNN>-t<frame_timestamp_ms>ms.jpg`. Legacy migration performs that same conversion
from the existing finite, non-negative `frame_ts`, parses the integer milliseconds from
the filename, and requires exact equality with the indexed timestamp and parent binding.
It refuses a negative/non-finite/out-of-range value, rounding mismatch, ambiguous frame
index, or filename disagreement rather than choosing one source. After migration,
membership consumes only `frame_timestamp_ms`; the legacy float is compatibility data,
not binding authority.

Backfill preserves transcript/body, page-level metadata, and media processing state; it
does not infer an empty classification. Pending and completed media, datasets, and scene
frames each have class-specific inputs and fixtures. Ambiguous dataset cards, missing
parents, stale jobs, or bytes that change between preview and commit refuse without
partial metadata. Repeating the exact committed event is idempotent; a different
descriptor requires a new review. Newly created companions always write the current
descriptor.

**Alternative considered:** require sidecars for all non-Markdown files. That would make
ordinary ungoverned media unusable and would be stricter than needed when path/ref-only
policy proves the classification.

### 3. Publish policy and catalog through one atomic active tuple

`_Governance/**.yaml` remains the canonical, reviewable authoring workspace and history,
but mutable workspace bytes are pending input, never live enforcement authority by
themselves. The existing private, no-follow `Knowledge Base/.governance.sqlite` sidecar
is upgraded from an inspection cache to the activation store. It contains immutable
`compiled_policy_generations` and immutable catalog-generation descriptors plus exactly
one `active_governance_tuple`. That singleton is the only active authority and contains
exactly `(policy_generation_id, policy_fingerprint, projector_schema_version,
catalog_generation)`; there is no independently active policy pointer, catalog pointer,
or projector selector.
An immutable row binds a generation ULID, its exact ordered source-document byte map,
source fingerprint and conflict digest, canonical compiled bytes, compiled-policy
fingerprint, compiler/projector schema versions, predecessor generation, authoring event
and receipt identities, and creation time. Inserts are append-only; neither an active
nor historical row is updated in place.

The activation store is reserved infrastructure, not a governed content artifact. Its
closed physical family is exactly `Knowledge Base/.governance.sqlite` plus SQLite's
same-directory `Knowledge Base/.governance.sqlite-wal`,
`Knowledge Base/.governance.sqlite-shm`, and
`Knowledge Base/.governance.sqlite-journal`. Those logical names are reserved whether
or not a file exists, and every retained or published physical identity of an existing
family member is reserved at protected acquisition.
Only the internal activation/session-store subsystem may open them, using a private
non-serializable authority and the same held-parent, no-follow primitives described in
Decision 4. No owner/L6 decision, non-Markdown classifier, dataset/media route, recovery
path, or generic filesystem command can treat one as ordinary knowledge.

Whether the OPEN singleton is legal is derived from the protected external cell
registry, never from absence of files alone. Every registered logical vault has an
authenticated record containing a monotonic `governance_enrolled` boolean and, once
enrolled, the expected activation tuple
`(activation_store_id, activation_epoch, activation_state_digest)`. The false value is
valid only with a null expected tuple; the transition `false -> true` is irreversible.
Enrollment is durably committed before `_Governance` or the activation store is created,
so a crash during first enrollment is fail-closed rather than accidentally OPEN.
`activation_store_id` is a random immutable logical store id persisted in SQLite
metadata, and `activation_epoch` increases on every active-tuple publication or
controlled restore. `activation_state_digest` is exactly
`SHA-256("exomem.activation-state.v1\\0" || JCS(value))`, where `value` contains only
`logical_vault_id`, `activation_store_id`, `activation_epoch`, policy generation id,
compiled-policy fingerprint, immutable policy-row digest, projector schema version,
catalog generation, immutable catalog-descriptor digest, and projection-namespace
identity.

The policy module separates a pure `compile_documents` primitive from live
`compile_prospective` acquisition. Recovery may call the pure primitive only with bytes
already pinned in an immutable generation, proposal, receipt, or journal. A user-visible
proposal or authoring preflight uses the live form, which acquires an
`AuthoringSnapshot` containing canonical workspace document bytes, their source
fingerprint, conflict-set digest, pending-authoring guard generation, and no-follow
regular-file identities. It probes every supported conflict-copy form before reading,
reads through held governance-root handles, and re-probes conflict, identity, and guard
state afterward. An unstable, conflicted, symlinked, aliased, or non-regular snapshot
refuses rather than producing a reviewable generation.

`ProspectiveCompile` binds that exact source snapshot, the complete current active tuple,
the immutable target compiled bytes/fingerprint, and the affected membership manifest.
Before commit, every projection variant and search lane for the target policy against
the tuple's exact catalog generation is built and verified as an immutable staged
namespace. Commit revalidates the expected tuple and affected manifest, then starts one
SQLite `BEGIN IMMEDIATE` transaction. In that transaction it inserts the complete
immutable target policy row and compare-and-swaps `active_governance_tuple` from the
reviewed predecessor to `(target policy generation/fingerprint, target projector
version, expected catalog generation)`; tuple activation, receipt state, and the ready
namespace reference commit together.

Content, deletion, media, and companion writers use the dual protocol. They stage one
immutable next catalog generation and every raw/projected index, graph, and measurement
needed for the currently active policy/projector, then in the same activation store
compare-and-swap the complete expected tuple to `(expected policy generation/
fingerprint, expected projector version, next catalog generation)`. A policy commit
therefore CASes the catalog it reviewed, and a content/companion commit CASes the policy
it reviewed. If they race, exactly one wins and the other restages/reviews; neither may
publish a separately current catalog, policy, projector, graph, or index. A reader
snapshots the tuple once and uses only its policy generation, immutable catalog, and
exact `(policy_fingerprint, projector_schema_version, catalog_generation)` projection
namespace for the whole request.
The catalog descriptor binds each artifact's immutable identity and content hash.
Direct reads and retrieval revalidate the held source snapshot against that row; an
out-of-band file/companion edit whose next catalog tuple has not committed is treated as
content-free stale/warming state and never joined to the old projection. The watcher
must publish it through the same content-writer protocol.

Suspend, resume, and undo are policy publications under this same protocol, not YAML
restoration shortcuts. Their reviewed target is one immutable policy generation plus the
exact before/after dependent-grant rows and membership manifest, the ready target
namespace for the expected catalog, and the predecessor/target tuple. Direction is
derived from the complete before/after lattice: proven narrowing may install only its
receipt-backed fail-closed overlay early; widening/unknown keeps the predecessor. The
tuple transaction atomically applies target dependent-grant state and policy generation.
A content/companion race makes one side stale. Undo's archived source bytes may be
mirrored to `_Governance` only under the cooperative writer fence with held-parent and
descriptor-identity checks; an observed drift refuses that mirror. Mirror failure cannot
change published authority, while an invalid resulting workspace still blocks serving.
Direct OS-owner mutation is outside the cooperative writer fence. Crash recovery
completes only the exact recorded tuple/registry acknowledgement and never replays
suspend/resume/undo semantics or re-resolves mutable YAML.

The successful tuple transaction is the authority-publication linearization point and
increments `activation_epoch`. It also stores the resulting activation-state digest.
Publication then updates the external registry's expected tuple through its authenticated
compare-and-swap. A reader snapshots the registry record and active tuple and serves only
when both exact values agree. The bounded interval after SQLite commit and before
registry acknowledgement is BLOCKED, never predecessor fallback or target-without-proof.
Recovery may complete that acknowledgement only from the committed immutable rows,
receipt, and exact digest; it cannot manufacture a different tuple. A crash before the
transaction exposes the predecessor; a crash after it exposes the complete target once
registry parity is restored. No reader can observe a partial policy/catalog combination
or infer authority from current workspace files or mutable index state.

Mirroring the reviewed target documents back into `_Governance` is a separate,
handle-relative operation under the cooperative writer fence. It retains held-parent and
descriptor identities from the proposal, refuses an observed byte or identity drift, and
records that outcome. It does not assert a portable final-component or file-level
filesystem guarantee against direct OS-owner mutation. A failed mirror
does not reinterpret the reviewed generation: observed divergent workspace bytes remain
pending input to a future proposal and owner-only diagnostics report source/active-
generation divergence. A successful mirror may happen before or after the tuple
transaction, but the receipt records both outcomes and recovery never treats mirror
completion as policy activation. Thus an uncoordinated workspace write need not abort
publication merely because it occurred before the tuple switch; it cannot become part of
the reviewed generation.

Source/generation parity is proved from the immutable source byte map in the generation,
not from mutable workspace state: recompile of those stored bytes must reproduce the
stored canonical compiled bytes and fingerprint. Startup verifies the sidecar is the
expected regular no-follow file, the active tuple names one complete immutable policy
row and catalog generation, every hash/schema version verifies, and its exact projection
namespace is ready. It also verifies the external record, immutable store id, epoch, and
activation digest before serving. Once `governance_enrolled` is true, an unavailable or
untrusted registry, missing or corrupt `_Governance` workspace, missing/aliased/corrupt
activation store, missing/corrupt policy generation/catalog/tuple, stale projection
namespace, or any registry/store tuple mismatch fails closed and cannot fall back to
workspace, a different historical generation/catalog, cached OPEN, or an in-process
last-good object. A syntactically valid external workspace edit may remain pending while
the verified tuple is served; a deleted, conflicted, symlinked, unreadable, or unparsable
workspace is not valid pending input and blocks content serving until owner repair.
Transaction recovery rolls back uncommitted rows; receipt recovery completes only the
already-committed tuple event and separately
retries a workspace mirror when the expected bytes still match. Backup/restore includes
the SQLite database, WAL-consistent active tuple/generations, referenced immutable
catalog/index generations, receipts, compiler version, and protected registry record.
Restore/rebuild uses an authenticated registry transition to a higher activation epoch
and may use only verified immutable policy/catalog snapshots plus the receipt identifying
the active tuple. Mutable `_Governance` or mutable index files alone cannot rebuild or
select active authority.

The never-governed fast path remains allocation-free only when a fresh authenticated
external-registry read proves `governance_enrolled=false`, its expected activation tuple
is null, and `_Governance` plus every governance activation-store family name are absent.
Other registered derived indexes may exist but remain structurally reserved. Missing,
stale, corrupt, or contradictory registry state is BLOCKED. Once enrollment begins, it
can never return to OPEN: a reviewed empty generation is still a governed active tuple,
and deletion is an incident that fails closed. Migration from the current direct-source model quiesces every old process
under the cooperative whole-tree writer/schema fence, snapshots and rechecks one valid
conflict-free workspace, first commits irreversible registry enrollment, compiles and
stores its immutable generation plus current catalog descriptor, initializes the tuple
in one transaction, and records the exact external expected tuple before any new binary
serves traffic. An invalid or changing workspace blocks migration; an old direct-source
binary is fenced from the upgraded store.

**Alternative considered:** make mutable YAML filesystem state the activation cut. That
would require every external editor to cooperate with a whole-tree fence and still
conflates pending authoring input with runtime authority. The immutable policy/catalog
tuple gives readers one atomic authority while preserving later workspace edits for the
next review.

### 4. Centralize reserved administration path authority

One closed, versioned internal-state registry feeds the same pure classifier as the
reserved administration roots. Its initial logical set is `_Governance/**`,
`_Consolidation/**`, the exact `Knowledge Base/` leaves `.governance.sqlite`,
`.embeddings.sqlite`, `.clip.sqlite`, `.lexical.sqlite`, `.graph.sqlite`,
`.claims.sqlite`, `.references.sqlite`, `.refs.sqlite`, `.freshness.sqlite`,
`.deferred-index.sqlite`, `.deferred_index.sqlite`, `.media-jobs.sqlite`,
`.media_jobs.sqlite`, `.idempotency.sqlite`, `.idempotency.json`,
`.idempotency.jsonl`, `.media-jobs.json`, `.deferred-index.json`,
`.voice_profiles.json`, `.media-worker.lock`, `.graph-sync.json`,
`.graph-sync-floor.json`, `.graph-commit-receipts/**`, and `.review-state.json`; the
current review-state writer temp form
`..review-state.json.[a-z0-9_]{8}.tmp`; the current lexical rebuild form
`.lexical.sqlite.rebuild-[0-9a-f]{32}.tmp` and its exact `-wal`, `-shm`, and `-journal`
siblings; and the grouped lexical quarantine forms
`.lexical.sqlite.quarantine-[0-9a-f]{32}`,
`.lexical.sqlite-wal.quarantine-[0-9a-f]{32}`, and
`.lexical.sqlite-shm.quarantine-[0-9a-f]{32}`. The set also contains the reserved
`Knowledge Base/.authorization-projections/**` namespace. Each ordinary SQLite descriptor also
reserves its exact `-wal`, `-shm`, and `-journal` siblings; the graph descriptor reserves
the exact bounded lower-hex rebuild-temp form already used by the graph builder and its
SQLite siblings. These include the existing raw lexical/vector/CLIP/graph/reference
indexes and the new immutable projected-index/catalog generations. A legacy spelling
remains reserved even after migration. A new internal store, temp form, journal, lock,
or index lane cannot start until its descriptor and registry-total tests land.
Both `/**` descriptors reserve the named directory itself and every descendant, not only
currently recognized receipt or projection leaf formats.

Reserved-path enforcement is a boundary on Exomem commands and cooperating Exomem
subsystems. Untrusted principals reach vault state only through those commands. Direct
filesystem or block-device access as the OS vault owner is owner-equivalent and outside
the zero-effect and universal-detection claims: it can disclose, corrupt, move, or delete
state. The command boundary fails closed only when drift is observable against retained
logical, catalogue, registry, or filesystem-identity anchors; it makes no claim to detect
or undo an unobservable out-of-band owner action.

This initial registry is derived and audited from every current private-state owner and
path factory—including governance, lexical/vector/CLIP/graph/reference/claims, graph
handoff/receipts, review state, deferred/media/idempotency, voice, and projection
builders—not copied from the hosted-portability classification. Portability/export
classification is one downstream consumer and cannot define or narrow the security
registry. Owner-inventory coverage enumerates each primary, transactional sibling,
temporary, quarantine, receipt directory, and physical identity form; an owner/path
factory without exactly one descriptor fails startup/tests before it can create state.

The classifier identifies every registry entry and retained or published physical alias
after all of the following:

- strip the configured knowledge-base prefix and normalize separators, including
  backslashes;
- apply Unicode NFKC normalization and case folding to every component;
- reject dot segments, absolute/drive/UNC spellings, alternate data streams, and
  platform aliases that cannot be proven canonical;
- resolve canonical references, managed aliases, Windows short-name aliases, trash
  metadata destinations, and both source and destination arguments; and
- inspect the resolved filesystem target without following a symlink outside the vault,
  guarding both the logical spelling and the physical target so an alias or symlink into
  a reserved tree is still reserved.

At protected acquisition, a stable pre-existing symlink, reparse point, hard link, or
physical alias to a reserved family member is refused. Reserved-tree writers reject
multiply linked or otherwise ambiguous files, and the secure target check compares stable
device/file identity where the platform exposes it rather than assuming `realpath`
detects every alias. Every owning subsystem retains and publishes stable identities for
currently open primary, WAL, SHM, journal, lock, temp, and immutable-index files under
the same cooperative coordination primitive used by generic leaves. SQLite primary/WAL/
SHM identities are published before that coordination is released, not before filesystem
reachability. A generic operation fails closed when its retained anchors expose a
create, checkpoint, rename, link, swap, deletion, or rebuild discrepancy; it does not
claim universal detection of direct owner-level races.

The check runs at the shared command dispatcher before existence checks, parsing,
candidate counting, mutation planning, or filesystem effects. Registry metadata names
every path/ref-bearing parameter and selector variant; startup coverage fails when a
new route is not classified. This logical preflight routes the request but is not the
leaf authorization: check-then-reopen by pathname remains unsafe.

Every filesystem leaf therefore executes inside a descriptor-bound, handle-relative
reserved-path transaction. It opens the vault root and each parent without following
links, holds those handles through the read or mutation, classifies stable volume/device
file IDs, and performs relative create/read/write/rename/link/unlink operations. POSIX
implementations use `openat2` with beneath/no-symlink/no-magic-link constraints or an
equivalent iterative dirfd/`openat`/`O_NOFOLLOW` design. Windows uses `NtCreateFile` with
`RootDirectory` and a relative name plus `NtSetInformationFile` rename/disposition
semantics, reparse-aware handles, and final volume/file identity. The Windows route is
enabled only after a runtime actual-filesystem capability probe proves those exact
handle-relative primitives, no-follow/reparse behaviour, and final identity checks;
otherwise it is disabled and returns the registered refusal without a fallback. A required
windows-latest CI gate runs on NTFS and covers junction, reparse, hard-link, 8.3, and
rename/disposition fixtures plus fallback-disable behaviour; that gate is a required
input to the combined release verification. Same-device rename, trash, and recovery hold
both parent handles and run under cooperative coordination. Cross-device move, trash,
and recovery are refused. A copy reads a held source and publishes a destination
atomically under the held destination parent; it is never source-and-destination atomic.
Recursive and multi-entry power-loss behaviour is a saga with recovery, not a cross-file
or recursive atomicity claim. Parent swaps, rename/link races, hard links, reparse
points, and bind aliases are revalidated at the kernel read/mutation operation against
retained anchors, not by a later `realpath`. A platform without an equivalent no-follow
handle-relative path disables the affected generic route.

`govern_memory` owns `_Governance` through its receipt-first lifecycle and receives an
internal, non-serializable authority token rather than a public bypass flag.
`consolidate_memory` will eventually own `_Consolidation`; until that command exists,
no public operation can read or mutate it. Each remaining internal-state descriptor is
owned only by its named store/index subsystem through a private non-serializable token;
even `govern_memory` gets no generic path bypass. Ordinary create, directory create, edit,
replace, append, move, delete, recover, list/browse, get/fetch, dataset, Records, media,
frame, search/walk, export, upload/download/transfer, audit/repair, and multiplexed alias
leaves must either hide or refuse a reserved target. Moves and recoveries check every
source, requested destination, metadata-derived original destination, and recursively
contained entry. No registered internal-state byte is eligible for non-Markdown L6,
dataset enumeration, an export manifest, a download stream, or raw/projected search
acquisition. Owning commands may expose bounded control-plane status, never a backing
file as ordinary knowledge.

**Alternative considered:** add checks to the currently known create/edit leaves. That
leaves recovery destinations, aliases, dataset/media readers, and future registry
routes as bypasses and cannot survive path spelling differences.

Delivery is deliberately split. PR A lands the primitives, capability probes, and
internal identity publication; PR B lands the closed registry, every public leaf, and
surface parity. No public security claim is made until both PRs land and their combined
verification, including the Windows gate, passes.

### 5. Project direct reads before honoring `include_raw`

The direct-read implementation will capture one immutable file snapshot, compute the
release decision against that snapshot, and pass every response variant through the
single level projector. `include_raw=true` authorizes a raw `content` field only when the
effective decision is L6. At L1-L5 it is behaviorally identical to
`include_raw=false`; at L0 it is byte-identical to missing. Exact content hashes,
unprojected frontmatter, history, links, and forward or reverse provenance are likewise
L6-only. Internal hashes remain available to stale-write guards without crossing the
release boundary.

L6 is exact with respect to governance projection, but it does not disable the
mandatory terminal secret scrubber. Before serialization, the shared parser scans the
candidate raw field. If no registered secret or canonical authorization bearer is
present, `content` is byte-for-byte the immutable file snapshot. If one is present, the
raw field is omitted and the route returns the deterministic content-free
`SECRET_BLOCKED` refusal; it never labels redacted bytes as raw. The L6 `content_hash`,
when otherwise part of the response contract, remains SHA-256 of the complete unmodified
raw snapshot, and stale-edit comparison always uses that same raw hash—not the refusal
or any scrubbed rendering. The identical rule applies to registry-proven never-enrolled
vaults because the terminal scrubber is always on.

Ungoverned content resolves to L6, so its current opt-in raw shape and stale-edit hash
round trip remain unchanged. Reserved administration paths are structurally excluded
before this projection and are not made readable by owner/L6 status.

Markdown has registered projections at L1-L6. Structured direct routes do not inherit
those projectors: `query_dataset` rows/aggregates/profile, Records values/reductions,
`read_media` bytes, and frame pixels are L6-or-missing unless that exact representation
later registers a typed, field-level projector with its own counterfactual tests. In
this change they return the ordinary missing envelope at L0-L5. A caller may discover a
lower-level textual description only by recalling/getting the bound Markdown companion,
which is projected as Markdown; a direct binary/dataset/frame request never silently
substitutes the companion or returns partial raw structure.

**Alternative considered:** reserialize the L5 projection into `content`. That is not
the requested exact raw representation, risks divergent frontmatter semantics, and
creates a second projector.

### 6. Compute public retrieval over the projected corpus

For a fixed caller, request, policy snapshot, and deterministic runtime configuration,
define the caller-visible corpus by replacing each artifact with its permitted
projection and deleting L0 artifacts and disallowed graph edges. Every public
computation must be observationally equivalent to running against that corpus.

Post-filtering a capped raw BM25/vector/CLIP result cannot meet that definition: it can
miss a query term that exists only in an authorized projection, calculate raw-corpus
IDF, or stop before the true visible top-k. Governed retrieval therefore uses an
authorization-projection namespace whose complete namespace key is exactly
`(policy_fingerprint, projector_schema_version, catalog_generation)`. Immutable content
identity is a row key inside that namespace; extractor and model versions are per-lane
measurement subkeys beneath a projection row and are never namespace components. The
persistent namespace holds principal-free canonical fixed projection variants and
measurements, not decisions or a caller's visible result. A request evaluates membership
and decision over the namespace's catalog snapshot and creates a request-local map that
selects exactly one variant id or L0 for each artifact. Principal, purpose, grant, and
authorization-session identity never enter persistent keys or shared hot-cache keys.

For each item the namespace builder enumerates the finite compiled decision domain: the
audience equivalence classes, declared-purpose classes including undeclared/other,
matched scope set, and reachable standing/session-grant levels L0-L6. It evaluates the
same conservative decision meet used at request time, materializes only unique reachable
non-L0 outputs, and deduplicates identical decision identities. It MUST NOT invent all
syntactic combinations of arbitrary principals or options. The fixed bound is
`MAX_PROJECTION_VARIANTS_PER_ITEM = 256` materialized non-L0 variants. This allows the
seven base disclosure levels plus 249 distinct policy/bridge/strip outcomes while
putting a hard limit on index fan-out for a personal knowledge base. L0 has no row and
does not consume the cap. If any item has more than 256 unique reachable outputs, policy
activation refuses with an owner-only overflow diagnostic; variants are never dropped,
merged permissively, or generated lazily from a query.

Each materialized row has
`projection_variant_id = SHA-256("exomem.authorization-projection.v1\\0" || JCS(value))`,
where RFC 8785 JCS `value` contains exactly the immutable item identity and content hash,
decision level, canonical closed-registry options, canonical release-strip set, canonical
bridge id plus the approved bridge dependency's content hash (or `null`), and projector
schema version. The fixed search projection selected by that identity is:

| level | canonical searchable representation |
| --- | --- |
| L0 | absent; no projection row |
| L1 | the canonical notice only |
| L2 | the canonical constraint only |
| L3 | the canonical abstract only |
| L4 | the bridge-approved abstraction only |
| L5 | the canonical `_excerpt_of` body after provenance and release-strip projection |
| L6 | all and only full permitted search fields after the same strip policy |

If the conservative decision lacks the content required by its level, it lowers/fails
closed before enumeration; indexing never fills that field from a sibling decision. L5
uses the existing fixed `_excerpt_of` transform: normalize body whitespace to single
U+0020 separators, trim, take at most the first 600 Unicode code points, retreat to the
last complete space-delimited token when truncated, and append the canonical ellipsis.
It is exactly `text = " ".join(body.split())`; return `text` when its length is at most
600 code points, otherwise take `prefix = text[:600]`, replace `prefix` by everything
before its last U+0020 when one exists, and return `prefix + U+0020 + U+2026`. It is
independent of the query, scorer, requested limit, and future text after that prefix.
Consequently an L5 find can match, embed, rank, rerank, and snippet only this
fixed excerpt; a term outside it does not acquire the item. Search snippets at every
level are slices of the already-selected fixed variant and never query-window projections
of hidden source. A vector measurement is keyed to the variant id and model version, so
L5 uses one fixed excerpt vector rather than a per-query or raw-body vector.

The search lanes consume that map directly:

1. lexical postings exist for every canonical projection variant; BM25 document
   frequency, IDF, score, and exact top-k are computed by intersecting postings with the
   authorization map, never from a capped raw candidate list;
2. vector embeddings are measured from authorized projected text and exact filtered
   vector top-k runs only over the selected projection rows; a missing/stale projected
   vector disables or warms that visible lane without falling back to a raw embedding;
3. reranking receives only selected projected text and runs before final visible top-k;
4. CLIP pixels/keyframes participate only for items selected at L6, with authorization
   filtering inside the CLIP lane before its cap. One immutable measurement row binds
   each media projection: an image has one untimestamped vector, while a video has one
   through forty strictly timestamp-ordered vectors using the canonical bounded
   `frame_timestamp_ms`. The fixed forty-sample ceiling is not configurable. Retrieval
   scores every authorized sample, emits the parent media item once at its best score,
   and carries the earliest best frame timestamp. At L1-L5 an image/video can match only
   through its authorized textual companion projection; if that record is unavailable,
   the binary CLIP lane is excluded rather than searched raw;
5. graph vertices and edges are projection-indexed and admitted before expansion, so
   degree, reachability, shortest path, relation matching, and graph-assisted fusion use
   the authorized graph; and
6. fusion, ordering, snippets, ranks, totals, facets, ambiguity, continuations,
   pagination, and error reduction operate only on complete lane outputs from this
   projected view. Public limits count projected candidates only.

Governed find pagination uses one opaque `pc1.<64-lowercase-hex>` continuation. The
token is the SHA-256 digest of NUL-terminated ASCII domain
`exomem.projected-find-continuation.v1\0` followed by RFC 8785 JCS containing only the
canonical principal id, verified authorization-session id or `null`, declared purpose
or `null`, the SHA-256 request digest, the next visible offset, and a
caller-visible snapshot digest. That snapshot digest is streamed over the ordered
`(item_identity, projection_variant_id)` pairs using the NUL-terminated ASCII domain
`exomem.projected-visible-snapshot.v1\0`, a big-endian u32 pair count, then for each UTF-8
field a big-endian u32 byte length followed by its bytes. It excludes
the vault root, hidden/L0 identities, catalog size/generation, issuance time, random
bytes, and every server-only authorization fact, so an L0 item present versus absent
produces the same continuation bytes. The token grants no authority and is useful only
as a key into a bounded process-local record created by the first page.

The request digest is SHA-256 over RFC 8785 JCS of the closed object
`{auto_rerank, graph, limit, mode, prefer_active, prefer_compiled, query, rerank,
scope}` with booleans, bounded integer `limit`, strings, and `rerank` boolean or `null`
encoded at their JSON types. No omitted default, presentation-only field, or server-only
ranking configuration enters that object.

That record retains the exact immutable runtime, authorization-map digest, selected-
projection digest, request and principal bindings, visible-snapshot digest, next offset,
the first page's repository-derived candidate depth, and a fixed repository-owned
15-minute monotonic expiry. At most 4,096 records may exist per process; expired records
are removed before admission and capacity exhaustion returns the same content-free
continuation refusal. A continuation request resolves the record for the exact vault,
requires the current policy fingerprint and projector schema to match, re-runs current
session/grant authorization against the retained namespace, compares the selected-
projection digest against the current namespace while excluding L0 rows, and requires
the resulting authorization-map and visible-snapshot digests to match before slicing the
next page.
Thus a policy/session/grant/revocation or visible-item change refuses rather than serving
stale authority or skipping rows, while a hidden-only catalog change continues over the
retained projected snapshot without changing page membership or revealing the change.
Unknown, malformed, expired, evicted, restart-lost, cross-vault, cross-principal,
cross-session, cross-purpose, or request-mismatched cursors all return the one bounded
`INVALID_CONTINUATION` application refusal under the fixed completion class. Replays are
read-only and deterministic; they do not consume or extend the record. A separately
issued first page may refresh a byte-identical token to the current verified runtime and
fixed expiry; a continuation replay may not replace that newer record with prior state.

Later pages reuse the first page's retained candidate depth exactly: offset growth never
widens a primary vector/BM25 prefix, changes graph-only classification, or recomputes a
different visible order. Exhausting that bounded ranked window omits the continuation.

Projection namespaces are built and validated before a governed compiled-policy
generation is activated. An item write creates the next catalog generation, reuses only
content-addressed unchanged rows, and publishes the complete catalog plus required
projection/index rows atomically; it never mutates the prior namespace in place. When
the active tuple requires CLIP measurements, the successor builder verifies that the
active image/video family is complete, carries only rows whose projection variant remains
content-identical, and requires exact target-item/content-hash-bound replacement samples
for changed visual media. Image rows remain one untimestamped sample; video rows remain
one through forty canonical timestamped samples. Derived frame companions bind
`parent_media` and are textual catalog artifacts, not duplicate CLIP measurement owners.
The complete successor CLIP family binds the target namespace and activates in the same
catalog publication transaction; missing, stale, mismatched, duplicate, or dimension-
incompatible rows refuse before canonical bytes change. The live media worker and bulk
backfill canonicalize each already-computed scene vector exactly once to bounded integer
milliseconds and pass that same immutable sample tuple into the planned frame-companion
publication. That transaction binds the samples to the guarded parent video sidecar and
advances the parent CLIP row, companion catalog rows, vector/CLIP roots, and active tuple
together; it neither invokes CLIP a second time nor gives a frame companion its own pixel
row.

When the active tuple requires a graph measurement family, the successor builder first
verifies exactly one active row for every active projection variant. The target family
likewise contains exactly one row per target variant: every variant below L6 has an empty
outgoing edge tuple, a content-identical L6 variant may carry its verified row, and a
changed L6 source item requires an immutable replacement bound to its exact target item
identity and content hash. Each replacement edge must name that source and a target
identity present in the target catalog; deleting a target that an otherwise-carried row
still names therefore refuses rather than relabeling stale graph state. Duplicate rows,
duplicate edges, mismatched sources, outside-catalog targets, missing replacements, and
capacity overflow all refuse before canonical bytes change. The complete successor graph
family binds the target namespace and publishes with the catalog, other measurement
roots, receipt, and active tuple. Live graph producers remain responsible for supplying
the target-bound replacements for every affected changed L6 source; an unknown required
measurement family remains blocked.

A producer may conservatively return a target-bound replacement for an affected item
whose target namespace has no L6 variant. The publisher still validates its item,
content, edge sources, edge targets, uniqueness, and aggregate capacity, then discards
that edge payload and emits only the required empty lower-variant rows. A lower-only
policy projection therefore cannot turn an otherwise valid semantic write into a graph
publication refusal or persist raw graph authority below L6.

The existing-page semantic writer, semantic creation writers, semantic move writer,
semantic trash-recovery writer, and semantic file/directory trash writers
derive their graph replacements from the freshest validated detached before-corpus
carried into the mutation boundary plus the exact guarded planned-write overlay. A move
or recovery starts from its exact detached after-corpus, while trash removes its exact
held Markdown identity set; each then overlays only its guarded
content and auxiliary writes. The retained-corpus paths do not walk the vault again;
trash builds one lazy detached before-corpus only after an active graph family is verified
and before canonical bytes change. None of these paths reopens the live graph. The overlay
re-resolves title-dependent links and reverse relations, so the replacement set includes
directly changed, created, moved, restored, or removed paths and otherwise-unchanged logical
sources whose outgoing edge tuple changes. The provider is invoked lazily only when the active tuple
contains a graph family; open and lexical-only writes do no graph-producer work. Other
live writer families remain blocked until they supply the same target-bound replacement
contract.

A policy
fingerprint or projector-schema change builds a new namespace tuple and never relabels
an old one. A model/extractor change writes or invalidates only the corresponding
versioned measurement subkey. Initial migration builds the exact
`(active_policy_fingerprint, projector_schema_version, catalog_generation)` namespace
from the complete catalog while non-owner governed retrieval stays unavailable behind
one content-free warming response. Variant-cap overflow, duplicate content identity,
variant-id mismatch, or incomplete required lexical rows blocks activation. Old
namespaces are garbage-collected only after no active request/cursor references their
exact tuple. Unguarded raw indexes remain eligible only for the never-governed/L6 fast
path, never as a governed fallback.

The strongest content regression oracle is a paired fixture: run the exact same
serialized request first with an L0 artifact/edge present, then with it physically
absent, and assert byte-identical canonical governed envelopes. That envelope is the
application status/code/message/remediation/data/content after projection, cursor
creation, error normalization, canonical serialization, and terminal secret scrubbing.
It includes ranks, order, top-k, counts, warnings, diagnostics, application timing
fields, and any application request/correlation id. The only excluded bytes are
transport framing produced outside the command envelope: an echoed JSON-RPC request id,
HTTP `Date`/outer trace headers, TLS/chunk/compression framing, and physical network
arrival time. Tests normalize those fields by one registered transport adapter rather
than deleting arbitrary response keys.

Suppressing serialized timing fields does not close a wall-clock channel. The repository
security contract owns these non-overridable bounds:

```text
MAX_HIDDEN_CORPUS_WIRE_DELTA_MS = 25
MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO = 0.10
MAX_GOVERNED_CATALOG_ITEMS = 16_384
MAX_GOVERNED_SEARCH_BYTES_PER_ITEM = 1_048_576
MAX_GOVERNED_GRAPH_EDGES = 262_144
```

The catalog/item/edge values define `capacity` for this gate: the present replica has
exactly 16,384 governed catalog identities, at least one item whose bounded searchable
source is exactly 1,048,576 bytes, and exactly 262,144 indexed graph edges. Larger raw
binary artifacts remain supported only through their separately bounded extracted text/
visual measurements; they do not enlarge query-time searchable item bytes. Raising any
capacity or timing constant requires a reviewed spec change, not a release-manifest,
environment, or operator override.

The threat contract covers an authenticated caller repeatedly measuring actual response
completion over a stable, warmed, quiescent deployment for a fixed public request shape.
Each release checks in a manifest that may lower but MUST NOT exceed the repository's
25 ms `hidden_corpus_wire_delta_ms`; it also fixes at least 200 samples per condition,
the hardware/runtime profile, and one repository-registered deadline/padding class before
sampling. Scheduler tolerance is reported but is neither subtracted from observations nor
added to a ceiling. The gate randomizes/interleaves hidden-present and physically-absent
replicas, covers zero, one, and the exact maximum supported capacity across lexical,
vector, rerank, CLIP, graph, error, and pagination paths, and computes 99% bootstrap
upper confidence bounds for absolute median and p95 completion-time differences.

Model execution is released as a closed profile, not one ambient "models enabled"
switch. A manifest, route set, completion class, exact device/backend/hard-off tuple, and
required measurement families belong to exactly one profile and cannot certify another.
The first live-model profile is `vectors-cpu-torch-v1`: text embeddings are enabled with
`EXOMEM_DEVICE=cpu` and `EXOMEM_EMBED_BACKEND=torch`, the embedding and legacy device
overrides are absent, `OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1`, and CLIP plus
reranking remain hard-off. It requires an exact
`projected-text-v1`/`BAAI/bge-base-en-v1.5` vector family before serving and uses the
repository class `projected-find-vector-cpu-v1` (1,000 ms padding, 1,500 ms deadline).
Only the vector-model input uses `" ".join(query.split())`, capped to the first 600
Unicode code points and retreated to the preceding complete U+0020-delimited token when
truncated. Lexical acquisition and the request/continuation digest retain the full
validated public query. This bound is fixed before characterization and cannot be
caller-, manifest-, or observation-selected. Reranker-, CLIP-, GPU-, ONNX-, mixed-, and
override-bearing configurations remain non-serving until separately characterized.

For each route and public request class, both upper bounds must be no greater than all
three applicable ceilings: the manifest differential, 25 ms absolute, and 10% of the
physically-absent replica's measured p95. The same fixed public deadline and padding are
used in both conditions and may depend only on registered public request shape and
runtime configuration. If the maximum supported configuration cannot complete under
that class or cannot satisfy either absolute or relative ceiling, the release fails; it
cannot adapt padding, lower the tested capacity, waive a lane, or raise the ceiling.
This is a bounded empirical non-interference contract, not a promise of cryptographic
constant time or of hiding arbitrary Internet jitter. Server logs may retain operational
timings but must not include paths, bearer capabilities, or hidden identifiers.

**Alternative considered:** keep current pre-filter ranking and remove score fields at
the projector. The result order, pagination, and selected top-k still reveal the hidden
candidate's displacement, so field stripping alone does not close the channel.

### 7. Use a durable, server-issued authorization-session capability

An authorization session is a stable server-side row with a random internal session
ID, canonical principal ID, trust issuer/surface family, status, expiry, credential
generation, opaque locator, verifier key id, and keyed verifier. The client receives a
bearer containing a 256-bit random secret only from an explicit generated
`govern_memory(operation="session")` lifecycle (`open`, `rotate`, `close`, and
`status`). The canonical bearer grammar is exactly
`as1.<locator>.<secret>`: `<locator>` is the 22-character unpadded RFC 4648 base64url
encoding of exactly 16 bytes, `<secret>` is the 43-character unpadded base64url encoding
of exactly 32 bytes, both use only `[A-Za-z0-9_-]`, and the complete ASCII credential is
exactly 70 bytes. The bounded parser rejects whitespace, padding, alternate alphabets,
wrong length, duplicate carriers, non-zero unused base64 bits, or any decoding whose
canonical re-encoding differs. Every carrier limit is 70 bytes, not an independently
configurable maximum. Rotation replaces the locator/verifier atomically while retaining
the stable session ID; close revokes the session, purpose, active grants, and unconsumed
escalation tokens. The terminal scrubber's authorization-bearer detector is generated
from this parser's candidate scanner and canonical re-encoding check; it has no separate
regex or looser grammar that can drift from accepted credentials.

Verifier keys and cell identity come only from two administrator-provisioned external
files: `EXOMEM_AUTH_SESSION_KEYRING_FILE` and
`EXOMEM_AUTH_SESSION_CONTROL_FILE`. Both are bounded, no-follow regular files outside
the vault, protected by owner-only POSIX mode or the Windows service-account ACL. The
keyring contains version, immutable keyring id, immutable `cell_id`, immutable
`logical_vault_id`, active key id, and accepted
`{key_id, 256-bit key, not_before, not_after}` entries. The control record repeats and
binds keyring id, `cell_id`, and `logical_vault_id`, names the serving-membership epoch
and registry attachment, and is signed/MACed by the deployment control plane. Any
mismatch fails readiness. No request, policy file, governance sidecar, vault copy,
receipt, CLI argument, or automatic first-use path may supply a key or cell identity.

Standalone provisioning is an explicit authenticated-local-owner operation. It mints
the keyring/cell/logical-vault ids once and atomically registers
`(cell_id, logical_vault_id, keyring_id, canonical storage attachment, owner,
governance_enrolled, expected_activation_tuple)` in the protected external host cell
registry before service startup. Hosted provisioning uses the authenticated cell control
plane for the same uniqueness record. `governance_enrolled` starts false only after a
trusted negative scan proves no workspace/internal activation artifact; it changes only
false-to-true and thereafter carries the expected activation store id, monotonic epoch,
and digest from Decision 3. Session readiness and standing-policy content readiness are
separate: an unavailable session key may disable capability issuance, but an unavailable,
stale, or contradictory enrollment record always makes governance state BLOCKED. A second live
vault/sidecar copy presenting the same ids and keyring collides with the attachment
lease and fails; copying the keyring/control file without registry ownership also fails.
A move quiesces the old attachment, obtains its explicit detach acknowledgement,
advances the registry attachment epoch, and attaches the same logical vault at the new
location before serving. A restore may preserve unexpired sessions only when the exact
vault, sidecar, keyring, control record, cell/logical-vault ids, and exclusive registry
attachment are restored after the old instance is proven offline; clone/import into a
new logical vault provisions a new cell/keyring and invalidates copied session rows.

The authoritative serving set is the external `serving_membership_epoch` control-plane
record, never peer gossip, timeout inference, or the set of recently responding pods.
Hosted mode publishes it through the provisioner/cell control plane and makes
`hosted_runtime.control_plane_readiness()` verify/surface only its content-free readiness
result; standalone mode uses the authenticated external host cell registry and the same
record schema.
It enumerates every admitted replica id and its state plus authenticated readiness
attestation over the current epoch, software/schema version, `cell_id`, active key id,
accepted key-id set, and control/keyring digests. Standalone mode has the same protocol
with one admitted replica. Issuance is ready only when every `SERVING` member has a fresh
valid attestation and the active key of every member is in the accepted-key intersection
of all members in that epoch. During rotation both key generations are distributed and
attested before issuance switches. Removing a replica requires an explicit
`SERVING -> DRAINING` transition, cessation of issuance, completion/no-in-flight
acknowledgement, and a committed epoch advance before the replica is excluded from the
intersection. A stopped or unreachable member remains included and blocks issuance.
A rejoin must attest the current epoch and accepted intersection before becoming
`SERVING`; a stale-epoch process cannot issue or resume credentials.

The version-1 wire record is a closed, canonical UTF-8 JSON object bounded to 64 KiB and
64 admitted replicas. It carries `{version, epoch, cell_id, logical_vault_id,
previous_epoch_digest, issued_at, expires_at, replicas, signing_key_id, mac}`; each
replica entry carries `{version, epoch, replica_id, state, software_version,
schema_version, cell_id, active_key_id, accepted_key_ids, control_digest,
keyring_digest, attested_at, expires_at, issuance_stopped, no_in_flight,
signing_key_id, mac}`. Identifiers are bounded opaque ASCII, keys and replicas are
sorted/unique, numbers are bounded integers, duplicate/extra keys and noncanonical bytes
are rejected, and epoch/attestation freshness is bounded to the maximum session TTL plus
30 seconds of skew. The membership digest is SHA-256 over the complete canonical signed
record. To avoid a circular commitment, replica `control_digest` is domain-separated over
the immutable control identity/attachment basis only; the signed control record separately
binds the complete membership digest and the mutable enrollment/activation tuple on every
request. An epoch successor binds the exact predecessor digest. Removal is legal only from
an already committed `DRAINING` predecessor with issuance stopped and no in-flight work;
the current runtime additionally refuses any accepted-key intersection that omits a key
named by a live unexpired session row.

Startup/readiness fails session capability service when either external key/control file
is missing/unsafe, identity binding differs, an admitted attestation is missing/stale, an
active key falls outside the epoch intersection, or a live row names an unavailable key.
Optional content calls without a credential may continue under standing policy only when
the protected registry attachment and governance enrollment/activation tuple still
verify. A missing/unsafe/stale registry attachment blocks all enrolled content; no
presented credential is silently ignored.

The keyed verifier covers a domain separator, bearer secret, locator, stable session id,
canonical principal, issuer/surface family, external `cell_id`, `logical_vault_id`,
keyring id, expiry, and credential generation. The row
stores the nonsecret locator/key id and verifier, never the secret; lookup and verifier
comparison are constant-time after bounded credential parsing. A new active key issues
new/rotated bearers while old keys remain verification-only for at least the maximum
session TTL plus clock skew and until no live row references them. A copied sidecar
without its exact registered external identities fails closed and never synthesizes a
replacement. Move/restore follows the exclusive attachment protocol above; a cross-cell,
unregistered, concurrently attached, or unmatched copy explicitly closes and invalidates
imported rows.

The raw bearer may appear only in the typed `issued_credential` field of a successful
`open` or `rotate` response and in the protected request credential field used to resume
it. `issued_credential` has the exact shape `{kind:
"authorization-session-bearer", bearer: <opaque>, expires_at: <RFC3339>}`. The terminal
scrubber permits that one occurrence only after the response matches the exact route
variant and schema and the bearer equals the just-minted value held in non-serializable
request context. A malformed/raised/extra-field response is scrubbed and refused; no
generic allowlist exempts bearer-shaped text.
It must never enter corpus content, projections, receipts, journals, SQLite rows, debug
representations, logs, metrics, traces, errors, remediation, or control-plane text. The
terminal secret/bearer scrubber is non-disableable for every content, error, governance,
session-lifecycle, diagnostic, warming, and other control envelope under active
governance. The just-minted typed issuance occurrence above is its only exception and
cannot be widened or disabled by policy. Retrieved governance-shaped text is data and
cannot open, rotate, resume, or close a session.

Each surface first extracts and destroys the bearer at its raw boundary, then resolves a
principal from trusted authentication, verifies against that principal and the external
cell identity, and installs immutable trusted context before ordinary schema work:

- MCP publishes one optional placeholder named exactly
  `authorization_session_credential`. Raw JSON-RPC/ASGI middleware performs bounded
  extraction from `params.arguments.authorization_session_credential`, removes or
  replaces it in every raw/logging copy, resolves transport authentication, verifies it,
  and installs trusted request context *before* FastMCP request logging, `FunctionTool`,
  or Pydantic validation. A non-string, duplicate, malformed, or invalid value receives
  the common credential refusal even when another argument is malformed. The generated
  tool wrapper and governance leaf receive no secret parameter. The same pre-framework
  interception applies to stdio/SSE; `Mcp-Session-Id` remains non-authoritative.
  A JSON-RPC batch containing any `tools/call` is rejected as one atomic request before
  FastMCP executes any element, even if other elements are notifications or non-tool
  methods. Middleware sanitizes every carrier copy first, then returns one content-free
  batch refusal. Two calls with A+absent, A+B, invalid+valid, duplicate carrier
  keys/values, or any reordering all execute zero elements and allocate no cache,
  idempotency, receipt, content mutation, or session state.
- REST and Hosted accept the credential only in the sensitive
  `X-Exomem-Authorization-Session` header, distinct from service/gateway
  `Authorization`; bodies/query strings and caller principal headers cannot carry it.
  Raw middleware removes/redacts that header before ASGI access logs, exception copies,
  validation, idempotency, or leaf dispatch.
- CLI exposes only `--authorization-session-fd <fd|->`; it reads one bounded bearer from
  an already-open protected descriptor or stdin (`-`), clears the buffer after
  verification, and never accepts a literal argv value or environment variable. Help
  names the descriptor carrier; generated completion/history material never contains the
  bearer.

Extraction/redaction precedes all framework logging and validation; trusted principal
resolution and capability verification then precede cache lookup/key creation,
idempotency lookup, ordinary argument validation/coercion, membership/decision work,
receipt allocation, or governance leaf dispatch. The public schemas expose only the MCP
placeholder, REST/Hosted protected header, or CLI descriptor option as appropriate;
none exposes an internal session id. Legacy arbitrary handles are accepted only as an
echo of an already verified capability during bounded migration and can never create or
claim a binding.

The registry credential matrix is closed:

| route variants | credential rule |
| --- | --- |
| session `open` | forbidden; authenticate trusted principal and issue a new session |
| session `status`, `rotate`, `close`; session `grant`, session `revoke`, `declare` | required and verified before any state/content read |
| `list`/`explain`/`simulate` for self; every registered content/resolution route including find/search/ask, get/fetch/read, browse/list, graph/link suggestions, review/attention/audit/provenance, Records/Planning, dataset, media/frame, recall/inject hooks, and content-bearing mutation previews/receipts | optional; absent means standing-only, present-invalid rejects the whole request, present-valid installs the session before any decision/cache |
| owner-only propose/commit/suspend/resume/undo/backfill-companion and standing grant/revoke/cross-audience inspection | no session authority required; if a credential field is admitted it must still verify before work and cannot replace owner authorization |

Startup schema coverage enumerates every command, selector variant, legacy leaf,
retrieve/inject hook, and generated MCP/REST/Hosted/CLI adapter into exactly one row. A
new unclassified route fails startup.

FastMCP runs stateless HTTP, so `Mcp-Session-Id` or `Context.session_id` is not a durable
authority there. Stable stdio/SSE transport identity may be supplementary context, but
reconnect and horizontal-scale continuity comes only from the verified capability.

Escalation tokens, session grants, purpose declarations, and revoke operations bind the
stable internal session ID—not the raw bearer—plus the canonical principal, issuer
family, item/path fingerprints, audience, purpose, and expiry as applicable. A token or
grant from session A is rejected in session B even when both sessions have the same
principal. Missing, expired, closed, unresolved, cross-principal, cross-issuer, or
cross-session state fails closed with one credential-independent error shape.

**Alternative considered:** bind the current caller-supplied string directly onto
`RequestPrincipal`, or use FastMCP's transport session ID. The former lets the caller
claim an authorization context; the latter is not durable under stateless HTTP,
reconnects, restarts, or horizontal scale.

### 8. Keep the security mechanisms composable and independently testable

The per-scope evaluator, membership outcome, path classifier, prospective-snapshot
comparison, capability verifier, and projected-corpus reducer remain pure or isolated
behind narrow stores. Pure red tests land before dispatcher/surface wiring. Registry
coverage proves every command/selector/path parameter and every MCP/REST/Hosted/CLI
adapter participates. Torch-backed vector/reranker tests use existing soft-fail seams;
the mandatory equivalence suite has a deterministic keyword/graph mode that runs
without model extras.

No change is accepted solely on focused green tests. The exact implementation diff must
receive an independent security review against the threat model, all findings must be
fixed, and the same reviewer must recheck them. A separate verifier then exercises the
four real surfaces, restart/rescale session continuity, reserved-path bypass matrix,
counterfactual result equivalence, strict OpenSpec validation, lint, lean/full tests,
and latency gates. Canonical spec sync and archive occur only after those durable records
exist.

## Risks / Trade-offs

- **[Risk] Exact projected-corpus top-k adds projection variants and filtered-lane work.**
  → Use the exact three-part namespace, fixed canonical variants, principal-free
  versioned measurements, and the hard 256-per-item overflow refusal; apply authorization
  inside each lane and require the non-waivable exact-capacity timing gate.
- **[Risk] A mutable workspace diverges from the active compiled generation.** → Keep
  mutable bytes pending, preserve exact source bytes in append-only generations, expose
  owner-only parity diagnostics, refuse observed drift under the cooperative writer
  fence, and make the verified SQLite policy/projector/catalog tuple the only runtime
  authority; direct OS-owner mutation remains outside that filesystem guarantee.
- **[Risk] Unicode/platform path alias handling diverges across Linux and Windows.** →
  Test logical and physical normalization separately, include NFKC/case/backslash/short-
  name/symlink fixtures, and require secure leaf resolution in addition to registry
  preflight.
- **[Risk] Failing closed on a missing media companion surprises users who authored
  semantic-only scopes.** → Return an owner-only diagnostic naming the missing or
  malformed companion and remediation; ordinary callers see missing. Path/ref selectors
  remain an explicit way to classify binary artifacts without companions.
- **[Risk] Invalidating legacy session grants interrupts a live conversation.** →
  Migrate schema monotonically, expire only rows that cannot prove exact scopes, and
  return a fresh authorization notice rather than guessing a broad scope binding.
- **[Risk] A bearer exposed by a client can resume its authorization session.** → Use
  256-bit random capabilities, keyed verifiers, short bounded expiry, issuer/principal
  binding, constant-time checks, explicit rotation/close, and aggressive redaction from
  all non-lifecycle outputs.
- **[Risk] A verifier key unavailable to one replica breaks resumption or staged
  rotation.** → Bind keyring/control files to registered cell/logical-vault identity,
  compute intersection from the authoritative serving epoch, require explicit drain/ack/
  epoch advance, overlap old/new keys for maximum TTL plus skew and live-row drain, and
  fail issuance/readiness closed on any stale member.
- **[Risk] Copying a vault and its local sidecars duplicates session authority.** →
  Require an exclusive external registry attachment; restore/move only after old-instance
  detach proof, and provision new cell/keyring identities plus session invalidation for a
  clone.
- **[Risk] Framework validation or logging copies a bearer before ordinary redaction.**
  → Consume the exact MCP/header/fd carriers at raw boundaries before FastMCP/ASGI/CLI
  framework logging and validation, then scan actual-wire failure copies.
- **[Risk] Central path metadata misses a newly added nested selector.** → Startup and
  schema-fidelity tests enumerate every registered route and fail until all path/ref
  fields and source/destination roles are classified.
- **[Risk] Counterfactual byte equality is polluted by nondeterministic response fields.**
  → Keep wall-clock timing/request IDs out of governed content payloads and compare the
  canonical wire envelope after the existing transport-only metadata boundary.

## Migration Plan

1. Retain the already-archived Wave 0/1 canonical baseline and run its strict validation
   before implementation.
2. Land pure red tests for the per-scope lattice, non-Markdown classification,
   prospective snapshot identity, reserved-path normalization, capability verification,
   raw projection, and projected-corpus reducer.
3. Remove every legacy `credential_scrubber` policy field through owner review; its
   presence leaves the immutable active tuple unchanged but enrolled content serving
   BLOCKED until a candidate compiles with the non-disableable terminal boundary.
4. Backfill class-specific `governance_companion` descriptors only through owner-reviewed
   receipt events with explicit artifact semantics and canonical frame milliseconds;
   semantic-only policy remains fail-closed during backfill.
5. Provision/register the external cell/logical-vault/keyring/control identity,
   never-enrolled proof, closed internal-state registry, and serving-membership epoch
   before any OPEN decision or session storage is reachable.
6. Under the cooperative whole-tree/schema fence, drain every v3 process, snapshot exact
   v3 sidecar/workspace/receipts, prove ordinary openers leave v3 unchanged, recheck
   stable conflict-free workspace/catalog bytes, irreversibly record enrollment, and
   apply one transactional v3→v4 migration. It creates exact session bindings plus an immutable
   compiled policy/catalog generation and atomic active tuple, expires arbitrary-handle/
   unscoped authority,
   and refuses newer schemas. The v3 binary is explicitly unsupported on v4.
7. Enumerate fixed reachable projection variants and build/validate the exact
   `(active fingerprint, projector version, catalog generation)` namespace before its
   single active tuple activates; race policy and content/companion writers at the tuple
   CAS and reject the stale side.
8. Wire exact-parser raw pre-framework carrier extraction, atomic MCP tool-call batch
   refusal, shared dispatcher/internal-state guard and trusted principal/session resolver,
   then generate MCP, REST, Hosted, CLI, OpenAPI, and schema parity.
9. Wire direct reads—including scrub-safe exact L6 raw versus content-free secret
   refusal—and all retrieval/graph/count/error/timing reductions through the projected-
   corpus contract. Run focused tests, the no-model equivalence matrix, and the
   optional semantic-lane checks where available, including exact-capacity 25 ms/10%
   actual-wire bounds.
10. Deploy the hardening before exposing any consolidated vault. Existing generic access
   to every registered administration/database/index/journal/alias path begins refusing
   immediately; operators use the owning governed command/subsystem.
11. Record independent security review plus same-reviewer recheck, then independent
   verification. Only after both are green, sync these deltas into canonical specs and
   archive the change.

Rollback before consolidation is explicit; the old binary is not assumed to ignore v4.
Either restore the pre-migration v3 sidecar snapshot (revoking all later ephemeral
authority), or run the offline v4→v3 tool that closes sessions, expires bound grants/
purposes/tokens, mirrors the active tuple's exact policy generation source bytes under the
whole-tree fence, proves recompilation parity, removes only v4 schema, verifies exact v3
schema/receipts, advances external recovery metadata without clearing monotonic
`governance_enrolled`, and only then starts the actual v3 binary. The suite first probes it against an isolated
v4 copy, records which paths self-refuse, and proves the rollout schema/lease fence bars
it regardless; no old-binary compatibility is claimed. Do not roll back after a consolidated vault depends on these
invariants: physically separate the compartments or restore the hardened release first.

## Open Questions

None. Immutable policy/catalog tuple activation, fixed projection variants, repository-
owned timing ceilings, non-disableable scrubbing, exact raw credential carriers and batch
semantics, external enrollment/cell/fleet identity, companion backfill, and closed
internal-state ownership are decisions of this change rather than follow-up design work.
