"""Principal-bound recovery of bounded episode input evidence.

This is an internal owner facade.  It neither captures conversational material
nor exposes episode state beyond the small operational projection below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import curation, memory_refs, semantic_unit_read
from . import episode_model as model
from .episode_store import EpisodeStore
from .get_page import GetError, get_page
from .governance import egress
from .governance.principal import effective_principal

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 8192


def _error(code: str, reason: str) -> model.EpisodeError:
    return model.EpisodeError(code, reason)


@dataclass(frozen=True)
class _ResolvedInput:
    evidence: dict[str, str]
    body: str | None = None
    text: str | None = None
    authorization: dict[str, Any] | None = None


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
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise _error("EPISODE_INPUT_INVALID", "input reference is invalid")
        parent, marker, fragment = value.partition("#")
        canonical_id = memory_refs.parse_memory_ref(parent)
        canonical = memory_refs.memory_ref(canonical_id) if canonical_id is not None else None
        if (
            canonical is None
            or parent != canonical
            or (marker and (not fragment or "#" in fragment))
        ):
            raise _error("EPISODE_INPUT_INVALID", "input reference is invalid")
        return canonical, f"{canonical}#{fragment}" if marker else None

    def _resolve_reference(self, reference: Any) -> _ResolvedInput:
        # The source snapshot is authorization evidence.  It is retained only
        # in the nested collector; this facade records a receipt later for the
        # exact body or unit span that it actually returns.
        canonical, _unit_ref = self._reference(reference)
        with egress.disclosure_boundary(
            self.vault_root, "episode-input-authorization"
        ) as collector:
            resolved = self._resolve_reference_authorized(reference)
        authorization = [
            outcome.value
            for outcome in collector.outcomes
            if outcome.value.get("decision") == "released"
            and outcome.value.get("ref") == canonical
        ]
        if len(authorization) > 1:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        return _ResolvedInput(
            evidence=resolved.evidence,
            body=resolved.body,
            text=resolved.text,
            authorization=authorization[0] if authorization else None,
        )

    def _resolve_reference_authorized(self, reference: Any) -> _ResolvedInput:
        canonical, unit_ref = self._reference(reference)
        principal = effective_principal()
        try:
            path = egress.resolve_visible_identifier(
                self.vault_root, canonical, principal=principal
            )
            page = get_page(self.vault_root, path=path)
        except (memory_refs.ReferenceError, GetError, OSError, ValueError) as error:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable") from error
        if (
            memory_refs.ref_from_markdown(page.content) != canonical
            or str(page.frontmatter.get("status") or "").casefold() == "superseded"
        ):
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        released = egress.annotate_page(
            self.vault_root,
            {
                "path": page.path,
                "frontmatter": page.frontmatter,
                "body": page.body,
                "content": page.content,
                "content_hash": page.content_hash,
                "mtime": page.mtime,
            },
            principal=principal,
            snapshot_content=page.content,
            stable_ref=canonical,
        )
        if (
            released is None
            or released.get("content_hash") != page.content_hash
            or released.get("body") != page.body
        ):
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        try:
            current = get_page(self.vault_root, path=page.path)
        except (GetError, OSError, ValueError) as error:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable") from error
        if current.content_hash != page.content_hash or current.content != page.content:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        if unit_ref is None:
            return _ResolvedInput(
                evidence={
                    "reference": canonical,
                    "digest": model._hash(
                        "exomem-episode-input-page-v1", canonical, page.content_hash
                    ),
                },
                body=page.body,
            )
        unit = semantic_unit_read.read_semantic_unit(
            self.vault_root, page=page, unit_ref=unit_ref
        )
        if unit.status != "found" or unit.unit is None or unit.parent.ref != canonical:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable")
        return _ResolvedInput(
            evidence={
                "reference": unit_ref,
                "digest": model._hash(
                    "exomem-episode-input-unit-v1",
                    canonical,
                    page.content_hash,
                    unit_ref,
                    unit.unit.fingerprint,
                ),
            },
            text=unit.unit.span.text,
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
        return self._resolve_reference(reference).evidence

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
            page = get_page(self.vault_root, path=path)
        except (GetError, OSError, ValueError) as error:
            raise _error("EPISODE_INPUT_UNAVAILABLE", "input is unavailable") from error
        if memory_refs.ref_from_markdown(page.content) != canonical:
            raise _error("EPISODE_INPUT_INVALID", "the page at that path holds another ref")
        digest = model._hash("exomem-episode-input-page-v1", canonical, page.content_hash)
        if str(page.frontmatter.get("status") or "").casefold() == "superseded":
            return {"digest": digest}
        with egress.disclosure_boundary(self.vault_root, "episode-input-authorization"):
            released = egress.annotate_page(
                self.vault_root,
                {
                    "path": page.path,
                    "frontmatter": page.frontmatter,
                    "body": page.body,
                    "content": page.content,
                    "content_hash": page.content_hash,
                    "mtime": page.mtime,
                },
                principal=effective_principal(),
                snapshot_content=page.content,
                stable_ref=canonical,
            )
        if (
            released is None
            or released.get("content_hash") != page.content_hash
            or released.get("body") != page.body
        ):
            return {"digest": digest}
        return {"reference": canonical, "digest": digest}

    def bind_committed_input(self, key: str, *, path: str, reference: str) -> dict[str, Any]:
        """Bind a just-committed page as the next input revision of episode `key`.

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
        evidence = self._committed_evidence(canonical, path)
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
                    resolved = self._resolve_reference(evidence["reference"])
                except model.EpisodeError:
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                if resolved.evidence["digest"] != evidence.get("digest"):
                    return {"status": "stale", "input_revision": revision["revision"]}
                field, value = ("body", resolved.body) if resolved.body is not None else ("text", resolved.text)
                if value is None or len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                if not egress.direct_text_references_visible(
                    self.vault_root, value, principal=effective_principal()
                ):
                    return {"status": "unavailable", "input_revision": revision["revision"]}
                # The legacy reference gate records every target it examines.
                # It only proves that this representation is safe; its target
                # outcomes cannot be receipts for content this facade returns.
                with egress.disclosure_boundary(
                    self.vault_root, "episode-input-reference-validation"
                ) as redaction_collector:
                    redacted = egress.redact_withheld_references(
                        self.vault_root, value, principal=effective_principal()
                    )
                    scrubbed = egress.postfilter("get", redacted, self.vault_root)
                if redaction_collector.credential_redactions:
                    egress._record_credential_block(  # noqa: SLF001
                        redaction_collector.credential_redactions
                    )
                if redacted != value or scrubbed != value:
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
