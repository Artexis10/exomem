"""Exomem local adapter: leaf (in-process ops) and wire (in-process MCP) modes.

Isolation is safe by construction: ``EXOMEM_VAULT_PATH`` is mandatory with no
fallback, and this adapter always points it at a fresh benchmark-owned temp
vault. Determinism knobs are pinned explicitly and recorded in the profile;
a scored response carrying warming/degraded markers raises
:class:`AdapterEnvironmentError` (environment fault, never a contender loss).
Diagnostic logs go OUTSIDE the disposable vault so evidence survives cleanup.

Governance wiring (``governance="wired"``, leaf mode only) goes through PUBLIC
product surfaces exclusively — the harness never simulates governance:

- The corpus ``PolicySet`` is translated into the vault's opt-in
  ``_Governance/`` YAML policy (the documented authoring format: strict
  schema-v1 scope/rule documents compiled by exomem's own loader, which
  validates the result).
- Query personas map onto exomem's canonical principal space at the CLI/leaf
  surface boundary: the ``owner`` persona is the vault operator
  (``owner_principal``) and every other persona becomes a
  ``normalize_audience`` principal, bound per call with ``request_scope`` —
  the same set/reset shape the MCP and REST surfaces use.
- Enforcement is exomem's release plane (egress), untouched: a translated
  ceiling-0 rule silently withholds the governed page from the restricted
  principal while the owner keeps full disclosure.

Translation happens at the end of ``ingest`` because scope selectors need the
real vault paths that only exist after capture; the vault is governed before
the first scored search. Corpus rules with a ``declassify_at`` on or before
the corpus knowledge horizon (``clock.end_of_window()``) are declassified and
not restricted — exomem's policy schema has no time-conditioned rules, so the
translation snapshots the policy at the same "now" the ingested vault state
represents. Corpus tombstones translate to ceiling-0 rules for EVERY declared
persona including the owner (the oracle's "removed from all future release"),
keeping the wiring inside the release plane rather than destroying content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from membench.adapters.base import (
    AdapterUnsupported,
    NativeAnswer,
    AdapterEnvironmentError,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    StateExportPage,
    register_adapter,
)
from membench.clock import end_of_window
from membench.ids import sentinels_in
from membench.schema import ClaimRecord, PolicyRule, PolicySet, load_jsonl

#: Per-source cap on quoted text in a native answer, mirroring the extractive
#: answerer's cap so answer LENGTH never differs by answer mode.
_ANSWER_CHARS_PER_SOURCE = 800

_NEUTRAL_SEARCH_KWARGS = {
    "scope": "kb",
    "detail": "full",
    "graph": False,
    "rerank": False,
    "prefer_compiled": False,
    "prefer_active": False,
    "prefer_used": False,
}

_PRODUCT_DEFAULT_SEARCH_KWARGS: dict[str, object] = {"scope": "kb", "detail": "full"}

# Stable issuer for the persona → principal mapping. Rules authored by the
# translation and principals bound at search time both derive the audience id
# through exomem's canonical ``normalize_audience(subject, issuer)``, so the
# two sides can only ever agree or both be wrong — never silently diverge.
_PERSONA_ISSUER = "membench-persona"

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _document_ulid(key: str) -> str:
    """Deterministic 26-char Crockford-base32 document id for authored YAML."""

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return "".join(_CROCKFORD_ALPHABET[byte & 31] for byte in digest[:26])


def _document_slug(key: str) -> str:
    """Filesystem-safe, collision-suffixed filename stem for a policy doc."""

    cleaned = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:60]
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{suffix}" if cleaned else suffix


def lexical_profile(name: str = "neutral-lexical") -> Profile:
    """Model-free profile: deterministic, embeddings off, backends pinned."""

    return Profile(
        name=name,
        settings={
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            # Correct as a determinism pin, and REQUIRES the product fix
            # `91b016f` (fix/lexical-degraded-retention, unmerged as of
            # 2026-08-05). Without that fix this flag zeroes *text* retrieval:
            # a disabled lane never "fails", so the BM25-only fallback at
            # find_candidates.py:242 never triggers, and the strict retention
            # seam vetoes every candidate lacking ALL query stems — which no
            # natural-language question satisfies. Measured on a fixed vault
            # over 20 real queries: 0 hits without the fix, 52 with it (the
            # August baseline recorded 52 on the same 20). Dropping the flag
            # instead is NOT the workaround: CLIP then reports `degraded` and
            # the run is refused for a misleading reason. Until the fix lands,
            # the retrieval floor makes the failure loud rather than silent.
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_VEC_BACKEND": "numpy",
            "EXOMEM_LEXICAL_BACKEND": "python",
        },
    )


def embeddings_profile(name: str = "recommended-embeddings") -> Profile:
    """Recommended profile: vector lane on; identical pins otherwise.

    CLIP/media stay off (image evidence is not CLIP-scored in v0.1) and every
    other determinism pin matches the lexical profile. Machine-specific model
    environment (HF_HOME, CUDA_VISIBLE_DEVICES, EXOMEM_DEVICE) is supplied by
    the caller's process env, never baked into the profile.
    """

    base = lexical_profile(name=name)
    settings = dict(base.settings)
    # exomem's disable flags are plain string-truthiness checks
    # (os.environ.get(...)): "0" would still DISABLE. Empty string is falsy,
    # which is the only way to express "enabled" through a set-only env seam.
    settings["EXOMEM_DISABLE_EMBEDDINGS"] = ""
    return Profile(name=name, settings=settings)


class ExomemLocalAdapter:
    """Drives exomem through public boundaries only (op leaves or MCP tools)."""

    name = "exomem-local"
    supports_group_reuse = False
    #: Altitudes this adapter can honour: captures raw sources, and compiles conclusions through `remember`.
    supported_altitudes = frozenset({"raw_source", "compiled"})

    def __init__(
        self,
        *,
        altitude: str = "raw_source",
        mode: str = "leaf",
        search_style: str = "neutral",
        governance: str = "off",
        answer_mode: str = "harness",
    ) -> None:
        #: Altitude this run asked for; validated by `ingestion_altitude`.
        self.altitude = altitude
        if mode not in {"leaf", "wire"}:
            raise ValueError(f"unknown mode {mode!r}")
        if search_style not in {"neutral", "product-default"}:
            raise ValueError(f"unknown search_style {search_style!r}")
        if governance not in {"off", "wired"}:
            raise ValueError(f"unknown governance {governance!r}")
        if answer_mode not in {"harness", "native"}:
            raise ValueError(f"unknown answer_mode {answer_mode!r}")
        if governance == "wired" and mode != "leaf":
            # The in-process wire transport has no per-request identity seam
            # (an unauthenticated stdio-shaped call always resolves to the
            # owner), so personas cannot be threaded there. Refuse loudly
            # rather than silently downgrading a requested wiring to
            # default-open.
            raise ValueError("governance wiring requires leaf mode")
        self.mode = mode
        self.search_style = search_style
        self.governance = governance
        #: `harness` scores the shared extractive answerer; `native` scores
        #: exomem's own context pack, citations and abstention. Default stays
        #: `harness` so every existing run and every other adapter keep
        #: identical behaviour and the change is opt-in and recorded.
        self.answer_mode = answer_mode
        self._workdir: Path | None = None
        self._vault: Path | None = None
        self._schema: object | None = None
        self._mcp: object | None = None
        self._saved_env: dict[str, str | None] = {}
        self._profile: Profile | None = None
        # source_id → ALL captured vault paths (a source may be captured more
        # than once, e.g. duplicate ops); scope selectors cover every one.
        self._source_paths: dict[str, list[str]] = {}
        #: conclusion_id → vault path of the compiled note (compiled altitude).
        self._compiled_paths: dict[str, str] = {}
        #: vault path → source_id, for reading a note's declared basis back.
        self._path_to_source: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------
    @property
    def governance_state(self) -> str:
        """Three-state governance measurement label for this configuration."""

        return "wired" if self.governance == "wired" else "default_open"

    @property
    def ingestion_altitude(self) -> str:
        """The altitude this run selected; validated against what we support.

        Refusing here rather than degrading is the 4b.29 rule applied to
        altitude: a run that cannot apply a tier to a contender must say so, not
        quietly measure it at a different one.
        """

        if self.altitude not in self.supported_altitudes:
            raise AdapterUnsupported(
                f"{self.name} cannot honour altitude {self.altitude!r}; "
                f"supports {sorted(self.supported_altitudes)}"
            )
        return self.altitude

    def capabilities(self) -> frozenset[Capability]:
        base = {Capability.INGEST_API, Capability.SEARCH, Capability.STATE_EXPORT}
        # Declared ONLY when the run asked for it, exactly as GOVERNED_VIEWS is
        # declared only under active wiring. Answer mode decides which of
        # provenance/abstention/calibration measure the product rather than the
        # harness, so it has to be a variable a run can hold fixed and A/B —
        # baking it into the adapter makes the one comparison that would
        # attribute its effect impossible to run.
        if self.answer_mode == "native":
            base.add(Capability.NATIVE_ANSWER)
        # Declared ONLY when the wiring is active (spec: "Governed Views Are
        # Wired, Not Simulated") — never from the product's mere ability.
        if self.governance == "wired":
            base.add(Capability.GOVERNED_VIEWS)
        return frozenset(base)

    def _set_env(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            self._saved_env.setdefault(key, os.environ.get(key))
            os.environ[key] = value

    def setup(self, workdir: Path, profile: Profile) -> None:
        workdir = Path(workdir)
        self._workdir = workdir
        self._profile = profile
        vault = workdir / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        logs = workdir / "logs"  # outside the vault: evidence survives cleanup
        logs.mkdir(parents=True, exist_ok=True)
        self._set_env(
            {
                "EXOMEM_VAULT_PATH": str(vault),
                "EXOMEM_CONFIG_PATH": str(workdir / "exomem-config.json"),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(workdir / "leases"),
                "EXOMEM_LOG_DIR": str(logs),
                **profile.settings,
            }
        )
        from exomem import embeddings as embeddings_module
        from exomem import find as find_module
        from exomem.init import init_vault
        from exomem.schema import load_source_schema

        # A profile that ASKS for the semantic lane must get it or stop. exomem
        # soft-degrades when sentence-transformers is missing ("embeddings
        # disabled (import failed) … keyword-mode find() still works"), which is
        # correct for a product and catastrophic for a measurement: the run
        # completes, reports invalid=False, and stamps the manifest
        # `profile: recommended-embeddings` over numbers that are bit-identical
        # to the lexical run. A false label on a real number is worse than a
        # wrong number, and this has now silently produced mislabelled runs
        # three times (two superseded in the v0.1 findings, once again on
        # 2026-08-05). An environment fault invalidates a run; it is never a
        # contender loss.
        if not profile.settings.get("EXOMEM_DISABLE_EMBEDDINGS", "1"):
            try:
                # get_model() is the same call the writer path makes; catching
                # its ImportError here is what turns a silent degrade into a
                # refusal. Loading the model is the point — "the package
                # imports" is not the same claim as "the lane works".
                embeddings_module.get_model()
            except Exception as exc:
                raise AdapterEnvironmentError(
                    f"profile {profile.name!r} requests the semantic lane but the "
                    f"embedding model could not load ({type(exc).__name__}: "
                    f"{str(exc)[:160]}). Refusing to score a lexical run labelled as "
                    "embeddings — install the `embeddings` extra."
                ) from exc

        init_vault(vault)
        self._vault = vault
        self._schema = load_source_schema(vault)
        find_module.clear_cache()
        embeddings_module.clear_embedding_indexes()
        if self.mode == "wire":
            from exomem.server import build_server

            self._mcp = build_server(require_auth=False)

    def cleanup(self) -> None:
        for key, previous in self._saved_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._saved_env.clear()
        self._mcp = None
        self._source_paths.clear()
        try:
            from exomem import find as find_module

            find_module.clear_cache()
        except Exception:  # pragma: no cover - cleanup best effort
            pass

    # -- wire helper ------------------------------------------------------
    def _call_tool(self, tool: str, args: dict) -> dict:
        import asyncio

        result = asyncio.run(self._mcp.call_tool(tool, args, run_middleware=False))  # type: ignore[union-attr]
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        for content in getattr(result, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                return json.loads(text)
        return {}

    # -- ingest -----------------------------------------------------------
    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        ops_file = Path(native_dir) / "capture-ops.jsonl"
        if not ops_file.is_file():
            raise AdapterEnvironmentError(f"missing native op stream: {ops_file}")
        from exomem import commands, find as find_module

        results: list[OpResult] = []
        for line in ops_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            op = json.loads(line)
            started = time.perf_counter()
            try:
                if op.get("op") == "remember":
                    ok, detail = self._compile_one(op, commands)
                elif self.mode == "wire":
                    payload = self._call_tool(
                        "capture_source",
                        {
                            "content": op["content"],
                            "title": op["title"],
                            "source_type": op.get("source_type", "other"),
                        },
                    )
                    ok = bool(payload) and not payload.get("error")
                    detail = None if ok else json.dumps(payload.get("error"))
                else:
                    captured = commands.op_capture_source(
                        self._vault,
                        self._schema,
                        content=op["content"],
                        title=op["title"],
                        source_type=op.get("source_type", "other"),
                    )
                    # Recorded unconditionally: governance wiring needs these
                    # for scope selectors, and the compiled altitude needs them
                    # to resolve `cites` into the vault paths `remember` links.
                    source = captured.get("source") if isinstance(captured, dict) else None
                    path = source.get("path") if isinstance(source, dict) else None
                    if isinstance(path, str) and op.get("source_id"):
                        self._source_paths.setdefault(str(op["source_id"]), []).append(path)
                        self._path_to_source[path] = str(op["source_id"])
                    ok, detail = True, None
            except Exception as exc:  # recorded, stays in denominators
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(
                OpResult(
                    seq=int(op.get("seq", len(results))),
                    op=str(op.get("op", "capture_source")),
                    source_id=op.get("source_id"),
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )
        if self.governance == "wired":
            # After capture (scope selectors need the real vault paths) and
            # before the first scored search: the run measures a governed
            # vault end to end.
            self._wire_governance(Path(corpus_dir))
        find_module.clear_cache()
        return results

    # -- compiled altitude -------------------------------------------------
    def _compile_one(self, op: dict, commands) -> tuple[bool, str | None]:
        """Author one compiled conclusion through the product's own surface.

        `remember(sources=[...])` is what writes `ingested_into:` back onto each
        cited source, which is the citation chain the compiled altitude exists
        to measure. Cited source ids are resolved to the vault paths recorded
        during capture; a conclusion whose sources were never captured is a
        failure rather than a silently source-less note, because a conclusion
        with no basis is exactly the record where citation precision cannot be
        verified.
        """

        cites = [str(c) for c in (op.get("cites") or [])]
        paths: list[str] = []
        for source_id in cites:
            captured = self._source_paths.get(source_id)
            if captured:
                paths.append(captured[0])
        if cites and not paths:
            return False, f"none of {cites} were captured; cannot link the chain"

        payload = commands.op_remember(
            self._vault,
            content=op["content"],
            title=op["title"],
            note_type="insight",
            sources=paths or None,
        )
        if isinstance(payload, dict) and payload.get("error"):
            return False, json.dumps(payload.get("error"))
        # Supersession demotes the earlier conclusion rather than merely adding a
        # newer one; without it the vault holds two live contradictory notes and
        # the contradiction dimension would score a conflict the corpus never
        # declared.
        if op.get("supersedes") and isinstance(payload, dict):
            self._compiled_paths[str(op["conclusion_id"])] = str(
                (payload.get("page") or {}).get("path") or ""
            )
        elif isinstance(payload, dict):
            self._compiled_paths[str(op["conclusion_id"])] = str(
                (payload.get("page") or {}).get("path") or ""
            )
        return True, None

    # -- governance wiring (public surfaces only) --------------------------
    def _audience_id(self, persona_id: str) -> str:
        """Persona → exomem's canonical audience id at the CLI/leaf surface."""

        from exomem.governance import principal as principal_module

        if persona_id == "owner":
            return principal_module.OWNER_AUDIENCE
        return principal_module.normalize_audience(
            subject=persona_id, issuer=_PERSONA_ISSUER
        )

    def _principal_for(self, persona: str | None):
        """A bound-per-call request principal; ``None`` means the operator."""

        from exomem.governance import principal as principal_module

        if persona is None or persona == "owner":
            return principal_module.owner_principal(surface="cli")
        # Enforcement is audience-id-based; the surface tag is audit-trail
        # cosmetics only (release decisions never branch on it).
        return principal_module.RequestPrincipal(
            audience_id=self._audience_id(persona), surface="cli", resolved=True
        )

    def _rule_paths(
        self, rule: PolicyRule, claims_by_id: dict[str, ClaimRecord]
    ) -> list[str]:
        """Vault paths a corpus rule covers: targeted sources plus the
        recorded provenance (assertion sources) of targeted claims — every
        captured path for each source, not just the last."""

        source_ids = set(rule.target_sources)
        for claim_id in rule.target_claims:
            claim = claims_by_id.get(claim_id)
            if claim is not None:
                source_ids.update(a.source_id for a in claim.assertions)
        return sorted(
            {
                path
                for sid in source_ids
                for path in self._source_paths.get(sid, [])
            }
        )

    def _governance_documents(
        self, policy: PolicySet, claims_by_id: dict[str, ClaimRecord]
    ) -> tuple[dict[str, dict], list[dict]]:
        """Corpus PolicySet → strict `_Governance/` schema-v1 documents,
        plus the record of what the translation could NOT represent.

        Ceiling 0 is exomem's silent withhold (L0): the restricted principal
        never sees the page. Audiences with no matching rule keep full
        disclosure — exactly the corpus semantics where only personas outside
        a rule's ``allow`` list are restricted. Only personas DECLARED by the
        corpus are translated; v0.1 corpora never query undeclared personas
        (generation enforces expectation consistency).

        A rule declassified inside the knowledge horizon is DROPPED (the
        vault is the final-state snapshot and exomem policy v1 has no
        time-conditioned rules) and recorded in the returned ``dropped``
        list; the runner turns the affected queries' governance gates into
        UNSUPPORTED — a translation gap is never scored as pass or fail.
        """

        documents: dict[str, dict] = {}
        dropped: list[dict] = []
        horizon = end_of_window()

        def add_scope(key: str, name: str, paths: list[str], audiences: set[str]) -> None:
            if not paths or not audiences:
                return
            scope_id = _document_ulid(f"membench:scope:{key}")
            documents[f"scopes/{_document_slug(key)}.yaml"] = {
                "governance_version": 1,
                "id": scope_id,
                "name": name,
                "refs": list(paths),
            }
            for audience in sorted(audiences):
                rule_key = f"{key}:{audience}"
                documents[f"rules/{_document_slug(rule_key)}.yaml"] = {
                    "governance_version": 1,
                    "id": _document_ulid(f"membench:rule:{rule_key}"),
                    "scope_ids": [scope_id],
                    "audience": audience,
                    "ceiling": 0,
                }

        for rule in policy.rules:
            if rule.declassify_at is not None and horizon >= rule.declassify_at:
                dropped.append(
                    {
                        "rule_id": rule.rule_id,
                        "declassify_at": rule.declassify_at.isoformat(),
                        "target_sources": sorted(rule.target_sources),
                        "target_claims": sorted(rule.target_claims),
                        "reason": (
                            "exomem policy v1 has no time-conditioned rules; the "
                            "translation snapshots the policy at the corpus "
                            "knowledge horizon, where this rule is declassified"
                        ),
                    }
                )
                continue
            restricted = {
                self._audience_id(persona.persona_id)
                for persona in policy.personas
                if not set(persona.audiences) & set(rule.allow)
            }
            add_scope(
                f"rule:{rule.rule_id}",
                f"membench {rule.rule_id}",
                self._rule_paths(rule, claims_by_id),
                restricted,
            )

        everyone = {
            self._audience_id(persona.persona_id) for persona in policy.personas
        }
        everyone.add(self._audience_id("owner"))
        for index, tombstone in enumerate(policy.tombstones):
            if horizon < tombstone.requested_at:
                continue
            paths = sorted(
                {
                    path
                    for sid in tombstone.target_sources
                    for path in self._source_paths.get(sid, [])
                }
            )
            add_scope(
                f"tombstone:{index}:{'+'.join(sorted(tombstone.target_sources))}",
                f"membench tombstone {index}",
                paths,
                set(everyone),
            )
        return documents, dropped

    def _write_translation_report(
        self, documents: dict[str, dict], dropped: list[dict], note: str | None
    ) -> None:
        """The wired-translation report — ALWAYS written for a wired run.

        Lands in the adapter workdir (``<run_dir>/provider/``); the runner
        surfaces it at the run root and joins ``dropped_rules`` onto the
        affected queries' gates.
        """

        if self._workdir is None:
            raise AdapterEnvironmentError("adapter not set up")
        payload = {
            "schema": "membench-governance-translation/v1",
            "provider": self.name,
            "governance": self.governance_state,
            "documents_authored": sorted(documents),
            "dropped_rules": dropped,
            "note": note,
        }
        (self._workdir / "governance-translation.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _wire_governance(self, corpus_dir: Path) -> None:
        """Author the vault's opt-in policy and let exomem's compiler judge it."""

        import yaml
        from pydantic import ValidationError

        from exomem.governance import policy as governance_policy_module

        policy_file = corpus_dir / "policies.yaml"
        if not policy_file.is_file():
            self._write_translation_report(
                {},
                [],
                "corpus has no policies.yaml; vault remains ungoverned (default-open)",
            )
            return
        try:
            policy = PolicySet.model_validate(
                yaml.safe_load(policy_file.read_text(encoding="utf-8")) or {}
            )
        except (yaml.YAMLError, ValidationError) as exc:
            # A wired run against an unreadable policy set must invalidate the
            # run — never crash raw and never measure silently ungoverned.
            raise AdapterEnvironmentError(
                f"malformed corpus policy set {policy_file}: {exc}"
            ) from exc
        claims_file = corpus_dir / "claims.jsonl"
        claims_by_id: dict[str, ClaimRecord] = {}
        if claims_file.is_file():
            claims_by_id = {c.claim_id: c for c in load_jsonl(ClaimRecord, claims_file)}
        documents, dropped = self._governance_documents(policy, claims_by_id)
        if not documents:
            self._write_translation_report(
                documents,
                dropped,
                "corpus policy set contains nothing translatable; vault remains "
                "ungoverned (default-open)",
            )
            return
        if self._vault is None:
            raise AdapterEnvironmentError("adapter not set up")
        root = governance_policy_module.governance_root(self._vault)
        for relative, document in sorted(documents.items()):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
            )
        compiled = governance_policy_module.load(self._vault)
        errors = [
            finding
            for finding in compiled.findings
            if finding.get("severity") == "error"
        ]
        if compiled.empty or compiled.blocked or errors:
            raise AdapterEnvironmentError(
                "authored governance policy failed to compile: "
                f"empty={compiled.empty} blocked={compiled.blocked} findings={errors!r}"
            )
        self._write_translation_report(documents, dropped, None)

    # -- search -----------------------------------------------------------
    def _search_kwargs(self) -> dict:
        base = dict(
            _NEUTRAL_SEARCH_KWARGS
            if self.search_style == "neutral"
            else _PRODUCT_DEFAULT_SEARCH_KWARGS
        )
        # Always hybrid: exomem's `keyword` mode is strict phrase-substring
        # matching, so natural-language questions never match (the root cause
        # of the historical Track-A zero-hits run). Hybrid's BM25 lane is
        # tokenized and degrades cleanly when embeddings are disabled.
        base["mode"] = "hybrid"
        return base

    @staticmethod
    def _hit_field(hit: object, key: str) -> object:
        if isinstance(hit, dict):
            return hit.get(key)
        return getattr(hit, key, None)

    def _normalize(self, payload: object) -> list[object]:
        if isinstance(payload, dict):
            for marker in ("warming", "degraded"):
                value = payload.get(marker)
                if value:
                    raise AdapterEnvironmentError(
                        f"scored response carries {marker} marker: {value!r}"
                    )
            for key in ("hits", "result", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        if isinstance(payload, list):
            return payload
        for marker in ("warming", "degraded"):
            value = getattr(payload, marker, None)
            if value:
                raise AdapterEnvironmentError(
                    f"scored response carries {marker} marker: {value!r}"
                )
        hits = getattr(payload, "hits", None)
        return list(hits) if hits is not None else []

    def _ask(
        self, query: str, limit: int, *, persona: str | None = None, deep: bool = False
    ) -> object:
        """One dispatch for every exomem ask: wire, governed, or plain.

        Extracted so `search` and `answer` cannot drift apart on the governance
        binding — a native answer that skipped `request_scope` would read the
        ungoverned vault while claiming a persona, which is the exact failure
        the wired-vs-default-open contract exists to prevent.
        """

        kwargs = self._search_kwargs()
        if deep:
            kwargs["deep"] = True
        if self.mode == "wire":
            return self._call_tool("ask_memory", {"query": query, "limit": limit, **kwargs})
        from exomem import commands

        if self.governance == "wired":
            from exomem.governance import principal as principal_module

            # The surface-boundary binding every exomem surface uses: resolve
            # the canonical principal once, bind it for exactly this call.
            with principal_module.request_scope(self._principal_for(persona)):
                return commands.op_ask_memory(self._vault, query=query, limit=limit, **kwargs)
        return commands.op_ask_memory(self._vault, query=query, limit=limit, **kwargs)

    def search(self, query: str, limit: int, *, persona: str | None = None) -> list[Hit]:
        """Search the vault; ``persona`` is honored only under active wiring.

        Without wiring the vault is the explicitly ungoverned default-open
        surface: a persona has no effect there by definition, and the call
        path is byte-identical to the pre-governance adapter.
        """

        payload = self._ask(query, limit, persona=persona)
        hits: list[Hit] = []
        for rank, raw_hit in enumerate(self._normalize(payload), start=1):
            path = self._hit_field(raw_hit, "path")
            if not isinstance(path, str):
                continue
            text = ""
            candidate = (self._vault or Path(".")) / path
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
            excerpt = self._hit_field(raw_hit, "excerpt")
            title = self._hit_field(raw_hit, "title")
            raw = raw_hit if isinstance(raw_hit, dict) else {"path": path, "title": title}
            hits.append(
                Hit(
                    rank=rank,
                    provider_path=path,
                    title=title if isinstance(title, str) else None,
                    excerpt=excerpt if isinstance(excerpt, str) else None,
                    sentinels=tuple(sentinels_in(text or (excerpt or ""))),
                    raw=raw,
                    text=text or None,
                )
            )
        return hits

    def _declared_basis(self, path: str) -> list[str]:
        """Source ids a compiled note DECLARES as its basis.

        This is the measurement the compiled altitude exists for. At raw-source
        altitude citations could only be scraped from quoted text, which scores
        which documents were read; here the note states what it was drawn from
        in `sources:` frontmatter, which is what the system claims — and the two
        are different questions.
        """

        if self._vault is None:
            return []
        note = self._vault / path
        if not note.is_file():
            return []
        text = note.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return []
        end = text.find("\n---", 3)
        front = text[3 : end if end != -1 else len(text)]
        basis: list[str] = []
        for raw in re.findall(r"\[\[([^\]]+)\]\]", front):
            source_id = self._path_to_source.get(raw.strip().lstrip("/"))
            if source_id and source_id not in basis:
                basis.append(source_id)
        return basis

    # -- native answer -----------------------------------------------------
    def answer(self, query: str, limit: int, *, persona: str | None = None) -> NativeAnswer:
        """Answer from exomem's own context pack, citing what the pack selected.

        ``ask_memory(deep=True)`` returns a packed reasoning context whose
        ``packed_paths`` are the pages exomem chose to reason over — a strict
        narrowing of the raw hit list, and the product's own statement about
        what its answer rests on. Citing those instead of a harness-chosen
        top-3 is the whole point of the native seam: it measures exomem's
        selection, not `extractive.py`'s cut.

        Hedging is taken from the pack's ``contradictions`` rather than from
        prose. exomem never emits hedging *language* — it has no generative
        step — so a prose-based calibration gate can only ever score zero
        against it (task 4b.33). What it does have is contradiction detection,
        and a pack that surfaces a contradiction IS the system expressing
        uncertainty, structurally.

        Abstention is honest and may well be unflattering: exomem returns
        something for nearly every query, so if it declines to abstain where
        the corpus requires it, that is now a product finding rather than an
        artifact of the harness abstaining only on zero hits.
        """

        payload = self._ask(query, limit, persona=persona, deep=True)
        hits = self._normalize(payload)
        pack = payload.get("pack") if isinstance(payload, dict) else None
        packed_paths: list[str] = []
        contradictions: list = []
        if isinstance(pack, dict):
            raw_paths = pack.get("packed_paths")
            if isinstance(raw_paths, list):
                packed_paths = [p for p in raw_paths if isinstance(p, str)]
            raw_contra = pack.get("contradictions")
            if isinstance(raw_contra, list):
                contradictions = raw_contra

        # A result list without a pack is still exomem's own claim about what
        # it returned, so the citation basis stays the product's rather than
        # the harness's — but which basis was used is recorded below, because
        # "the pack selected these" and "everything it retrieved" are different
        # precision claims and must never be silently interchangeable.
        basis = "pack" if packed_paths else "hits"
        if not packed_paths:
            packed_paths = [
                path
                for path in (self._hit_field(hit, "path") for hit in hits)
                if isinstance(path, str)
            ]

        citations: list[str] = []
        chunks: list[str] = []
        compiled = self.altitude == "compiled"
        for path in packed_paths:
            if compiled:
                for source_id in self._declared_basis(path):
                    if source_id not in citations:
                        citations.append(source_id)
            candidate = (self._vault or Path(".")) / path
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            chunks.append(text[:_ANSWER_CHARS_PER_SOURCE])
            if compiled:
                # The note's declared basis is the claim; scraping sentinels out
                # of quoted text would put back the raw-altitude proxy and
                # measure reading rather than attribution.
                continue
            for token in sentinels_in(text):
                if token not in citations:
                    citations.append(token)

        if not packed_paths:
            return NativeAnswer(
                text="",
                citations=(),
                abstained=True,
                raw={"reason": "no packed paths and no hits"},
            )
        return NativeAnswer(
            text="\n\n".join(chunks),
            citations=tuple(citations),
            abstained=False,
            # Structural, not prose: the pack found conflicting material.
            hedged=bool(contradictions) or None,
            raw={
                "citation_basis": basis,
                "packed_paths": packed_paths,
                "contradiction_count": len(contradictions),
                "hits": len(hits),
            },
        )

    # -- state export ------------------------------------------------------
    def export_state(self) -> StateExport:
        if self._vault is None:
            raise AdapterEnvironmentError("adapter not set up")
        pages = []
        for path in sorted(self._vault.rglob("*.md")):
            relative = path.relative_to(self._vault).as_posix()
            pages.append(
                StateExportPage(
                    path=relative, text=path.read_text(encoding="utf-8", errors="replace")
                )
            )
        return StateExport(pages=tuple(pages))

    def version_info(self) -> dict[str, str]:
        import exomem

        info = {
            "provider": self.name,
            "mode": self.mode,
            "search_style": self.search_style,
            "exomem_version": getattr(exomem, "__version__", "unknown"),
        }
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("exomem-local", lambda **kw: ExomemLocalAdapter(**kw))
