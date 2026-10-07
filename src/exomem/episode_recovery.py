"""Principal-bound recovery of bounded episode input evidence.

This is an internal owner facade.  It neither captures conversational material
nor exposes episode state beyond the small operational projection below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import curation, provenance, retained_inputs
from . import episode_model as model
from .episode_store import EpisodeStore
from .get_page import GetResult
from .governance import egress
from .governance.principal import effective_principal
from .vault import PathGuard

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 8192


def _error(code: str, reason: str) -> model.EpisodeError:
    return model.EpisodeError(code, reason)


def _retained_error(error: retained_inputs.RetainedInputError) -> model.EpisodeError:
    return _error(error.code.replace("RETAINED_", "EPISODE_", 1), error.reason)


@dataclass(frozen=True)
class _ResolvedInput:
    evidence: dict[str, str]
    body: str | None = None
    text: str | None = None
    authorization: dict[str, Any] | None = None


@dataclass(frozen=True)
class EpisodeRootProof:
    """An exact owned journal binding, not input release permission."""

    root: str
    guard: PathGuard


class EpisodeInputOwner:
    """Store and recover source input under the currently resolved audience."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)

    def _owner(self) -> str:
        principal = effective_principal()
        audience = principal.audience_id
        if (
            not principal.resolved
            or not isinstance(audience, str)
            or not audience.strip()
            or "\x00" in audience
        ):
            raise _error("EPISODE_OWNER_UNRESOLVED", "episode owner is unavailable")
        return audience

    def _store(self) -> EpisodeStore:
        return EpisodeStore(self.vault_root, owner_audience_id=self._owner())

    def prove_original(self, resolved: retained_inputs.RetainedInput) -> EpisodeRootProof | None:
        """Prove a recap's whole parent against only this audience's journal."""
        page = resolved.page
        if (
            resolved.released is None
            or resolved.guard is None
            or str(page.frontmatter.get("type") or "").casefold() != "source"
            or str(page.frontmatter.get("source_type") or "").casefold() != "episode"
        ):
            return None
        try:
            store = self._store()
            identity = model.episode_id(page.frontmatter.get("episode"))
            with store._guard():
                current, guard = store.read_guarded(identity)
                for revision in current["state"]["input_revisions"]:
                    evidence = revision["evidence"]
                    if evidence.get("reference") != resolved.canonical:
                        continue
                    expected = self._page_evidence(
                        page, resolved.canonical, evidence.get("version_scheme")
                    )
                    if evidence.get("digest") == expected["digest"]:
                        guard.recheck(self.vault_root)
                        return EpisodeRootProof(
                            model._hash("exomem-episode-origin-v1", store.owner_audience_id, identity),
                            guard,
                        )
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _projection(current: dict[str, Any]) -> dict[str, Any]:
        state = current["state"]
        return {
            "episode_id": state["episode_id"],
            "revision": current["revision"],
            "journal_digest": current["journal_digest"],
            "input_revision": state["input_revisions"][-1]["revision"],
            "coverage_current": "unchecked",
        }

    @staticmethod
    def _reference(value: Any) -> tuple[str, str | None]:
        try:
            return retained_inputs.parse_reference(value)
        except retained_inputs.RetainedInputError as error:
            raise _retained_error(error) from error

    @staticmethod
    def _page_evidence(
        page: GetResult, canonical: str, version_scheme: str | None,
        *, unit_ref: str | None = None, unit_fingerprint: str | None = None,
    ) -> dict[str, str]:
        material = version_scheme == model.MATERIAL_EVIDENCE_SCHEME
        try:
            version = provenance.evidence_version(page.content) if material else page.content_hash
        except ValueError as error:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable") from error
        evidence = {
            "reference": unit_ref or canonical,
            "digest": (
                model._hash("exomem-episode-input-page-v1", canonical, version)
                if unit_ref is None else model._hash(
                    "exomem-episode-input-unit-v1", canonical, version, unit_ref, unit_fingerprint
                )
            ),
        }
        if material:
            evidence["version_scheme"] = model.MATERIAL_EVIDENCE_SCHEME
        return evidence

    def _resolve_reference(
        self, reference: Any, *, version_scheme: str | None = None, new_input: bool = False
    ) -> _ResolvedInput:
        try:
            resolved = retained_inputs.resolve_retained_input(self.vault_root, reference)
        except retained_inputs.RetainedInputError as error:
            raise _retained_error(error) from error
        page = resolved.page
        if new_input and str(page.frontmatter.get("type") or "").casefold() in {
            "source",
            "evidence",
        }:
            version_scheme = model.MATERIAL_EVIDENCE_SCHEME
        if (
            version_scheme == model.MATERIAL_EVIDENCE_SCHEME
            and resolved.released.get("frontmatter") != page.frontmatter
        ):
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        return _ResolvedInput(
            evidence=self._page_evidence(
                page,
                resolved.canonical,
                version_scheme,
                unit_ref=resolved.unit_ref,
                unit_fingerprint=resolved.unit.fingerprint if resolved.unit else None,
            ),
            body=page.body if resolved.unit is None else None,
            text=resolved.unit.span.text if resolved.unit else None,
            authorization=resolved.authorization,
        )

    def _evidence(
        self, *, reference: str | None, input_digest: str | None
    ) -> dict[str, str]:
        if (reference is None) == (input_digest is None):
            raise _error("EPISODE_INPUT_INVALID", "supply exactly one input representation")
        if input_digest is not None:
            if not isinstance(input_digest, str) or not _DIGEST.fullmatch(input_digest):
                raise _error("EPISODE_INPUT_INVALID", "input digest is invalid")
            return {"digest": input_digest}
        assert reference is not None
        return self._resolve_reference(reference, new_input=True).evidence

    def create(
        self, key: str, *, reference: str | None = None, input_digest: str | None = None
    ) -> dict[str, Any]:
        store = self._store()
        with store._guard():
            current = store.create(key, self._evidence(reference=reference, input_digest=input_digest))
        return self._projection(current)

    def _committed_evidence(self, canonical: str, path: str) -> dict[str, str]:
        """Input evidence for the page the Source writer just committed at `path`.

        The writer's receipt names the path, so the ref is checked AT that path
        rather than resolved through `egress.resolve_visible_identifier`, which
        walks the whole corpus by design. One page read, one release decision
        on that page alone. A page the recorder may not read back binds by
        digest only: the write already happened, and answering it with an
        error would invite a retry that writes nothing new.
        """
        try:
            snapshot = retained_inputs._read_snapshot(  # noqa: SLF001
                self.vault_root, canonical, committed_path=path
            )
        except retained_inputs.RetainedInputError as error:
            raise _retained_error(error) from error
        page = snapshot.page
        digest = model._hash("exomem-episode-input-page-v1", canonical, page.content_hash)
        version_scheme = (
            model.MATERIAL_EVIDENCE_SCHEME
            if str(page.frontmatter.get("type") or "").casefold() in {"source", "evidence"}
            else None
        )
        try:
            released = retained_inputs._release_snapshot(self.vault_root, snapshot)  # noqa: SLF001
            if (
                version_scheme == model.MATERIAL_EVIDENCE_SCHEME
                and released.released.get("frontmatter") != page.frontmatter
            ):
                return {"digest": digest}
            return self._page_evidence(page, canonical, version_scheme)
        except (retained_inputs.RetainedInputError, model.EpisodeError):
            return {"digest": digest}

    def bind_committed_input(
        self, key: str, *, path: str, reference: str, about: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Bind a just-committed page as the next input revision of episode `key`.

        `about` is the refs the input concerns, already visibility-filtered for
        this caller; they are retained here, in this audience's own ledger,
        rather than on the page every audience may read.

        Creates the episode on first use and appends a revision after that. A
        byte-equivalent input (`EPISODE_REVISION_UNCHANGED`) is success, so a
        retried record binds once; a concurrent writer's revision
        (`EPISODE_REVISION_CONFLICT`) is reloaded and retried once. The owner
        is resolved before the page is read, and every audience keeps its own
        history of a shared key.
        """
        store = self._store()
        canonical, unit_ref = self._reference(reference)
        if unit_ref is not None:
            raise _error("EPISODE_INPUT_INVALID", "a committed input is a whole page")
        evidence: dict[str, Any] = self._committed_evidence(canonical, path)
        if about:
            evidence["about"] = list(about)
        identity = model.episode_id(key)
        for attempt in range(2):
            with store._guard():
                try:
                    current = store.read(identity)
                except curation.CurationError as error:
                    if error.code != "CURATION_RUN_NOT_FOUND":
                        raise
                    current = store.create(key, evidence)
                    break
                try:
                    current = store.transition(
                        identity,
                        expected_revision=current["revision"],
                        expected_digest=current["journal_digest"],
                        action="append_input_revision",
                        args={"input_evidence": evidence},
                    )
                except model.EpisodeError as error:
                    if error.code == "EPISODE_REVISION_CONFLICT" and attempt == 0:
                        continue
                    if error.code != "EPISODE_REVISION_UNCHANGED":
                        raise
                break
        return {
            **self._projection(current),
            "ledger": "bound" if "reference" in evidence else "digest_only",
            "recovery": "available" if "reference" in evidence else "unavailable",
        }

    def append_input(
        self,
        episode_id: str,
        *,
        expected_revision: int,
        expected_digest: str,
        reference: str | None = None,
        input_digest: str | None = None,
    ) -> dict[str, Any]:
        store = self._store()
        try:
            with store._guard():
                current = store.transition(
                    episode_id,
                    expected_revision=expected_revision,
                    expected_digest=expected_digest,
                    action="append_input_revision",
                    args={"input_evidence": self._evidence(reference=reference, input_digest=input_digest)},
                )
        except curation.CurationError as error:
            if error.code == "CURATION_RUN_NOT_FOUND":
                raise _error("EPISODE_NOT_FOUND", "episode is unavailable") from error
            raise
        return self._projection(current)

    def inspect(self, episode_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            return self._projection(store.read(episode_id))
        except curation.CurationError as error:
            if error.code == "CURATION_RUN_NOT_FOUND":
                raise _error("EPISODE_NOT_FOUND", "episode is unavailable") from error
            raise

    def input_history(self, key: str) -> dict[str, Any]:
        """Each input revision's recorded recovery class, and the latest input ref.

        Keyed by the episode key rather than its identity, for a caller that
        holds only the key. The recovery class is what the bind recorded, not
        a current release decision; `recover_input` makes that one.
        """
        store = self._store()
        try:
            current = store.read(model.episode_id(key))
        except curation.CurationError as error:
            if error.code == "CURATION_RUN_NOT_FOUND":
                raise _error("EPISODE_NOT_FOUND", "episode is unavailable") from error
            raise
        revisions = current["state"]["input_revisions"]
        return {
            "episode_id": current["state"]["episode_id"],
            "revisions": [
                {"revision": item["revision"], "recovery": item["recovery"]}
                for item in revisions
            ],
            "latest_reference": revisions[-1]["evidence"].get("reference"),
        }

    def recover_input(
        self, episode_id: str, *, input_revision: int | None = None
    ) -> dict[str, Any]:
        if input_revision is not None and (
            type(input_revision) is not int or input_revision < 1
        ):
            raise _error("EPISODE_INPUT_INVALID", "input revision is invalid")
        store = self._store()
        try:
            with store._guard():
                try:
                    current = store.read(episode_id)
                except curation.CurationError as error:
                    if error.code == "CURATION_RUN_NOT_FOUND":
                        raise _error("EPISODE_NOT_FOUND", "episode is unavailable") from error
                    raise
                revisions = current["state"]["input_revisions"]
                revision = revisions[-1] if input_revision is None else next(
                    item for item in revisions if item["revision"] == input_revision
                )
                evidence = revision["evidence"]
                if "reference" not in evidence:
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                try:
                    resolved = self._resolve_reference(
                        evidence["reference"], version_scheme=evidence.get("version_scheme")
                    )
                except model.EpisodeError:
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                if resolved.evidence["digest"] != evidence.get("digest"):
                    return {"status": "stale", "input_revision": revision["revision"]}
                field, value = ("body", resolved.body) if resolved.body is not None else ("text", resolved.text)
                if value is None or len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                if not retained_inputs.exact_text_visible(self.vault_root, value):
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                representation = (
                    "page_body" if field == "body" else "semantic_unit_span"
                )
                egress.record_direct_text_release(
                    value,
                    stable_ref=evidence["reference"],
                    representation=representation,
                    principal=effective_principal(),
                    authorization=resolved.authorization,
                )
                return {
                    "status": "available",
                    "input_revision": revision["revision"],
                    "reference": evidence["reference"],
                    "digest": evidence["digest"],
                    "representation": representation,
                    field: value,
                }
        except (StopIteration, KeyError, TypeError, model.EpisodeError) as error:
            if isinstance(error, model.EpisodeError) and error.code in {
                "EPISODE_OWNER_UNRESOLVED",
                "EPISODE_NOT_FOUND",
                "EPISODE_ID_INVALID",
                "EPISODE_JOURNAL_INVALID",
            }:
                raise
            return {"status": "unavailable", "input_revision": input_revision or 0}
