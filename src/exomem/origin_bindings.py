"""Exact retained-input proofs for managed origin metadata, not reusable authority."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import provenance, retained_inputs, source_closure
from .episode_recovery import EpisodeInputOwner
from .vault import PathGuard, PlannedWrite, parse_frontmatter

if TYPE_CHECKING:
    from .semantic_contract import RelationFact
    from .semantic_units import SemanticUnitDocument


@dataclass(frozen=True)
class OriginInputProof:
    root: str
    retained: retained_inputs.RetainedInput
    text: str
    journal_guard: PathGuard | None = None

    @property
    def guards(self) -> tuple[PathGuard, ...]:
        assert self.retained.guard is not None
        return (self.retained.guard,) + (
            (self.journal_guard,) if self.journal_guard is not None else ()
        )


@dataclass(frozen=True)
class PreparedOriginInputs:
    """Exact authoring bytes and input proofs; each commit check reacquires release."""

    source: str
    inputs: tuple[tuple[str, OriginInputProof], ...]

    def revalidate(self, vault_root: Path) -> None:
        document = provenance.parse_origin(self.source, managed=True, authoring=True, strict=True)
        if document.payload is None:
            raise _unavailable()
        bindings = document.payload["inputs"]
        for label, prior in self.inputs:
            current = resolve_origin_input(vault_root, bindings[label])
            if current.root != prior.root:
                raise provenance.OriginError("ORIGIN_INPUT_STALE", "retained input root changed")

    def required_guards(
        self, vault_root: Path, writes: tuple[PlannedWrite, ...] | list[PlannedWrite]
    ) -> tuple[PathGuard, ...]:
        """Retain old-byte guards only for inputs this atomic batch does not rewrite."""
        guarded_writes = {
            write.path.absolute().relative_to(vault_root.absolute()).as_posix(): write
            for write in writes
        }
        guards = []
        for _label, proof in self.inputs:
            assert proof.retained.guard is not None
            guard = proof.retained.guard
            write = guarded_writes.get(guard.target)
            if write is None:
                guards.append(guard)
            else:
                # Existing Source closure supplies the write guard. The atomic
                # owner checks that pre-image and its installed post-image.
                if (
                    write.guard is None
                    or write.guard.expected_content_hash != guard.expected_content_hash
                    or provenance.evidence_version(write.content)
                    != provenance.evidence_version(proof.retained.page.content)
                ):
                    raise provenance.OriginError(
                        "ORIGIN_INPUT_STALE", "retained input rewrite changed"
                    )
            if proof.journal_guard is not None:
                guards.append(proof.journal_guard)
        return tuple(guards)


def _unavailable() -> provenance.OriginError:
    return provenance.OriginError("ORIGIN_INPUT_UNAVAILABLE", "retained input is unavailable")


def resolve_origin_input(
    vault_root: Path, binding: object, *, disclosure: bool = False
) -> OriginInputProof:
    """Validate one parsed binding against the current released page/unit/span."""
    # The envelope owner defines this grammar; do not create a second validator.
    spec = provenance._origin_payload(  # noqa: SLF001
        {"inputs": {"input": binding}, "assessments": [], "bindings": []}, authoring=False
    )["inputs"]["input"]
    try:
        retained = retained_inputs.resolve_retained_input(
            vault_root, spec["reference"], disclosure=disclosure
        )
    except retained_inputs.RetainedInputError as error:
        raise _unavailable() from error
    page = retained.page
    if (
        not source_closure._eligible_path(page.path)  # noqa: SLF001
        or str(page.frontmatter.get("type") or "").casefold() not in {"source", "evidence"}
        or retained.released is None
        or retained.released.get("frontmatter") != page.frontmatter
    ):
        raise _unavailable()
    try:
        current_version = provenance.evidence_version(page.content)
    except ValueError as error:
        raise _unavailable() from error
    if current_version != spec["version"] or (
        retained.unit is not None and retained.unit.fingerprint != spec["unit_fingerprint"]
    ):
        raise provenance.OriginError("ORIGIN_INPUT_STALE", "retained input binding changed")
    text = retained.unit.span.text if retained.unit is not None else page.body
    if "span" in spec:
        start, end = spec["span"]["start_offset"], spec["span"]["end_offset"]
        if end > len(text):
            raise provenance.OriginError("ORIGIN_INPUT_STALE", "retained input span changed")
        text = text[start:end]
    if not retained_inputs.exact_text_visible(vault_root, text):
        raise _unavailable()
    root = retained.canonical
    journal_guard = None
    if str(page.frontmatter.get("source_type") or "").casefold() == "episode":
        proof = EpisodeInputOwner(vault_root).prove_original(retained)
        if proof is None:
            raise _unavailable()
        root, journal_guard = proof.root, proof.guard
    try:
        assert retained.guard is not None
        retained.guard.recheck(vault_root)
    except (OSError, ValueError) as error:
        raise _unavailable() from error
    return OriginInputProof(root, retained, text, journal_guard)


def project_origin_text(vault_root: Path, text: str) -> str:
    """Release a managed carrier whole or remove it, without changing canonical bytes.

    Disclosure depends only on whether every bound input is still released and
    available to the caller. A stale binding (its input changed since the
    assessment) is accounting state: the carrier stays visible.
    """
    metadata = provenance.parse_origin(text, managed=True)
    if metadata.status == "absent":
        return text
    if metadata.status == "valid":
        assert metadata.payload is not None
        try:
            proofs = []
            for binding in metadata.payload["inputs"].values():
                try:
                    proofs.append(resolve_origin_input(vault_root, binding, disclosure=True))
                except provenance.OriginError as error:
                    if error.code != "ORIGIN_INPUT_STALE":
                        raise
            retained_inputs.recheck_retained_inputs(
                vault_root,
                tuple((proof.retained, proof.text) for proof in proofs),
                disclosure=True,
            )
            # Check earlier inputs again after all later releases, including
            # exact episode-journal evidence. Changes after this checkpoint
            # remain the existing readers' observational window.
            for proof in proofs:
                for guard in proof.guards:
                    guard.recheck(vault_root)
        except (provenance.OriginError, retained_inputs.RetainedInputError, OSError, ValueError):
            pass
        else:
            return text
    # Malformed, duplicate and unavailable carriers are never partially
    # disclosed. Keep unrelated prose and examples in their original positions.
    return metadata.without_metadata(text)


def _authored_origin(source: str, before_source: str | None) -> provenance.OriginDocument | None:
    if before_source is not None:
        # Semantic writers compare logical LF text even when an append keeps
        # physical CRLF bytes. Use that same representation on both sides.
        prior = before_source.replace("\r\n", "\n").replace("\r", "\n")
        candidate = source.replace("\r\n", "\n").replace("\r", "\n")
        before = provenance.parse_origin(prior, managed=True)
        after = provenance.parse_origin(candidate, managed=True, authoring=True)
        if before.spans and tuple(prior[start:end] for start, end in before.spans) == tuple(
            candidate[start:end] for start, end in after.spans
        ):
            # Carry existing metadata verbatim, including stale attribution.
            # Its persisted fingerprints are never filled or rebound by edits.
            return None
    metadata = provenance.parse_origin(source, managed=True, authoring=True, strict=True)
    return None if metadata.status == "absent" else metadata


def extract_origin_metadata(content: str) -> tuple[str, str | None]:
    """Detach designated metadata before a typed content/summary renderer runs."""
    metadata = _authored_origin(content, None)
    if metadata is None:
        return content, None
    start, end = metadata.spans[0]
    return (content[:start] + content[end:]).strip(), content[start:end]


def place_authored_origin(source: str, *, before_source: str | None = None) -> str:
    """Keep new metadata outside the semantic content it is about to bind."""
    metadata = _authored_origin(source, before_source)
    if metadata is None:
        return source
    start, end = metadata.spans[0]
    carrier = source[start:end]
    clean = metadata.without_metadata(source)
    _fields, body, _ = parse_frontmatter(clean)
    prefix = clean[: len(clean) - len(body)]
    # One line of its own: removing that line restores the author's text.
    return prefix + carrier + ("\r\n" if "\r\n" in source else "\n") + body


def matches_retained_origin(source: str, retained: str) -> bool:
    """Recognize a retained normalization without reading or recertifying its inputs."""
    try:
        placed = place_authored_origin(source)
        authored = provenance.parse_origin(placed, managed=True, authoring=True, strict=True)
        historical = provenance.parse_origin(retained, managed=True, strict=True)
        if authored.payload is None or historical.payload is None:
            return False
        bindings = authored.payload["bindings"]
        previous = historical.payload["bindings"]
        if len(bindings) != len(previous):
            return False
        for binding, old in zip(bindings, previous, strict=True):
            scope, target = binding["scope"], old["scope"]
            missing = target.keys() - scope.keys()
            if not missing <= {"fingerprint", "occurrence_fingerprint"}:
                return False
            # Only omitted fingerprints may come from this exact retained
            # binding. Supplied fingerprints and every other field stay authored.
            binding["scope"] = {**target, **scope}
        if authored.payload != historical.payload:
            return False
        start, end = authored.spans[0]
        normalized = placed[:start] + provenance.encode_origin(authored.payload) + placed[end:]
        return normalized == retained
    except provenance.OriginError:
        return False


def normalize_origin_scopes(
    source: str,
    *,
    document: SemanticUnitDocument,
    fields: Mapping[str, object],
    owner_ref: str | None,
    relations: Iterable[tuple[RelationFact, str | None, str | None]] = (),
    record_identity: tuple[str, str] | None = None,
    before_source: str | None = None,
) -> str:
    """Bind new metadata to exact staged outputs; never refresh an existing block."""
    metadata = _authored_origin(source, before_source)
    if metadata is None:
        return source
    assert metadata.payload is not None
    relation_snapshot = tuple(relations)
    for binding in metadata.payload["bindings"]:
        match = provenance.match_origin_scope(
            binding["scope"],
            document=document,
            fields=fields,
            record_identity=record_identity,
            owner_ref=owner_ref,
            relations=relation_snapshot,
            authoring=True,
        )
        if match.status != "found":
            raise provenance.OriginError(
                "ORIGIN_SCOPE_STALE" if match.status == "stale" else "ORIGIN_METADATA_INVALID",
                "output scope does not match the staged effect",
            )
        binding["scope"] = match.scope
    start, end = metadata.spans[0]
    return source[:start] + provenance.encode_origin(metadata.payload) + source[end:]


def prepare_origin_inputs(
    vault_root: Path, source: str, *, before_source: str | None = None
) -> PreparedOriginInputs | None:
    """Prepare managed authoring inputs without filling or rebinding output scopes."""
    metadata = _authored_origin(source, before_source)
    if metadata is None:
        return None
    assert metadata.payload is not None
    inputs = tuple(
        (label, resolve_origin_input(vault_root, binding))
        for label, binding in metadata.payload["inputs"].items()
    )
    return PreparedOriginInputs(source, inputs)
