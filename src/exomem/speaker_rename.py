"""Rename the speaker labels of one recording, after the person tells the agent who spoke.

The sidecar stores no turn list: the labelled lines of the extraction, part pages included, are
the stored turns. A rename rewrites the label span at the start of each line that carries a
renamed label (`speaker_names.rename_labels`), and nothing else, so every other byte of every
page stays as it was. It does not enrol a voice: keeping a voice is an owner action on a personal
install (`enroll-speaker`), which the reply names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import extraction_parts, preserve, speaker_names
from .speech_attach import Source, commit_under_guard, refuse, resolve
from .speech_attach_schema import RENAME_OPERATION
from .vault import PlannedWrite, content_hash, parse_frontmatter, read_guarded_text

#: The reply's pointer to the owner action that keeps a voice, so an agent can send the person there.
ENROLL_NEXT_ACTION = (
    "This changed names on the page and no voice profile. To keep a voice for later recordings, the owner "
    "runs enroll-speaker for the recording and label, in the terminal or the Home screen of a personal install."
)


def _check(renames: Mapping[str, str], labels: Sequence[str]) -> dict[str, str]:
    """The renames still to apply, or a refusal that names the rule a name or label breaks.

    A pair whose label the page no longer carries, and whose name it does, was applied by an earlier call.
    """
    pending: dict[str, str] = {}
    for label, name in renames.items():
        problem = speaker_names.name_problem(name)
        if problem is not None:
            raise refuse("SPEAKER_NAME_INVALID", problem)
        if label in labels:
            if name != label:
                pending[label] = name
        elif name not in labels:
            raise refuse("SPEAKER_LABEL_UNKNOWN", f"the recording carries no label {label!r}; it carries {list(labels)}")
    after = [renames.get(label, label) if label in pending else label for label in labels]
    if len(set(after)) != len(after):
        raise refuse("SPEAKER_NAME_IN_USE", "a name would stand for two labels, which merges two speakers")
    return pending


def rename(
    vault_root: Path,
    path: str,
    renames: Mapping[str, str],
    *,
    commit_guard: Callable[[], AbstractContextManager[object]],
) -> dict[str, Any]:
    """Rename the labels of the recording at `path` as `renames` maps them; the reply says what changed."""
    vault = Path(vault_root).resolve()
    resolve(vault, path)  # a path that is no recording's Source is refused before the guard is taken

    def unit(commit: Callable[[Sequence[PlannedWrite]], None]) -> dict[str, Any]:
        current = resolve(vault, path)
        before, guard = read_guarded_text(vault, current.sidecar)
        fields = parse_frontmatter(before)[0]
        labels = [label for label in fields.get("speakers") or [] if isinstance(label, str)]
        pending = _check(renames, labels)
        if not pending:
            return _reply(current, before, outcome="unchanged", applied={})
        parts = _part_pages(vault, current.sidecar, fields)

        def rewrite(text: str) -> str:
            return speaker_names.rename_labels(text, pending)

        texts = [preserve.extracted_text(before), *(extraction_parts.text_of_part(content) for _, content, _ in parts)]
        on_lines = {label for text in texts for label in speaker_names.labels_in(text)}
        # `speakers` can list a label that no line carries; a rename of it would change no line and still say `renamed`.
        for label in pending:
            if label not in on_lines:
                raise refuse("SPEAKER_LABEL_UNKNOWN", f"no line of the extraction carries the label {label!r}")
        # Every page's lines, as they read after the rename: `speakers` lists the labels they then carry.
        carried = list(dict.fromkeys(label for text in texts for label in speaker_names.labels_in(rewrite(text))))
        entries = speaker_names.rename_entries(
            [item for item in fields.get("speaker_names") or [] if isinstance(item, dict)], pending
        )
        page = preserve.render_sidecar_renamed(
            before,
            rewrite=rewrite,
            speakers=carried,
            speaker_names=entries,
            speaker_verification=speaker_names.with_names(carried, entries, fields.get("speaker_verification")),
        )
        writes = [PlannedWrite(current.sidecar, page, guard=guard, expected_hash=content_hash(before))]
        for part_page, content, part_guard in parts:
            rewritten = extraction_parts.map_part_text(content, rewrite)
            if rewritten != content:
                writes.append(PlannedWrite(part_page, rewritten, guard=part_guard, expected_hash=content_hash(content)))
        commit(writes)
        after, _ = read_guarded_text(vault, current.sidecar)
        return _reply(current, after, outcome="renamed", applied=pending)

    return commit_under_guard(vault, commit_guard, "media_rename_speakers_commit", unit)


def _part_pages(vault: Path, sidecar: Path, fields: Mapping[str, Any]) -> list[tuple[Path, str, Any]]:
    """Each part page of the extraction with its text and the guard that binds what was read.

    A part that is missing or unreadable refuses the rename: the labels on it would stay as they were.
    """
    pages = []
    for number in range(2, extraction_parts.count_of(fields) + 1):
        page = extraction_parts.part_path(sidecar, number)
        try:
            content, guard = read_guarded_text(vault, page)
        except (OSError, UnicodeError) as error:
            raise refuse(
                "MEDIA_PARTS_UNREADABLE",
                f"part {number} of the extraction is missing or unreadable, so a rename would leave its labels as they were",
            ) from error
        pages.append((page, content, guard))
    return pages


def _reply(source: Source, content: str, *, outcome: str, applied: Mapping[str, str]) -> dict[str, Any]:
    fields = parse_frontmatter(content)[0]
    labels = fields.get("speakers")
    return {
        "operation": RENAME_OPERATION,
        "path": source.relative,
        "outcome": outcome,
        "sidecar_path": source.sidecar_relative,
        "renamed": [{"label": label, "name": name} for label, name in applied.items()],
        "speakers": labels if isinstance(labels, list) else [],
        "speaker_verification": fields.get("speaker_verification"),
        "next_action": ENROLL_NEXT_ACTION,
    }
