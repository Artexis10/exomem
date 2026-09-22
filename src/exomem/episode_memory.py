"""The `episode_memory` operation: record a bounded recap, bind it, inspect it.

`record` is the only way a recap reaches the vault. The agent authors it; this
module validates it (`episode_capture`), writes it through the ordinary Source
writer as an `episode` Source, retires the episode's earlier live revision in
the same batch, and binds the committed page to the caller's own episode ledger
by the writer's receipt. `inspect` reads that ledger back.

What it never does: summarise anything itself, walk the corpus for the page it
just wrote, accept curation leaves or proposals, or answer a committed write
with an error because the recorder may not read the page back.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import add as add_module
from . import episode_capture, episode_nudge, memory_refs, query_log, source_taxonomy
from .episode_model import EpisodeError
from .episode_recovery import EpisodeInputOwner
from .governance import egress
from .governance.principal import effective_principal
from .vault import (
    BatchWriteError,
    ContentHashMismatchError,
    kb_root,
    parse_frontmatter,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Revision:
    order: str
    path: str
    frontmatter: dict[str, Any]

    @property
    def live(self) -> bool:
        return str(self.frontmatter.get("status") or "").casefold() != "superseded"


def _folder(vault_root: Path) -> Path:
    return kb_root(vault_root) / source_taxonomy.SOURCES_ROOT / source_taxonomy.EPISODE_PATH_LABEL


def _revisions(vault_root: Path, key: str) -> list[_Revision]:
    """This episode's recap pages, oldest first, from ONE listing of one folder.

    The filename carries the key's group and the recording time, so only the
    pages of this group are read, and only their frontmatter matters: a group
    collision is ruled out by the `episode` field itself.
    """
    folder = _folder(vault_root)
    group = episode_capture.key_group(key)
    try:
        with os.scandir(folder) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
    except FileNotFoundError:
        return []
    matched = sorted(
        (parts[1], name)
        for name in names
        if (parts := episode_capture.filename_parts(name)) is not None and parts[0] == group
    )
    revisions: list[_Revision] = []
    for order, name in matched:
        path = folder / name
        try:
            frontmatter, _body, _raw = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if frontmatter.get("episode") == key:
            revisions.append(
                _Revision(order, path.relative_to(vault_root).as_posix(), frontmatter)
            )
    return revisions


def _source(revision_path: str, frontmatter: dict[str, Any]) -> dict[str, str]:
    return {
        "ref": memory_refs.memory_ref(str(frontmatter.get("exomem_id") or "")),
        "path": revision_path,
        "title": str(frontmatter.get("title") or ""),
    }


def _write(
    vault_root: Path, source_schema: Any, recap: episode_capture.Recap, when: Any
) -> tuple[dict[str, str], bool]:
    """The committed recap page for `recap`, writing it only when it is new.

    Identical to the newest live revision means a retry: nothing is written.
    Otherwise the new page is written and every live earlier revision is
    retired in the same batch. A concurrent record that changed one of those
    revisions first fails that batch's CAS; it is re-read and retried once.
    """
    for attempt in range(2):
        revisions = _revisions(vault_root, recap.key)
        live = [item for item in revisions if item.live]
        if live and live[-1].frontmatter.get("episode_digest") == recap.digest:
            return _source(live[-1].path, live[-1].frontmatter), True
        try:
            result = add_module.add(
                vault_root,
                source_schema,
                content=recap.body,
                title=recap.subject,
                source_type=source_taxonomy.EPISODE_KIND,
                slug=recap.slug,
                today=when.replace(microsecond=0),
                extra_frontmatter=recap.frontmatter,
                supersede=tuple(item.path for item in live),
            )
        except add_module.AddError as error:
            raise ValueError(f"{error.code}: {error.reason} (missing: {error.missing})") from error
        except (ContentHashMismatchError, BatchWriteError):
            if attempt:
                raise
            continue
        query_log.log_write_call(tool="episode_memory", written_path=result.path, cited_sources=[])
        return {"ref": result.ref, "path": result.path, "title": recap.subject}, False
    raise AssertionError("unreachable")  # pragma: no cover


def record(
    vault_root: Path,
    source_schema: Any,
    *,
    episode: str | None,
    subject: Any,
    summary: Any,
    worked_on: Any = None,
    decided: Any = None,
    open: Any = None,  # noqa: A002 - the recap's own field name
    said: Any = None,
    about: Any = None,
    client: Any = None,
) -> dict[str, Any]:
    """Write (or recognise) one recap revision and bind it to the caller's ledger."""
    vault_root = Path(vault_root)
    owner = EpisodeInputOwner(vault_root)
    # Before anything else, so an unresolved caller writes nothing at all.
    owner._owner()  # noqa: SLF001 - the facade's own owner resolution
    # Microseconds kept for the filename's order token only; the Source writer
    # dates the page to the second, as it dates every page.
    when = dt.datetime.now().astimezone()
    fields = {
        "subject": subject,
        "summary": summary,
        "worked_on": worked_on,
        "decided": decided,
        "open": open,
        "said": said,
        "client": client,
    }
    recap = episode_capture.prepare(episode=episode, about=about, when=when, **fields)
    about_skipped = 0
    if recap.about:
        visible = egress.visible_memory_refs(
            vault_root, recap.about, principal=effective_principal()
        )
        kept = [ref for ref in recap.about if ref in visible]
        about_skipped = len(recap.about) - len(kept)
        if about_skipped:
            recap = episode_capture.prepare(episode=recap.key, about=kept, when=when, **fields)

    source, idempotent = _write(vault_root, source_schema, recap, when)
    revision: int | None
    try:
        bound = owner.bind_committed_input(
            recap.key, path=source["path"], reference=source["ref"]
        )
    except EpisodeError as error:
        if error.code == "EPISODE_OWNER_UNRESOLVED":
            raise
        # The recap is committed and stays readable; only this caller's ledger
        # missed it. A retry is idempotent and binds it.
        log.warning("episode ledger bind failed after a committed recap: %s", error.code)
        ledger, recovery, revision = "unbound", "unavailable", None
    else:
        ledger, recovery, revision = bound["ledger"], bound["recovery"], bound["input_revision"]
    # A recorded episode is what the `episode_due` advisory asks for.
    episode_nudge.note_record(vault_root)
    return {
        "operation": "episode_memory",
        "episode": recap.key,
        "revision": revision,
        "source": source,
        "idempotent": idempotent,
        "recovery": recovery,
        "ledger": ledger,
        "about_skipped": about_skipped,
    }


def inspect(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    """This caller's revision history of `episode`.

    An unknown key and a key only another audience holds are the same
    `EPISODE_NOT_FOUND`: the ledger is per audience and lists nothing across.
    """
    if not isinstance(episode, str) or not episode_capture.EPISODE_KEY_RE.fullmatch(episode):
        raise EpisodeError("EPISODE_KEY_INVALID", "episode must be an ep- key of 32 lowercase hex")
    history = EpisodeInputOwner(Path(vault_root)).input_history(episode)
    return {
        "episode": episode,
        "revisions": history["revisions"],
        "latest_source_ref": history["latest_reference"],
        "coverage_current": "unchecked",
    }
