"""What the speech engine decided for one recording.

A backend fills one `AsrFacts` as it transcribes; the extraction layer adds where
the language came from. The job runtime carries it as a plain mapping, and the
sidecar records it, so a wrong-language transcript can be diagnosed from the page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: How a transcript's language was chosen. The precedence among them lives in `extract`.
LANGUAGE_REQUESTED = "requested"
LANGUAGE_CONFIGURED = "configured"
LANGUAGE_DETECTED = "detected"
LANGUAGE_SOURCES = frozenset({LANGUAGE_REQUESTED, LANGUAGE_CONFIGURED, LANGUAGE_DETECTED})

#: Whether the engine read one language for the whole file or detected the language per window.
LANGUAGE_MODE_SINGLE = "single"
LANGUAGE_MODE_MULTILINGUAL = "multilingual"
LANGUAGE_MODES = frozenset({LANGUAGE_MODE_SINGLE, LANGUAGE_MODE_MULTILINGUAL})


@dataclass
class AsrFacts:
    language: str | None = None
    #: Only a detection has a probability; a language that was asked for has none.
    language_probability: float | None = None
    language_source: str | None = None
    #: The language the caller pinned, kept so a later run on the same recording reuses it.
    language_requested: str | None = None
    #: The mode the engine ran in. In `multilingual` mode `language` is the engine's report for the file.
    language_mode: str | None = None
    #: Seconds of audio that held speech, after the engine removed silence.
    speech_seconds: float | None = None
    duration_seconds: float | None = None
    #: The revision of the decoding policy the engine ran with.
    policy: str | None = None

    @property
    def reported(self) -> bool:
        """False when the backend reported nothing, so the sidecar records nothing."""
        return any(
            value is not None
            for value in (self.language, self.speech_seconds, self.duration_seconds, self.policy)
        )

    def as_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> AsrFacts:
        """Rebuild facts from a job result; a value of the wrong type is a corrupt result."""
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("ASR facts must be a mapping of the known fields")
        texts = ("language", "language_source", "language_requested", "language_mode", "policy")
        numbers = ("language_probability", "speech_seconds", "duration_seconds")
        for key in texts:
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"ASR fact {key!r} must be text")
        for key in numbers:
            item = value.get(key)
            if item is not None and (isinstance(item, bool) or not isinstance(item, int | float)):
                raise ValueError(f"ASR fact {key!r} must be a number")
        if value.get("language_source") not in (None, *LANGUAGE_SOURCES):
            raise ValueError("ASR fact 'language_source' is not a known source")
        if value.get("language_mode") not in (None, *LANGUAGE_MODES):
            raise ValueError("ASR fact 'language_mode' is not a known mode")
        return cls(**value)
