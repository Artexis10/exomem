"""Per-engine media switches (`cloud-multimodal-processing`, "off by default on Cloud").

`EXOMEM_MEDIA_ENGINES` holds the comma-separated engines this deployment runs. Unset,
a Cloud cell runs none and every other install runs all of them, which keeps a
personal install's behaviour. cellctl renders the variable into the cells an operator
selects, so an engine turns on one cell at a time.

A media kind whose extractor needs no engine (plain text, email, calendar) runs on
the standard library and is never switched off.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from . import cloud_cell, media_types

log = logging.getLogger(__name__)

ENGINES_ENV = "EXOMEM_MEDIA_ENGINES"

# A closed set: each name is extraction code this package implements, so a new
# engine is a code change. Which of them run is deployment configuration.
DOCUMENTS = "documents"
OCR = "ocr"
SPEECH = "speech"
ENGINES = (DOCUMENTS, OCR, SPEECH)

_DOCUMENT_KINDS = frozenset({"pdf", *media_types.DOC_EXTS.values()})
_SPEECH_KINDS = frozenset({"audio", "video"})

ENABLED = "enabled"
DISABLED = "disabled"
UNAVAILABLE = "unavailable"


def engine_for(media_type: str | None) -> str | None:
    """The engine whose extractor reads `media_type`, as `extract.extract_text` dispatches."""
    if media_type in _SPEECH_KINDS:
        return SPEECH
    if media_type == "image":
        return OCR
    if media_type in _DOCUMENT_KINDS:
        return DOCUMENTS
    return None


def enabled_engines(env: Mapping[str, str] | None = None) -> frozenset[str]:
    values = os.environ if env is None else env
    raw = values.get(ENGINES_ENV)
    if raw is None:
        return frozenset() if cloud_cell.cloud_mode_enabled(values) else frozenset(ENGINES)
    named = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = named.difference(ENGINES)
    if unknown:
        log.warning("%s names unknown engines, ignored: %s", ENGINES_ENV, ", ".join(sorted(unknown)))
    return frozenset(named.intersection(ENGINES))


def stage_enabled(media_type: str | None, env: Mapping[str, str] | None = None) -> bool:
    """Whether this deployment extracts `media_type` at all."""
    engine = engine_for(media_type)
    return engine is None or engine in enabled_engines(env)


def stage_available(media_type: str | None) -> bool:
    """Enabled, and the extractor's dependencies are installed. Loads no model."""
    from . import extract

    return stage_enabled(media_type) and extract.dependencies_present(media_type)


def status(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Each engine as enabled, disabled (switched off or not shipped) or unavailable.

    Unavailable means switched on with its software missing, which the operator repairs.
    """
    from . import extract

    enabled = enabled_engines(env)
    out: dict[str, str] = {}
    for engine in ENGINES:
        kinds = [kind for kind in sorted(media_types.KINDS) if engine_for(kind) == engine]
        if engine not in enabled:
            out[engine] = DISABLED
        elif all(extract.dependencies_present(kind) for kind in kinds):
            out[engine] = ENABLED
        else:
            out[engine] = UNAVAILABLE
    return out


def unavailable_next_action() -> str:
    """The next action of a job whose engine cannot load.

    A Cloud tenant cannot install software, so a Cloud cell reports the engine as
    unavailable on this deployment, for the operator to repair.
    """
    if cloud_cell.cloud_mode_enabled():
        return "wait: this media engine is unavailable on this deployment until the operator restores it"
    return "install the required media dependency, then retry"
