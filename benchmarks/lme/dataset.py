"""Strict LongMemEval dataset plumbing with no network behavior."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)


class DatasetValidationError(ValueError):
    """The input is not a valid LongMemEval question collection."""


def _parse_timestamp(value: object, *, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise DatasetValidationError(f"{field} must be a non-empty timestamp string")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in (
            "%Y/%m/%d (%a) %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                parsed = dt.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise DatasetValidationError(f"{field} has unsupported timestamp {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).replace(microsecond=0)


def _timestamp_text(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant", "system"}:
            raise DatasetValidationError(f"unsupported chat role {self.role!r}")
        if not self.content.strip():
            raise DatasetValidationError("chat content must not be empty")


@dataclass(frozen=True)
class LmeSession:
    session_id: str
    timestamp: dt.datetime
    messages: tuple[ChatMessage, ...]

    def __post_init__(self) -> None:
        if not self.session_id:
            raise DatasetValidationError("session_id must not be empty")
        if not self.messages:
            raise DatasetValidationError(f"session {self.session_id!r} has no messages")

    @property
    def timestamp_text(self) -> str:
        return _timestamp_text(self.timestamp)


@dataclass(frozen=True)
class LmeQuestion:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: dt.datetime
    sessions: tuple[LmeSession, ...]
    answer_session_ids: tuple[str, ...]
    validation_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.question_id:
            raise DatasetValidationError("question_id must not be empty")
        if self.question_type not in QUESTION_TYPES:
            raise DatasetValidationError(
                f"question {self.question_id!r} has unsupported question_type "
                f"{self.question_type!r}"
            )
        if not self.question.strip():
            raise DatasetValidationError(f"question {self.question_id!r} has empty text")
        session_ids = [session.session_id for session in self.sessions]
        if len(session_ids) != len(set(session_ids)):
            raise DatasetValidationError(f"question {self.question_id!r} repeats a session id")
        unknown = set(self.answer_session_ids) - set(session_ids)
        if unknown:
            message = (
                f"question {self.question_id!r} names unknown answer sessions "
                f"{sorted(unknown)}"
            )
            if not self.is_abstention:
                raise DatasetValidationError(message)
            object.__setattr__(
                self,
                "validation_warnings",
                (*self.validation_warnings, message),
            )

    @property
    def question_date_text(self) -> str:
        return _timestamp_text(self.question_date)

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith("_abs")

    def gold_sessions(self) -> tuple[LmeSession, ...]:
        wanted = set(self.answer_session_ids)
        return tuple(session for session in self.sessions if session.session_id in wanted)


@dataclass(frozen=True)
class LmeDataset:
    questions: tuple[LmeQuestion, ...]

    def __post_init__(self) -> None:
        ids = [question.question_id for question in self.questions]
        if not ids:
            raise DatasetValidationError("dataset contains no questions")
        if len(ids) != len(set(ids)):
            raise DatasetValidationError("dataset repeats a question_id")


def _required(row: Mapping[str, Any], key: str, *, question_id: str) -> Any:
    if key not in row:
        raise DatasetValidationError(f"question {question_id!r} is missing {key}")
    return row[key]


def _messages(value: object, *, question_id: str, session_id: str) -> tuple[ChatMessage, ...]:
    if not isinstance(value, list):
        raise DatasetValidationError(
            f"question {question_id!r} session {session_id!r} must be a message list"
        )
    parsed: list[ChatMessage] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise DatasetValidationError(
                f"question {question_id!r} session {session_id!r} message {index} is not an object"
            )
        role = raw.get("role")
        content = raw.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise DatasetValidationError(
                f"question {question_id!r} session {session_id!r} message {index} "
                "requires string role and content"
            )
        parsed.append(ChatMessage(role=role, content=content))
    return tuple(parsed)


def _question(raw: object, index: int) -> LmeQuestion:
    if not isinstance(raw, Mapping):
        raise DatasetValidationError(f"question row {index} is not an object")
    raw_id = raw.get("question_id")
    question_id = raw_id if isinstance(raw_id, str) and raw_id else f"row-{index}"
    session_ids = _required(raw, "haystack_session_ids", question_id=question_id)
    dates = _required(raw, "haystack_dates", question_id=question_id)
    sessions = _required(raw, "haystack_sessions", question_id=question_id)
    if not all(isinstance(value, list) for value in (session_ids, dates, sessions)):
        raise DatasetValidationError(
            f"question {question_id!r} haystack fields must be parallel lists"
        )
    if not len(session_ids) == len(dates) == len(sessions):
        raise DatasetValidationError(
            f"question {question_id!r} haystack ids, dates, and sessions differ in length"
        )
    parsed_sessions: list[LmeSession] = []
    for session_index, (session_id, timestamp, messages) in enumerate(
        zip(session_ids, dates, sessions, strict=True)
    ):
        if not isinstance(session_id, str):
            raise DatasetValidationError(
                f"question {question_id!r} session id {session_index} is not a string"
            )
        parsed_sessions.append(
            LmeSession(
                session_id=session_id,
                timestamp=_parse_timestamp(
                    timestamp, field=f"question {question_id} haystack_dates[{session_index}]"
                ),
                messages=_messages(messages, question_id=question_id, session_id=session_id),
            )
        )
    answer_ids = raw.get("answer_session_ids", [])
    if not isinstance(answer_ids, list) or not all(isinstance(item, str) for item in answer_ids):
        raise DatasetValidationError(f"question {question_id!r} answer_session_ids must be strings")
    text_fields = {}
    for key in ("question_id", "question_type", "question", "answer"):
        value = _required(raw, key, question_id=question_id)
        if not isinstance(value, str):
            raise DatasetValidationError(f"question {question_id!r} field {key} must be a string")
        text_fields[key] = value
    return LmeQuestion(
        **text_fields,
        question_date=_parse_timestamp(
            _required(raw, "question_date", question_id=question_id),
            field=f"question {question_id} question_date",
        ),
        sessions=tuple(parsed_sessions),
        answer_session_ids=tuple(answer_ids),
    )


def load_dataset_bytes(raw: bytes) -> LmeDataset:
    """Load the official LongMemEval parallel-haystack JSON representation."""

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetValidationError("cannot load dataset bytes") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("questions")
    if not isinstance(payload, list):
        raise DatasetValidationError("dataset root must be a list or a questions object")
    return LmeDataset(tuple(_question(raw, index) for index, raw in enumerate(payload)))


def stable_dataset_bytes(path: Path | str) -> bytes:
    """Read a no-follow regular dataset once and reject path replacement races."""
    source = Path(path)
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise DatasetValidationError("dataset must be a no-follow regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DatasetValidationError("dataset must be a no-follow regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            raw = stream.read()
    finally:
        os.close(descriptor)
    try:
        after = source.lstat()
    except OSError as exc:
        raise DatasetValidationError("dataset changed during stable read") from exc
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise DatasetValidationError("dataset changed during stable read")
    return raw


def load_dataset(path: Path | str) -> LmeDataset:
    return load_dataset_bytes(stable_dataset_bytes(path))


def _question_dict(question: LmeQuestion) -> dict[str, object]:
    return {
        "question_id": question.question_id,
        "question_type": question.question_type,
        "question": question.question,
        "answer": question.answer,
        "question_date": question.question_date_text,
        "haystack_session_ids": [session.session_id for session in question.sessions],
        "haystack_dates": [session.timestamp_text for session in question.sessions],
        "haystack_sessions": [
            [{"role": message.role, "content": message.content} for message in session.messages]
            for session in question.sessions
        ],
        "answer_session_ids": list(question.answer_session_ids),
    }


def dump_dataset(dataset: LmeDataset, path: Path | str) -> None:
    """Write a canonical JSON representation accepted by the official schema."""

    Path(path).write_text(
        json.dumps(
            [_question_dict(question) for question in dataset.questions],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def render_session(session: LmeSession) -> str:
    """Render one timestamped session as retrieval-visible source text."""

    lines = [
        f"Session timestamp: {session.timestamp_text}",
        f"Session ID: {session.session_id}",
        "",
    ]
    lines.extend(f"{message.role}: {message.content}" for message in session.messages)
    return "\n".join(lines)


def question_ids(questions: Iterable[LmeQuestion]) -> set[str]:
    return {question.question_id for question in questions}
