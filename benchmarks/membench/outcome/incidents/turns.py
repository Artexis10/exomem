"""The user turns of an incident case, shared by both actors and by the judge.

A case gives ``prompt`` (one user turn) or ``turns`` (several, played in order in one session). An optional
``follow_up`` is one more turn that a NEW session plays on the same vault after the main session ends.
"""

from __future__ import annotations


def user_turns(case: dict) -> list[str]:
    """The turns of the case's main session: its ``turns``, or its one ``prompt``."""
    return list(case["turns"]) if "turns" in case else [case["prompt"]]


def transcript(turns: list[str], answers: list[str]) -> tuple[str, str]:
    """The user turns and the answers as the judge reads them: ``(turn text, answer text)``.

    One turn reads as it always has. Several read numbered, so that a claim made in any turn is judged
    against the turn it answers.
    """
    if len(turns) == 1:
        return turns[0], answers[0]
    return (
        "\n\n".join(f"[turn {n}] {turn}" for n, turn in enumerate(turns, 1)),
        "\n\n".join(f"[answer {n}]\n{answer}" for n, answer in enumerate(answers, 1)),
    )
