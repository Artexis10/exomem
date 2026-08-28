"""Crash-safe owner-only storage for trusted consolidation review progress."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from .. import reserved_paths
from . import consolidation_plan, consolidation_plan_store, consolidation_review

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_SNAPSHOT_SCHEMA = "exomem.consolidation-render-snapshot/v1"
_ACTIVE_SCHEMA = "exomem.consolidation-render-active/v1"
_SNAPSHOT_DOMAIN = _SNAPSHOT_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_PLAN_KINDS = frozenset({"cutover", "retirement", "rollback"})
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_ACTIVE_BYTES = 4 * 1024
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema",
        "render_session_digest",
        "state_digest",
        "session",
        "state",
        "acknowledgements",
        "has_completeness",
        "completeness",
    }
)
_ACTIVE_FIELDS = frozenset(
    {
        "schema",
        "render_session_digest",
        "state_digest",
        "snapshot_digest",
    }
)


class ConsolidationReviewStoreUnavailable(RuntimeError):
    """Content-free refusal for stale, missing, or corrupt review storage."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ConsolidationReviewStoreUnavailable(code) from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("REVIEW_STORE_INPUT_INVALID")
    return value


def _run_id(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail("REVIEW_STORE_INPUT_INVALID")
    return value


def _plan_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _PLAN_KINDS:
        _fail("REVIEW_STORE_INPUT_INVALID")
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail("REVIEW_STORE_CORRUPT")
    return value


def _snapshot_digest(raw: bytes) -> str:
    framed = (
        len(_SNAPSHOT_DOMAIN).to_bytes(4, "big")
        + _SNAPSHOT_DOMAIN
        + len(raw).to_bytes(8, "big")
        + raw
    )
    return hashlib.sha256(framed).hexdigest()


def _exact_mapping(raw: bytes, *, maximum: int) -> Mapping[str, object]:
    try:
        parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
            raw,
            maximum=maximum,
        )
        if consolidation_plan.canonical_closed_jcs(parsed) != raw:
            _fail("REVIEW_STORE_CORRUPT")
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail("REVIEW_STORE_CORRUPT")
    return parsed


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationReviewStoreUnavailable:
        raise
    except (
        OSError,
        RuntimeError,
        ValueError,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_review.ConsolidationReviewUnavailable,
    ):
        _fail("REVIEW_STORE_UNAVAILABLE")


class ConsolidationReviewStore:
    """Persist immutable review snapshots behind one CAS active pointer."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).absolute()
        self.plan_store = consolidation_plan_store.ConsolidationPlanStore(self.vault_root)

    def _review_dir(
        self,
        run_id: str,
        plan_kind: str,
        plan_digest: str,
        render_session_digest: str,
    ) -> Path:
        return (
            self.plan_store._plan_dir(  # noqa: SLF001
                run_id,
                plan_kind,
                plan_digest,
            )
            / "reviews"
            / render_session_digest
        )

    def _read_optional(self, path: Path, *, limit: int) -> bytes | None:
        try:
            return reserved_paths._read_owner_bytes(  # noqa: SLF001
                self.vault_root,
                path,
                _DESCRIPTOR_ID,
                limit=limit,
            )
        except FileNotFoundError:
            return None

    def _publish_missing(self, path: Path, raw: bytes) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            raw,
            require_missing=True,
        )

    def _publish_active(
        self,
        path: Path,
        raw: bytes,
        *,
        expected_sha256: str | None,
    ) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            raw,
            expected_sha256=expected_sha256,
            require_missing=expected_sha256 is None,
        )

    @staticmethod
    def _snapshot_bytes(review: consolidation_review.CanonicalRenderReview) -> bytes:
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _SNAPSHOT_SCHEMA,
                "render_session_digest": review.session.digest,
                "state_digest": review.state.digest,
                "session": review.session.preimage,
                "state": review.state.preimage,
                "acknowledgements": tuple(
                    acknowledgement.preimage for acknowledgement in review.acknowledgements
                ),
                "has_completeness": review.completeness is not None,
                "completeness": (
                    {} if review.completeness is None else review.completeness.preimage
                ),
            }
        )

    @staticmethod
    def _active_bytes(
        *,
        render_session_digest: str,
        state_digest: str,
        snapshot_digest: str,
    ) -> bytes:
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _ACTIVE_SCHEMA,
                "render_session_digest": render_session_digest,
                "state_digest": state_digest,
                "snapshot_digest": snapshot_digest,
            }
        )

    @staticmethod
    def _restore(
        raw: bytes,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        expected_session_digest: str,
        expected_state_digest: str,
        expected_snapshot_digest: str,
    ) -> consolidation_review.CanonicalRenderReview:
        if _snapshot_digest(raw) != expected_snapshot_digest:
            _fail("REVIEW_STORE_CORRUPT")
        snapshot = _mapping(
            _exact_mapping(raw, maximum=_MAX_SNAPSHOT_BYTES),
            _SNAPSHOT_FIELDS,
        )
        if (
            snapshot["schema"] != _SNAPSHOT_SCHEMA
            or snapshot["render_session_digest"] != expected_session_digest
            or snapshot["state_digest"] != expected_state_digest
        ):
            _fail("REVIEW_STORE_CORRUPT")
        acknowledgements = snapshot["acknowledgements"]
        if isinstance(acknowledgements, (str, bytes)) or not isinstance(acknowledgements, Sequence):
            _fail("REVIEW_STORE_CORRUPT")
        has_completeness = snapshot["has_completeness"]
        if type(has_completeness) is not bool:
            _fail("REVIEW_STORE_CORRUPT")
        completeness = snapshot["completeness"]
        if not isinstance(completeness, Mapping):
            _fail("REVIEW_STORE_CORRUPT")
        if (has_completeness and not completeness) or (not has_completeness and completeness):
            _fail("REVIEW_STORE_CORRUPT")
        session = snapshot["session"]
        state = snapshot["state"]
        if not isinstance(session, Mapping) or not isinstance(state, Mapping):
            _fail("REVIEW_STORE_CORRUPT")
        acknowledgement_mappings: list[Mapping[str, object]] = []
        for item in acknowledgements:
            if not isinstance(item, Mapping):
                _fail("REVIEW_STORE_CORRUPT")
            acknowledgement_mappings.append(item)
        try:
            review = consolidation_review.restore_review(
                plan=plan,
                session=_mapping(session, frozenset(session)),
                state=_mapping(state, frozenset(state)),
                acknowledgements=tuple(
                    _mapping(item, frozenset(item)) for item in acknowledgement_mappings
                ),
                completeness=(
                    _mapping(completeness, frozenset(completeness)) if has_completeness else None
                ),
            )
        except (TypeError, consolidation_review.ConsolidationReviewUnavailable):
            _fail("REVIEW_STORE_CORRUPT")
        if (
            review.session.digest != expected_session_digest
            or review.state.digest != expected_state_digest
        ):
            _fail("REVIEW_STORE_CORRUPT")
        return review

    def _load_locked(
        self,
        directory: Path,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        expected_session_digest: str,
    ) -> tuple[consolidation_review.CanonicalRenderReview, bytes]:
        active_raw = self._read_optional(
            directory / "active.json",
            limit=_MAX_ACTIVE_BYTES,
        )
        if active_raw is None:
            _fail("REVIEW_NOT_FOUND")
        active = _mapping(
            _exact_mapping(active_raw, maximum=_MAX_ACTIVE_BYTES),
            _ACTIVE_FIELDS,
        )
        if (
            active["schema"] != _ACTIVE_SCHEMA
            or active["render_session_digest"] != expected_session_digest
        ):
            _fail("REVIEW_STORE_CORRUPT")
        state_digest = _digest(active["state_digest"])
        snapshot_digest = _digest(active["snapshot_digest"])
        snapshot_raw = self._read_optional(
            directory / "snapshots" / f"{state_digest}.json",
            limit=_MAX_SNAPSHOT_BYTES,
        )
        if snapshot_raw is None:
            _fail("REVIEW_STORE_CORRUPT")
        return (
            self._restore(
                snapshot_raw,
                plan=plan,
                expected_session_digest=expected_session_digest,
                expected_state_digest=state_digest,
                expected_snapshot_digest=snapshot_digest,
            ),
            active_raw,
        )

    @staticmethod
    def _initial_review(
        review: consolidation_review.CanonicalRenderReview,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
    ) -> bool:
        session = review.session.preimage
        identity = consolidation_review.TrustedRenderIdentity(
            owner_binding_digest=str(session["owner_binding_digest"]),
            owner_principal_digest=str(session["owner_principal_digest"]),
            authorization_session_digest=str(session["authorization_session_digest"]),
            issuer=str(session["issuer"]),
            surface=str(session["surface"]),
        )
        try:
            expected = consolidation_review.begin_review(
                plan,
                identity=identity,
                issued_at=str(session["issued_at"]),
                expires_at=str(session["expires_at"]),
                nonce=str(session["nonce"]),
            )
        except consolidation_review.ConsolidationReviewUnavailable:
            return False
        return expected == review

    @staticmethod
    def _valid_successor(
        current: consolidation_review.CanonicalRenderReview,
        target: consolidation_review.CanonicalRenderReview,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
    ) -> bool:
        if current.session != target.session or current.completeness is not None:
            return False
        state = current.state.preimage
        session = current.session.preimage
        next_page_ordinal = state["next_page_ordinal"]
        page_count = session["page_count"]
        if type(next_page_ordinal) is not int or type(page_count) is not int:
            return False
        identity = consolidation_review.TrustedRenderIdentity(
            owner_binding_digest=str(session["owner_binding_digest"]),
            owner_principal_digest=str(session["owner_principal_digest"]),
            authorization_session_digest=str(session["authorization_session_digest"]),
            issuer=str(session["issuer"]),
            surface=str(session["surface"]),
        )
        try:
            if state["pending"]:
                if len(target.acknowledgements) != len(current.acknowledgements) + 1:
                    return False
                expected = consolidation_review.acknowledge_page(
                    current,
                    plan=plan,
                    acknowledgement=target.acknowledgements[-1],
                )
            elif next_page_ordinal < page_count:
                expected, _page = consolidation_review.serve_page(
                    current,
                    plan=plan,
                    identity=identity,
                    page_ordinal=next_page_ordinal,
                    served_at=str(session["issued_at"]),
                )
            elif target.completeness is not None:
                completeness = target.completeness.preimage
                expected, _artifact = consolidation_review.complete_review(
                    current,
                    plan=plan,
                    identity=identity,
                    issued_at=str(completeness["issued_at"]),
                    expires_at=str(completeness["expires_at"]),
                    nonce=str(completeness["nonce"]),
                )
            else:
                return False
        except (
            KeyError,
            TypeError,
            ValueError,
            consolidation_review.ConsolidationReviewUnavailable,
        ):
            return False
        return expected == target

    def _load_plan(
        self,
        run_id: str,
        *,
        plan_kind: str,
        plan_digest: str,
    ) -> consolidation_plan.CanonicalConsolidationPlan:
        try:
            return self.plan_store.load(
                run_id,
                plan_kind=plan_kind,
                plan_digest=plan_digest,
            )
        except consolidation_plan_store.ConsolidationPlanStoreUnavailable:
            _fail("REVIEW_STORE_UNAVAILABLE")

    def persist(
        self,
        review: consolidation_review.CanonicalRenderReview,
        *,
        plan: consolidation_plan.CanonicalConsolidationPlan,
        expected_state_digest: str | None,
    ) -> consolidation_review.CanonicalRenderReview:
        """Create, advance, or byte-identically adopt one review session."""

        try:
            review = consolidation_review.validate_review(review, plan=plan)
        except consolidation_review.ConsolidationReviewUnavailable:
            _fail("REVIEW_STORE_INPUT_INVALID")
        session = review.session.preimage
        run_id = _run_id(session["run_id"])
        kind = _plan_kind(session["plan_kind"])
        plan_digest = _digest(session["plan_digest"])
        session_digest = _digest(review.session.digest)
        if expected_state_digest is not None:
            expected_state_digest = _digest(expected_state_digest)
        stored_plan = self._load_plan(
            run_id,
            plan_kind=kind,
            plan_digest=plan_digest,
        )
        if stored_plan != plan:
            _fail("REVIEW_PLAN_MISMATCH")
        directory = self._review_dir(run_id, kind, plan_digest, session_digest)
        snapshot_raw = self._snapshot_bytes(review)
        snapshot_digest = _snapshot_digest(snapshot_raw)
        active_raw = self._active_bytes(
            render_session_digest=session_digest,
            state_digest=review.state.digest,
            snapshot_digest=snapshot_digest,
        )

        with _authority(self.vault_root, mutation=True):
            try:
                current, current_active_raw = self._load_locked(
                    directory,
                    plan=plan,
                    expected_session_digest=session_digest,
                )
            except ConsolidationReviewStoreUnavailable as error:
                if error.code != "REVIEW_NOT_FOUND":
                    raise
                current = None
                current_active_raw = None
            if current == review:
                return review
            if current is None:
                if expected_state_digest is not None or not self._initial_review(
                    review,
                    plan=plan,
                ):
                    _fail("REVIEW_STATE_CONFLICT")
            else:
                if expected_state_digest != current.state.digest or not self._valid_successor(
                    current, review, plan=plan
                ):
                    _fail("REVIEW_STATE_CONFLICT")

            snapshot_path = directory / "snapshots" / f"{review.state.digest}.json"
            existing_snapshot = self._read_optional(
                snapshot_path,
                limit=_MAX_SNAPSHOT_BYTES,
            )
            if existing_snapshot is None:
                self._publish_missing(snapshot_path, snapshot_raw)
            elif existing_snapshot != snapshot_raw:
                _fail("REVIEW_STORE_CONFLICT")
            self._publish_active(
                directory / "active.json",
                active_raw,
                expected_sha256=(
                    hashlib.sha256(current_active_raw).hexdigest()
                    if current_active_raw is not None
                    else None
                ),
            )
        return review

    def load(
        self,
        run_id: str,
        *,
        plan_kind: str,
        plan_digest: str,
        render_session_digest: str,
    ) -> consolidation_review.CanonicalRenderReview:
        """Load the one current exact snapshot for a trusted review session."""

        run_id = _run_id(run_id)
        kind = _plan_kind(plan_kind)
        plan_digest = _digest(plan_digest)
        session_digest = _digest(render_session_digest)
        plan = self._load_plan(
            run_id,
            plan_kind=kind,
            plan_digest=plan_digest,
        )
        directory = self._review_dir(run_id, kind, plan_digest, session_digest)
        with _authority(self.vault_root, mutation=False):
            review, _active_raw = self._load_locked(
                directory,
                plan=plan,
                expected_session_digest=session_digest,
            )
        return review


__all__ = ["ConsolidationReviewStore", "ConsolidationReviewStoreUnavailable"]
