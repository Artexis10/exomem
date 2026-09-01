# Design — entity recurrence sensor (unresolved-identity v1)

Authority order: source+tests > active/accepted OpenSpec > the no-nudge
architecture report (KB, S5) > older notes.

## D1 — Evidence source

The only evidence is what pages already say: for each parsed page in the audit's
existing page list, the body's wikilinks (`vault.find_body_wikilinks`) whose
target does not exist in the vault. No new I/O beyond the bodies the audit
already holds; no embedding; no model. An identity is the NFKC-normalised form
of the link target via `entity_candidates.identity_key` — imported, never
reimplemented. Pages whose body is unavailable are not counted (absence is
never evidence).

## D2 — Gates

A candidate fires only when ALL hold:

1. **Spread**: unresolved wikilinks to the identity appear in at least
   `SPREAD_MIN_PAGES` distinct pages (PROVISIONAL, 3 — f21's "three distinct
   sources" precedent).
2. **Unresolved as a page**: no vault page exists at the link target, and none
   exists at the NAME the target ends in either. `[[Some/Wrong/Path/Marin Osk]]`
   while `Notes/Marin Osk.md` exists is a misfiled link to a page that exists —
   the audit already reports it as one — not evidence that nobody has written
   the identity down. An AMBIGUOUS bare name counts as existence for the same
   reason: a name the vault wrote down twice is emphatically not one it never
   wrote down.
3. **Unresolved as an entity**: the identity does not NFKC-resolve against the
   entity registry's titles or aliases (`entity_candidates` machinery reused).
   A resolved identity is the registry's business, not a candidate.
4. **Self-links excluded**: a page linking to itself, and links inside
   `Entities/`, count nothing.
5. **Present attention only**: a page whose status is `superseded`, `archived`
   or `draft`, and a page whose tree is `excluded` in `_access.yaml`, supplies
   no spread and can never be the anchor. A retired note's links record what the
   vault USED to reach for, and a retired page often sorts early, so without
   this a finding both crosses the gate on history and names it. This is the
   status half of `audit._is_active_compiled_rw` and DELIBERATELY only that
   half: a Source or an Evidence page IS the corpus reaching for a name, so the
   template's compiled-and-read-write restriction would discard exactly the
   evidence this sensor exists to count.

Counting is per page (a page mentioning the identity five times contributes
one), so frequency inside one note never substitutes for spread — the
incidental-mention discipline made mechanical.

**A dot in a name is punctuation until a file proves otherwise.** A wikilink
target carrying an extension is an attachment only when `_ordinary_file_exists`
confirms a file at it — the rule `_check_wikilinks` already applies, and the
reason it probes the filesystem instead of reading the suffix. Deciding from the
dot alone silences `SomeProduct 2.0` (`Path.suffix` is `.0`), `Dr. Ines Roth`,
`U.S. Navy` and `Node.js`. The probe runs AFTER spread and the registry have
narrowed the set, so it costs at most one existence check per distinct suffixed
target per surviving candidate, never one per link.

## D3 — Identity assist (lexical only)

The finding carries up to `MAX_NEAR_MATCHES` (PROVISIONAL, 3) registry entries
sharing at least one identity token with the candidate, ordered
deterministically (shared-token count desc, then path asc). This is advice for
the agent's own existing check-before-create judgment. No fuzzy-distance
algorithm, no embedding — no entity-title vector index exists and this change
may not add one.

## D4 — Placement, fingerprint, anchor

Category `entity_recurrence`, reason `unresolved_identity_recurs`, registered
in `EPISTEMIC_REVIEW_CATEGORIES` (opt-in; same argument as
`scope_divergence_semantic`). Findings ride the existing review composer:
`meta["signal_version"] = content_hash(identity_key)[:16]` — the identity IS
the signal, so a dismissal binds to the candidate and survives every
incidental edit; v1 defines no material-change reopen (growth in spread does
not re-raise a dismissed candidate — PROVISIONAL, revisit with calibration).
The finding anchors to the lexicographically smallest mentioning page
(deterministic; the full sorted page list rides `meta`), accepting that if
that page stops mentioning the identity the anchor moves and a dismissal can
orphan — recorded here as a known v1 trade rather than hidden.

The page list rides `meta` and NOT the `paths` group field, and that is the
dismissal contract rather than a formatting choice: `review_state.fingerprint`
folds a finding's `paths` into the item identity, so a list that grows every
time somebody links the name again would move the fingerprint on exactly the
event this design says must not re-raise a settled candidate. Because `paths` is
therefore empty, `review_context` draws this category's related-page evidence
from `meta["pages"]`, so a reader opening the item still sees the pages the
count is about.

`meta["review_partition"]` is the identity, for the same reason the signal
version is. Two identities recurring across one corpus routinely share an
anchor — the page that sorts smallest mentions both — and without a partition
`attention` fuses them onto ONE review id: a single dismissal puts down several
unrelated candidates, and a third identity arriving on that anchor changes the
fused fingerprint and reopens the settled decision. Same mechanism
`prediction_window`, `question_aging`, `bridge_review` and
`unreflected_outcomes` already use.

**No cap on findings, deliberately.** One finding per qualifying identity, with
no per-sweep ceiling — the `scope_divergence_semantic` precedent. A cap would
make which candidates a reader sees depend on how many others qualified, and the
first-run backlog is exactly what the opt-in registration already handles: the
category is registered and triageable but stays out of the default attention
union, so a large first sweep never displaces the daily surface.

## D5 — Resolution by state change

Quiet when: the target page is created (link no longer unresolved), or an
entity page whose title or alias NFKC-resolves the identity exists. Deleting
either brings the finding back. Never resolved by time or by the runtime
editing anything.

## D6 — Acceptance fixtures

1. Three pages link `[[Unresolved Name]]` → one finding: candidate, three
   pages sorted, near-matches listed. RED on base (unknown category).
2. Plain-text mentions at matched frequency, no wikilinks → quiet (the
   deferred stream, asserted so the v1 scope is pinned, not implied).
3. Registry-resolved identity (alias match) linked from five pages → quiet.
4. Two-page spread → quiet.
5. State change both directions: create entity page → quiet; delete it →
   finding returns; create the target page itself → quiet.
6. S6: family `off` silences; a dismissed candidate stays dismissed across
   incidental edits.
7. Determinism: identical findings across page-insertion orders.

## D7 — What the reviewer should attack

The identity normalisation (aliasing collisions two distinct names into one
candidate), the anchor-movement trade in D4, registry-resolution correctness
(subpath/heading links, `[[a|display]]` forms), cost on large vaults (the
sweep must stay one pass over already-parsed bodies), and whether the
near-match ordering is genuinely deterministic.

Known follow-ups, named rather than hidden:

- **The skill scaffold's `audit-checks.md` does not list this category.** That
  gap is pre-existing and general: `scope_divergence_semantic` (this change's
  own template), `unreflected_outcomes` and `entity_type_unregistered` are all
  registered and all absent from that file too. `src/exomem/_scaffold/**` is out
  of scope for this change.
  A separate change should reconcile the scaffold with the registry once, for
  every category, rather than each sensor patching it on the way past.
- **Vault-root spelling.** The resolution-entry walk derives a relative path the
  same way `ParsedPage` and `WikilinkResolver._build` do (through `resolve()`),
  so one file can never reach the resolver under two spellings. The divergence
  this prevents is currently unreachable and therefore unpinned by a test: the
  only vector is a symlinked vault root, and `_parse_all` refuses one upstream
  (`reserved identity catalogue could not acquire the vault`) on the untouched
  base too. The alignment is kept as defence, not as a fix for a live bug.

## D8 — Measured cost (recorded at delivery)

10,300-page synthetic vault (two wikilinks per page, 300 registry entities),
quiesced WSL box: sweep 0.70 s ≈ 69 µs per page over already-parsed pages —
about a fifth of the `_parse_all` it rides on. Exactly one path-only vault walk
and one file open (`entity-types.yaml`, digest-cached) per sweep, plus at most
two existence probes per gate-crossing dotted identity and none below the gate.
Near-match assist ≈ 35 µs per surviving candidate against a 300-entry registry
(O(candidates × registry), in-memory).
