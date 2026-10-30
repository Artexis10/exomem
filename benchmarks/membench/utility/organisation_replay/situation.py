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
#: The note-entry fields of SCHEMA.md's table, and the advisory ``sources`` it
#: allows. The seed builder would silently drop any other field, such as a
#: supersession link, and build a different seed than the case describes.
NOTE_FIELDS = frozenset({"title", "type", "folder", "topics", "projects", "body", "sources"})


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


def load_situation(path: Path, *, manifest: Path | None = None) -> Situation:
    path = Path(path)
    if not _CASE_STEM.fullmatch(path.stem) or path.suffix != ".yaml":
        raise SituationRefused(f"{path.name} is not a situation file name (hNN, dNN or xNN .yaml)")
    if path.stem.startswith(HELD_OUT_PREFIX) and manifest is None:
        raise SituationRefused(f"{path.name} is held out and needs the custody manifest")
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
    if data.get("pool") == "heldout" and manifest is None:
        raise SituationRefused(f"{path.name} is held out and needs the custody manifest")
    if data.get("client_shape") not in CLIENT_SHAPES:
        raise SituationRefused(f"{path.name}: client_shape {data.get('client_shape')!r}")
    turns = (data.get("capture_session") or {}).get("turns") or []
    question = (data.get("fresh_session") or {}).get("question")
    if not turns or not all(isinstance(t, str) and t for t in turns):
        raise SituationRefused(f"{path.name}: capture_session.turns needs at least one text turn")
    if not isinstance(question, str) or not question:
        raise SituationRefused(f"{path.name}: fresh_session.question is missing")
    seed = data.get("seed") or {}
    for entry in seed.get("vocabulary") or []:
        if (entry.get("seeded_as") or ESTABLISHED) not in (ESTABLISHED, PRIOR_AGENT):
            raise SituationRefused(f"{path.name}: seeded_as {entry.get('seeded_as')!r}")
    for note in seed.get("notes") or []:
        if extra := sorted(set(note) - NOTE_FIELDS):
            raise SituationRefused(
                f"{path.name}: note {note.get('title')!r} has fields outside SCHEMA.md: {extra}"
            )
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
