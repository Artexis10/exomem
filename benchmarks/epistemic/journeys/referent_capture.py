"""The f33 referent-capture case set: authored cases, answer key and seed vault.

The case files under ``fixtures/sequence7`` are the source. Every value the key
states appears in the authored turns, and nothing in them comes from a measured
session. Three rules make the set a yardstick rather than a description of a run:

1. **Frozen by digest.** The amendment receipt records the SHA-256 of every case
   file, the answer key and the seed vault. :func:`verify_fixture_bytes` refuses
   bytes that differ, at scenario load and at every key or seed read, so an arm
   run cannot score against an edited case.
2. **Twins differ by durability, not by mention count.** A twin names the same
   referent as often as its positive, in the same number of turns, as a passing
   or one-off thing. :func:`build_case_set` refuses a set where the counts differ.
3. **No turn names the store.** The f28 construction-and-load gate, the one the
   scenario loader applies to f33, runs over every turn and every fresh-session
   question; no new vocabulary is added here.

The second fresh session and its packet capture belong to the measurement lane's
driver, not to this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from membench.scoring.gates import states_value

from .collection_replay import StoreBearingUtterance, assert_no_store_bearing_utterance

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = "benchmarks/epistemic/fixtures/sequence7"
KEY_PATH = f"{FIXTURE_DIR}/f33-answer-key.v1.json"
SEED_PATH = f"{FIXTURE_DIR}/f33-seed-vault.v1.json"
RECEIPT_PATH = "benchmarks/epistemic/contracts/amendment-2026-10-referent-capture.v1.json"
FAMILY_ID = "f33"
ARMS: tuple[str, ...] = ("hookless", "hooked")


class ReferentCaseError(ValueError):
    """The f33 case set is missing, edited after freezing, or malformed."""


@dataclass(frozen=True)
class ReferentCase:
    """One authored case: its turns, its question and its key entry."""

    case_id: str
    polarity: str
    category: str
    turns: tuple[tuple[str, str], ...]
    question: str | None
    key: dict[str, Any]


def case_path(case_id: str) -> str:
    return f"{FIXTURE_DIR}/{case_id}.yaml"


def recorded_digests(repo_root: Path | str = REPO_ROOT) -> dict[str, str]:
    """The fixture digests the sequence-7 receipt froze. Fails closed."""

    from protocol.contracts import AmendmentReceipt

    path = Path(repo_root) / RECEIPT_PATH
    try:
        receipt = AmendmentReceipt.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ReferentCaseError(f"f33 receipt cannot be read: {RECEIPT_PATH}: {error}") from error
    if not receipt.fixture_sha256:
        raise ReferentCaseError(f"f33 receipt records no fixture digests: {RECEIPT_PATH}")
    return dict(receipt.fixture_sha256)


def verify_fixture_bytes(
    relative: str, data: bytes, *, repo_root: Path | str = REPO_ROOT
) -> None:
    """Refuse fixture bytes whose SHA-256 differs from the receipt's record."""

    expected = recorded_digests(repo_root).get(relative)
    if expected is None:
        raise ReferentCaseError(f"{relative} has no digest in the f33 receipt")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ReferentCaseError(
            f"{relative} differs from its frozen digest: expected {expected}, actual {actual}"
        )


def _read_verified(relative: str, repo_root: Path | str) -> bytes:
    try:
        data = (Path(repo_root) / relative).read_bytes()
    except OSError as error:
        raise ReferentCaseError(f"f33 fixture cannot be read: {relative}") from error
    verify_fixture_bytes(relative, data, repo_root=repo_root)
    return data


@lru_cache(maxsize=4)
def _key(repo_root: str) -> dict[str, dict[str, Any]]:
    document = json.loads(_read_verified(KEY_PATH, repo_root))
    if document.get("family_id") != FAMILY_ID:
        raise ReferentCaseError("the f33 answer key names another family")
    return {case["case_id"]: case for case in document["cases"]}


def answer_key(repo_root: Path | str = REPO_ROOT) -> dict[str, dict[str, Any]]:
    """``case_id -> key entry``, read only from digest-verified bytes."""

    return _key(str(Path(repo_root).resolve()))


def case_key(case_id: str | None, repo_root: Path | str = REPO_ROOT) -> dict[str, Any]:
    key = answer_key(repo_root)
    if case_id not in key:
        raise ReferentCaseError(f"unknown f33 case: {case_id!r}")
    return key[case_id]


def _arm_ops(payload: dict[str, Any], kind: str) -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        phase["phase_id"]: tuple(
            (op["ref"], op.get("detail", "")) for op in phase["ops"] if op["op"] == kind
        )
        for phase in payload["phases"]
    }


def _case(case_id: str, entry: dict[str, Any], repo_root: Path | str) -> ReferentCase:
    payload = yaml.safe_load(_read_verified(case_path(case_id), repo_root))
    if payload.get("scenario_id") != case_id or payload.get("family_id") != FAMILY_ID:
        raise ReferentCaseError(f"{case_id}: case file names another case or family")
    turns_by_arm = _arm_ops(payload, "agent_turn")
    questions_by_arm = _arm_ops(payload, "fresh_agent")
    if set(turns_by_arm) != set(ARMS) or len(set(turns_by_arm.values())) != 1:
        raise ReferentCaseError(f"{case_id}: both client shapes must replay the same turns")
    if len(set(questions_by_arm.values())) != 1:
        raise ReferentCaseError(f"{case_id}: both client shapes must ask the same question")
    turns = turns_by_arm[ARMS[0]]
    questions = questions_by_arm[ARMS[0]]
    if entry["polarity"] == "positive" and len(questions) != 1:
        raise ReferentCaseError(f"{case_id}: a positive asks exactly one fresh-session question")
    if entry["polarity"] == "twin" and questions:
        raise ReferentCaseError(f"{case_id}: a twin is scored on state and asks no question")
    question = questions[0][1] if questions else None
    try:
        assert_no_store_bearing_utterance(
            (*turns, *((f"{case_id}:{ref}", text) for ref, text in questions))
        )
    except StoreBearingUtterance as error:
        raise ReferentCaseError(f"{case_id}: {error}") from error
    return ReferentCase(case_id, entry["polarity"], entry["category"], turns, question, entry)


def _check_positive(case: ReferentCase, twin: ReferentCase) -> None:
    text = "\n".join(turn for _ref, turn in case.turns)
    key = case.key
    name = key["referent"]["name"]
    for value in (key["owner"]["name"], name, key["answer_value"]):
        if not states_value(value, text):
            raise ReferentCaseError(f"{case.case_id}: key value {value!r} is not stated in the turns")
    question = case.question or ""
    if not states_value(key["owner"]["name"], question) or states_value(name, question):
        raise ReferentCaseError(
            f"{case.case_id}: the question must name the person and not the referent"
        )
    twin_text = "\n".join(turn for _ref, turn in twin.turns)
    if len(twin.turns) != len(case.turns) or twin_text.count(name) != text.count(name):
        raise ReferentCaseError(
            f"{twin.case_id}: a twin must mention {name!r} as often as its positive, "
            "in the same number of turns"
        )
    if twin.key["referent"] != key["referent"] or twin.key["owner"] != key["owner"]:
        raise ReferentCaseError(f"{twin.case_id}: a twin names its positive's person and referent")


def build_case_set(repo_root: Path | str = REPO_ROOT) -> tuple[ReferentCase, ...]:
    """Load every case through verified bytes and refuse a malformed set."""

    key = answer_key(repo_root)
    cases = {case_id: _case(case_id, entry, repo_root) for case_id, entry in key.items()}
    positives = [case for case in cases.values() if case.polarity == "positive"]
    for case in positives:
        twin = cases.get(case.key["twin"])
        if twin is None or twin.key.get("positive") != case.case_id:
            raise ReferentCaseError(f"{case.case_id}: its twin is missing or names another case")
        _check_positive(case, twin)
    if len(cases) != 2 * len(positives):
        raise ReferentCaseError("every f33 case is a positive or the twin of one")
    return tuple(cases[case_id] for case_id in sorted(cases))


def seed_inputs(root: Path, *, repo_root: Path | str = REPO_ROOT) -> None:
    """Lay the generic seeded vault; the client owns every later change."""

    from exomem.init import init_vault

    seed = json.loads(_read_verified(SEED_PATH, repo_root))
    init_vault(root)
    for page in seed["pages"]:
        path = root / page["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page["body"], encoding="utf-8")


def reset_cache() -> None:
    """Drop the memoized key read. For tests that point at a fixture repo."""

    _key.cache_clear()


__all__ = [
    "FAMILY_ID",
    "KEY_PATH",
    "RECEIPT_PATH",
    "SEED_PATH",
    "ReferentCase",
    "ReferentCaseError",
    "answer_key",
    "build_case_set",
    "case_key",
    "case_path",
    "recorded_digests",
    "reset_cache",
    "seed_inputs",
    "verify_fixture_bytes",
]
