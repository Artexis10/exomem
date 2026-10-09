"""Act, don't advise: what a strong incident route asks the agent to do.

A `records_routing` advisory used to be passive: it named a collection and left
the rest to an agent that, measured on a live vault, ignored it. For the case
that matters most -- a new failure-shaped observation routed STRONG into a
Records collection -- the advisory now carries a `disposition` chosen by the
effective capture prominence:

- `maximal`  -> `file`: a ready `record_memory` append payload, fields filled
  from the note where the manifest's fields map and the note cited in
  `sources`, with an instruction to perform it without asking.
- `balanced` -> `ask`: one precomposed yes/no question in the user's domain
  language, asked once per signal version; later writes of the same signal
  come back as `hold`.
- `light` / `off` -> `hold`: nothing to do now; the observation stays in the
  review and upkeep surfaces.

When the observation matches an existing item in the same collection (the same
natural-key value, or a strong symptom overlap), the disposition is
`append_occurrence`: a ready `record_memory` update, with its hash guards, that
adds the note to that item's sources instead of filing a new one. An item that
already cites the note is never its recurrence.

A disposition is offered when a note is created, and on a later edit only while
the note's own unreflected entry is still open -- unfiled and neither dismissed
nor snoozed: editing a note that is already filed or decided must not file it,
recur it into its own item, or ask twice.

The runtime never appends. Appending is `proactive_capture` and belongs to the
agent under the served capture disposition, through the same `record_memory`
path, writer lease and validation as any other append; the payload only saves
the agent from composing it. Moderate routes and observations that are not
failure-shaped keep the plain advisory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .collection_store.preview import canonical_read, selected_writer

log = logging.getLogger(__name__)

#: Folded type/category values that make an observation failure-shaped.
FAILURE_SHAPES = frozenset(  # PROVISIONAL
    {"bug", "defect", "error", "failure", "incident", "outage", "regression"}
)
#: A symptom overlap is a recurrence when the observation and an item share at
#: least this many folded terms and this Jaccard similarity.
RECURRENCE_MIN_SHARED = 3  # PROVISIONAL
RECURRENCE_MIN_JACCARD = 0.6  # PROVISIONAL
#: The most projected items one write compares against. The projection already
#: holds them in memory; this bounds the comparison, not a read.
MAX_RECURRENCE_ITEMS = 256  # PROVISIONAL
#: The asked-once ledger keeps the newest signals only.
MAX_ASKED_SIGNALS = 512  # PROVISIONAL

ASKED_FILENAME = "records-asked.json"
_ASKED_LOCK_TIMEOUT = 5
#: Page lifecycle frontmatter, never an observed value of a record.
_PAGE_ONLY_FIELDS = frozenset({"status"})

_FILE_INSTRUCTION = (
    "Perform this record_memory call now, without asking: the user's prominence "
    "delegates filing a recurring incident. Fill any missing_fields from the note "
    "first, and report what was filed in one line."
)
_OCCURRENCE_INSTRUCTION = (
    "Perform this record_memory call now, without asking: it adds the note to the "
    "existing item's sources. Report the added occurrence in one line."
)
_ASK_INSTRUCTION = (
    "Ask the question once, in these words or the user's own, and act only on a yes."
)
_HOLD_INSTRUCTION = (
    "Do nothing now; the observation stays in the review surface until someone files it."
)


def _asked_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ASKED_FILENAME


def first_ask(vault_root: Path, signal_version: str) -> bool:
    """Record one question's signal; True only the first time it is seen.

    A separate ledger rather than a key of the due-state projection: every
    projection write replaces that payload with a fixed shape, and a question
    must not be asked again just because a reconcile ran. The read-modify-write
    holds a `filelock` lock beside the ledger, the pattern `prominence_preferences`
    uses for its record, so two concurrent writes cannot both ask; a lock that
    cannot be taken asks nothing, and the observation stays in review.
    """
    from . import state_paths

    path = _asked_path(Path(vault_root))
    try:
        state_paths.ensure_vault_state_dir(Path(vault_root))
        with FileLock(str(path.parent / f".{ASKED_FILENAME}.lock"), timeout=_ASKED_LOCK_TIMEOUT):
            try:
                asked = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                if not isinstance(asked, list):
                    asked = []
            except (OSError, ValueError):
                asked = []
            if signal_version in asked:
                return False
            asked = [*[str(item) for item in asked if str(item) != signal_version], signal_version]
            handle, temp = tempfile.mkstemp(prefix=f".{ASKED_FILENAME}.", dir=path.parent)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(asked[-MAX_ASKED_SIGNALS:], stream)
            os.replace(temp, path)
    except Timeout:
        return False
    except OSError:
        log.debug("asked-once ledger not writable at %s", path, exc_info=True)
    return True


def _day(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _cites(name: str, spec: Any) -> bool:
    """Whether a field is where a record cites the note it came from."""
    is_link_list = spec.type == "array" and spec.items is not None and spec.items.type == "link"
    return name == "sources" or (is_link_list and name in {"evidence", "links"})


def _proposed_item(
    manifest: Any, state: Any, observation_ref: str
) -> tuple[dict[str, Any], list[str]]:
    """Fill the manifest's fields from the note where they map; cite the note."""
    frontmatter = getattr(state, "frontmatter", None) or {}
    title = str(getattr(state, "title", "") or "").strip()
    moment = _day(frontmatter.get("created") or frontmatter.get("updated"))
    item: dict[str, Any] = {}
    dated = False
    for name, spec in manifest.schema.fields.items():
        if _cites(name, spec):
            if observation_ref:
                item[name] = [observation_ref] if spec.type == "array" else observation_ref
            continue
        if spec.type == "string" and title and (name == "title" or name in manifest.schema.natural_key):
            item[name] = title
            continue
        if spec.type == "date" and moment and not dated and (
            spec.required or name in manifest.schema.natural_key
        ):
            item[name] = moment
            dated = True
            continue
        raw = (
            frontmatter.get(name)
            if isinstance(frontmatter, Mapping) and name not in _PAGE_ONLY_FIELDS
            else None
        )
        if raw is None:
            continue
        if spec.type == "string" and type(raw) in {str, int, float}:
            item[name] = str(raw)
        elif spec.type == "enum" and str(raw) in (spec.enum or ()):
            item[name] = str(raw)
        elif (
            spec.type == "array"
            and spec.items is not None
            and spec.items.type == "string"
            and isinstance(raw, (list, tuple))
        ):
            item[name] = [str(value) for value in raw if type(value) in {str, int, float}]
    missing = sorted(
        name for name, spec in manifest.schema.fields.items() if spec.required and name not in item
    )
    return item, missing


def _recurrence(
    manifest: Any,
    items: Sequence[Mapping[str, Any]],
    proposed: Mapping[str, Any],
    title: str,
    *,
    excluded: Collection[str] = (),
) -> Mapping[str, Any] | None:
    """The existing item this observation recurs, by natural key or symptom overlap.

    `excluded` names items already known to cite this note: a note never recurs
    into the record that files it.
    """
    from . import collection_claims

    natural = tuple(manifest.schema.natural_key)
    wanted = (
        tuple(collection_claims.normalize_text(proposed[name]) for name in natural)
        if natural and all(name in proposed for name in natural)
        else None
    )
    observed = collection_claims.normalize_terms([title])
    best: tuple[float, str, Mapping[str, Any]] | None = None
    for item in list(items)[-MAX_RECURRENCE_ITEMS:]:
        values = item.get("values")
        if (
            not isinstance(values, Mapping)
            or type(item.get("key")) is not str
            or item["key"] in excluded
        ):
            continue
        if wanted is not None and all(
            type(values.get(name)) in {str, int, float, bool} for name in natural
        ) and tuple(collection_claims.normalize_text(values[name]) for name in natural) == wanted:
            return item
        text = [
            str(value)
            for name, value in values.items()
            if type(value) is str and manifest.schema.fields.get(name) is not None
            and manifest.schema.fields[name].type in {"string", "enum"}
        ]
        terms = collection_claims.normalize_terms(text)
        shared = observed & terms
        union = observed | terms
        if len(shared) < RECURRENCE_MIN_SHARED or not union:
            continue
        score = len(shared) / len(union)
        if score >= RECURRENCE_MIN_JACCARD and (
            best is None or (score, item["key"]) > (best[0], best[1])
        ):
            best = (score, str(item["key"]), item)
    return best[2] if best is not None else None


def _question(title: str, collection_title: str, recurrence: Mapping[str, Any] | None) -> str:
    subject = title or "this"
    if recurrence is not None:
        return (
            f"This looks like another occurrence of “{recurrence['key']}”. "
            f"Add “{subject}” to it in {collection_title}?"
        )
    return f"Log “{subject}” as a new entry in {collection_title}?"


def _observation_entry(
    vault_root: Path, page_path: str, collection: str
) -> Mapping[str, Any] | None:
    """This page's stored per-page entry for the collection, if the projection has one."""
    from . import due_state

    categories = (due_state.load(Path(vault_root)) or {}).get("categories") or {}
    pages = categories.get("unreflected_observations") or {}
    for entry in due_state._unbucket(pages.get(page_path) if isinstance(pages, Mapping) else None):
        component = entry.get("component")
        if (
            isinstance(component, Mapping)
            and component.get("collection") == collection
            and component.get("kind") != due_state.BACKFILL_KIND
        ):
            return entry
    return None


def _still_open(vault_root: Path, entry: Mapping[str, Any]) -> bool:
    """Whether nobody has triaged this entry: no dismissal or live snooze.

    The same review-state reading the due-state serve applies, so an edit
    never acts on an item the owner already decided about.
    """
    from . import review_state

    effective, _decision = review_state.ReviewStateStore(Path(vault_root)).effective_state(
        str(entry.get("item_id") or ""), str(entry.get("fingerprint") or "")
    )
    return effective == "open"


@canonical_read
def _occurrence_call(
    vault_root: Path, manifest: Any, collection: str, key: str, observation_ref: str, why: str
) -> dict[str, Any] | None:
    """A `record_memory` update, runnable as returned, adding the note to one item.

    Reads the collection once for the guards `update` requires. None when the
    item cannot take another citation or already carries this one.
    """
    from . import record_formats
    from .structured_collections import CollectionError

    field = next(
        (name for name, spec in manifest.schema.fields.items()
         if _cites(name, spec) and spec.type == "array"),
        None,
    )
    if field is None or not observation_ref:
        return None
    writer = selected_writer(vault_root, manifest)
    if writer is not None:
        try:
            writer._operation.require_collection(manifest.collection_id, complete=True)
            writer._operation.require_complete_fields(manifest.collection_id)
        except CollectionError as error:
            if error.code != "COLLECTION_NOT_FOUND":
                raise
            # A refused advisory costs a suggestion; canonical guards would disclose withheld state.
            return None
    snapshot = record_formats.load_adapter(Path(vault_root), manifest).read()
    matches = [record for record in snapshot.records if record.identity.key == key]
    if len(matches) != 1 or matches[0].ambiguous:
        return None
    record = matches[0]
    current = record.values.get(field)
    if current is None:
        cited: list[str] = []
    elif isinstance(current, list) and all(type(value) is str for value in current):
        cited = list(current)
    else:
        return None
    if observation_ref in cited:
        return None
    container = (
        snapshot.source_versions[-1].hash
        if manifest.storage.strategy == "markdown-log"
        else snapshot.snapshot
    )
    item_version = record.source.hash
    if writer is not None:
        from .collection_store.writer import _row

        row = writer._collection(manifest)[0]
        item = _row(writer.connection.execute(
            "SELECT collection_id,item_key,row_version,payload_hash FROM items WHERE collection_id=? AND item_key=?",
            (manifest.collection_id, key)))
        if item is None:
            return None
        container = writer._container(row)
        item_version = writer._version(item)
    return {
        "action": "update",
        "collection": collection,
        "item_key": key,
        "changes": {field: [*cited, observation_ref]},
        "expected_container_hash": container,
        "expected_item_version": item_version,
        "why": why,
    }


def _fits(candidate: Mapping[str, Any], collection: str) -> bool:
    """Whether the compact terminal would carry this disposition as it stands."""
    from . import mutation_terminal

    return mutation_terminal._routing_disposition_projection(candidate, collection) is not None


@canonical_read
def disposition(
    vault_root: Path,
    routing: Mapping[str, Any],
    state: Any,
    *,
    level: str,
    created: bool = True,
) -> dict[str, Any] | None:
    """Fields to add to a strong, failure-shaped `records_routing`; None otherwise.

    A disposition the compact terminal could not carry (an over-long question
    or payload) is dropped on its own, before anything is marked asked, so the
    plain advisory still reaches the agent.
    """
    from . import collection_claims, due_state, memory_refs, semantic_writes

    if routing.get("strength") != "strong":
        return None
    facets = collection_claims.normalize_match(semantic_writes._records_routing_facets(state))
    shapes = facets.get("type", frozenset()) | facets.get("category", frozenset())
    if not shapes & FAILURE_SHAPES:
        return None
    collection = str(routing.get("collection") or "")
    manifest = due_state._load_manifest(Path(vault_root), collection) if collection else None
    if manifest is None:
        return None
    entry = _observation_entry(
        Path(vault_root), str(getattr(state, "path", "") or ""), collection
    )
    component = (entry or {}).get("component") or {}
    filed = {
        str(record.get("key"))
        for record in component.get("reflecting_records") or ()
        if isinstance(record, Mapping) and record.get("key")
    }
    if not created and (entry is None or filed or not _still_open(Path(vault_root), entry)):
        return None
    identity = str(getattr(state, "identity", "") or "")
    try:
        observation_ref = memory_refs.memory_ref(identity) if identity else ""
    except ValueError:
        observation_ref = ""
    title = str(getattr(state, "title", "") or "").strip()
    proposed, missing = _proposed_item(manifest, state, observation_ref)
    recurrence = _recurrence(
        manifest,
        due_state.visible_claim_items(Path(vault_root), collection),
        proposed,
        title,
        excluded=filed,
    )
    signal_version = hashlib.sha256(
        json.dumps(
            [
                str(manifest.collection_id),
                observation_ref or str(getattr(state, "path", "") or ""),
                str(recurrence["key"]) if recurrence is not None else None,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    hold = {"disposition": "hold", "signal_version": signal_version, "instruction": _HOLD_INSTRUCTION}
    if level not in {"balanced", "maximal"}:
        return hold
    if level == "balanced":
        asked = {
            "disposition": "ask",
            "signal_version": signal_version,
            "question": _question(title, str(manifest.title), recurrence),
            "instruction": _ASK_INSTRUCTION,
        }
        if not _fits(asked, collection):
            return None
        return asked if first_ask(Path(vault_root), signal_version) else hold
    why = f"Recurring incident observed in {observation_ref or title}"
    if recurrence is not None:
        call = _occurrence_call(
            Path(vault_root), manifest, collection, str(recurrence["key"]), observation_ref, why
        )
        if call is None:
            return None
        result = {
            "disposition": "append_occurrence",
            "signal_version": signal_version,
            "instruction": _OCCURRENCE_INSTRUCTION,
            "record_memory": call,
        }
    else:
        result = {
            "disposition": "file",
            "signal_version": signal_version,
            "instruction": _FILE_INSTRUCTION,
            "record_memory": {
                "action": "append",
                "collection": collection,
                "item": proposed,
                "why": why,
            },
            **({"missing_fields": missing} if missing else {}),
        }
    return result if _fits(result, collection) else None
