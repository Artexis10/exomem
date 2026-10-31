"""Load one situation file under the custody rules of ``SCHEMA.md`` section 1."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

#: The opaque case-file names SCHEMA.md fixes: ``hNN`` held out, ``dNN``
#: development, ``xNN`` the format toy. Key files (``kNN``, ``dkNN``) and any
#: other name fail this shape, so the loader never opens one.
_CASE_STEM = re.compile(r"[hdx][0-9]{2}")

#: The file-name prefix SCHEMA.md gives the held-out set.
HELD_OUT_PREFIX = "h"

#: The client shapes SCHEMA.md defines; each wires its sessions differently.
CLIENT_SHAPES = ("hooked", "hookless")

#: ``seeded_as`` values SCHEMA.md defines; each selects a different session.
ESTABLISHED = "established"
PRIOR_AGENT = "recent_promotion_by_prior_agent_session"

#: The top-level fields of SCHEMA.md's table; a case file with any other is refused.
TOP_LEVEL_FIELDS = frozenset(
    {
        "id",
        "pool",
        "use",
        "category",
        "variant",
        "client_shape",
        "seed",
        "capture_session",
        "fresh_session",
    }
)
#: The nested field tables of SCHEMA.md. The seed builder would silently drop
#: any other field, such as a supersession link, and build a different seed
#: than the case describes; so would the sessions.
SEED_FIELDS = frozenset({"vocabulary", "homes", "entities", "notes"})
#: The vocabulary entry table, and the relation ``direction`` that the door table lets a seed state.
VOCABULARY_FIELDS = frozenset(
    {"registry", "key", "label", "guidance", "description", "parent", "seeded_as", "direction"}
)
HOME_FIELDS = frozenset({"folder", "topics", "projects"})
ENTITY_FIELDS = frozenset({"ref", "title", "type", "body"})
#: The note-entry table, and the advisory ``sources`` it allows.
NOTE_FIELDS = frozenset({"title", "type", "folder", "topics", "projects", "body", "sources"})
CAPTURE_FIELDS = frozenset({"turns"})
FRESH_FIELDS = frozenset({"question"})
#: f34's preregistered aggregate digest of the held-out set: the SHA-256 of the
#: custody manifest that lists its 80 case files in name order.
HELD_OUT_MANIFEST_SHA256 = "2feb75c61589b5dacad5b2ef96a557118663141e4e690fc7912fedadfe97ae01"


class SituationRefused(ValueError):
    """The file breaks custody or the schema; no session may run from it."""


@dataclass(frozen=True)
class Situation:
    id: str
    pool: str
    category: str
    variant: str
    client_shape: str
    seed: dict
    turns: tuple[str, ...]
    question: str
    sha256: str
    path: Path

    def vocabulary(self, seeded_as: str) -> list[dict]:
        return [
            entry
            for entry in self.seed.get("vocabulary") or []
            if (entry.get("seeded_as") or ESTABLISHED) == seeded_as
        ]


def manifest_digests(manifest: Path) -> dict[str, str]:
    """``name -> sha256`` from a ``sha256sum``-format manifest.

    Only the digests are read; the adapter looks up its own situation files and
    nothing else the manifest lists.
    """

    digests: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.strip().partition(" ")
        if digest and name:
            digests[Path(name.strip().lstrip("*")).name] = digest.lower()
    return digests


def _refuse_held_out_without_custody(name: str, manifest: Path | None) -> None:
    if manifest is None:
        raise SituationRefused(f"{name} is held out and needs the custody manifest")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if digest != HELD_OUT_MANIFEST_SHA256:
        raise SituationRefused(
            f"{name} is held out, and the manifest {manifest.name} (sha256 {digest}) is not "
            "f34's preregistered held-out manifest"
        )


def _refuse_extra(
    name: str, where: str, value: object, fields: frozenset[str], label: str | None = None
) -> None:
    if not isinstance(value, dict):
        raise SituationRefused(f"{name}: a {where} is not a mapping")
    if extra := sorted(set(value) - fields):
        named = f"{where} {value.get(label)!r}" if label else where
        raise SituationRefused(f"{name}: {named} has fields outside SCHEMA.md: {extra}")


def load_situation(path: Path, *, manifest: Path | None = None) -> Situation:
    path = Path(path)
    # A case name that links to another file is judged by both names, so a link
    # never opens a key file or runs a held-out file as a development one.
    names = (path.name, path.resolve().name)
    for name in names:
        if not _CASE_STEM.fullmatch(Path(name).stem) or Path(name).suffix != ".yaml":
            raise SituationRefused(f"{name} is not a situation file name (hNN, dNN or xNN .yaml)")
    if any(name.startswith(HELD_OUT_PREFIX) for name in names):
        _refuse_held_out_without_custody(path.name, manifest)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if manifest is not None:
        expected = manifest_digests(manifest).get(path.name)
        if expected != digest:
            raise SituationRefused(
                f"{path.name}: sha256 {digest} does not match the manifest ({expected or 'not listed'})"
            )
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise SituationRefused(f"{path.name} is not a mapping")
    if "key" in data:
        # Keys live in their own files; a case file with one has broken custody.
        raise SituationRefused(f"{path.name} carries a key; the runner reads case files only")
    if extra := sorted(set(data) - TOP_LEVEL_FIELDS):
        raise SituationRefused(f"{path.name}: top-level fields outside SCHEMA.md: {extra}")
    if data.get("id") != path.stem:
        raise SituationRefused(f"{path.name}: id {data.get('id')!r} does not equal the file stem")
    if data.get("pool") == "heldout":
        _refuse_held_out_without_custody(path.name, manifest)
    if data.get("client_shape") not in CLIENT_SHAPES:
        raise SituationRefused(f"{path.name}: client_shape {data.get('client_shape')!r}")
    capture = data.get("capture_session") or {}
    fresh = data.get("fresh_session") or {}
    _refuse_extra(path.name, "capture_session", capture, CAPTURE_FIELDS)
    _refuse_extra(path.name, "fresh_session", fresh, FRESH_FIELDS)
    turns = capture.get("turns") or []
    question = fresh.get("question")
    if not turns or not all(isinstance(t, str) and t for t in turns):
        raise SituationRefused(f"{path.name}: capture_session.turns needs at least one text turn")
    if not isinstance(question, str) or not question:
        raise SituationRefused(f"{path.name}: fresh_session.question is missing")
    seed = data.get("seed") or {}
    _refuse_extra(path.name, "seed", seed, SEED_FIELDS)
    for entry in seed.get("vocabulary") or []:
        _refuse_extra(path.name, "vocabulary entry", entry, VOCABULARY_FIELDS, "key")
        if (entry.get("seeded_as") or ESTABLISHED) not in (ESTABLISHED, PRIOR_AGENT):
            raise SituationRefused(f"{path.name}: seeded_as {entry.get('seeded_as')!r}")
    for home in seed.get("homes") or []:
        _refuse_extra(path.name, "home", home, HOME_FIELDS, "folder")
    for entity in seed.get("entities") or []:
        _refuse_extra(path.name, "entity", entity, ENTITY_FIELDS, "ref")
    for note in seed.get("notes") or []:
        _refuse_extra(path.name, "note", note, NOTE_FIELDS, "title")
    return Situation(
        id=path.stem,
        pool=str(data.get("pool")),
        category=str(data.get("category")),
        variant=str(data.get("variant")),
        client_shape=data["client_shape"],
        seed=seed,
        turns=tuple(turns),
        question=question,
        sha256=digest,
        path=path,
    )
