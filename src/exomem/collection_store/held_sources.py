"""Exact Source membership carried by retained candidates and their projected copies."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .. import provenance, record_source_custody, source_capture
from .version_sources import _reference


class HeldBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    canonical_ref: Annotated[str, AfterValidator(_reference)]
    dep_epoch: Annotated[str, Field(min_length=1, max_length=1024)] | None
    dep_operation: Annotated[str, Field(min_length=1, max_length=1024)]


class Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    store_id: Annotated[str, Field(min_length=1, max_length=1024)]
    collection_id: Annotated[str, Field(min_length=1, max_length=1024)]
    held_id: Annotated[str, Field(min_length=1, max_length=1024)]
    publication_id: Annotated[str, Field(min_length=1, max_length=1024)]
    candidate_sha256: Annotated[str, AfterValidator(source_capture.validate_sha256)]
    held_sha256: Annotated[str, AfterValidator(source_capture.validate_sha256)]
    complete: bool
    bindings: Annotated[tuple[HeldBinding, ...], Field(max_length=record_source_custody.MAX_PAYLOAD_REFERENCES)]


def create(*, store_id: str, collection_id: str, held_id: str, candidate: str, raw: bytes,
           facts: record_source_custody.Facts, operation: str | None = None,
           epochs: Mapping[str, str] | None = None) -> str:
    """Every replacement gets a new publication, including replacements with identical bytes."""
    publication = str(uuid.uuid4())
    operation, epochs = operation or publication, epochs or {}
    descriptor = Descriptor(store_id=store_id, collection_id=collection_id, held_id=held_id,
        publication_id=publication, candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
        held_sha256=hashlib.sha256(raw).hexdigest(), complete=facts.complete,
        bindings=tuple(HeldBinding(canonical_ref=reference, dep_epoch=epochs.get(reference), dep_operation=operation)
                       for reference in facts.references))
    return json.dumps(descriptor.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def raw_facts(raw: bytes) -> record_source_custody.Facts:
    """A foreign view may declare known inputs, but its whole attribution remains incomplete."""
    try:
        document = provenance.parse_origin(raw.decode("utf-8"), managed=True, authoring=True)
    except UnicodeError:
        return record_source_custody.Facts((), False)
    references = () if document.payload is None else tuple(dict.fromkeys(
        value["reference"].partition("#")[0] for value in document.payload["inputs"].values()))
    return record_source_custody.Facts(references[:record_source_custody.MAX_PAYLOAD_REFERENCES], False)
