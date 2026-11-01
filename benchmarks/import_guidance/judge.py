"""The judge: a second headless `claude -p` that reads only the transcript and the criteria."""

from __future__ import annotations

import json
from collections.abc import Iterable


def render_transcript(prompt: str, stream_lines: Iterable[str]) -> str:
    """The user's request, then the agent's words, tool calls and the tool replies, in order."""
    parts = [f"PERSON: {prompt}"]
    for raw in stream_lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("assistant", "user"):
            continue
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(f"AGENT: {block.get('text', '')}")
            elif block.get("type") == "tool_use":
                parts.append(f"AGENT CALLS {block.get('name')}: {json.dumps(block.get('input'))}")
            elif block.get("type") == "tool_result":
                parts.append(f"TOOL REPLY: {json.dumps(block.get('content'))[:3000]}")
    return "\n\n".join(parts)


def assistant_texts(stream_lines: Iterable[str]) -> list[str]:
    """Every text message the agent wrote in the run, in order: all that the person could have read."""
    texts: list[str] = []
    for raw in stream_lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content")
        texts.extend(
            block["text"]
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        )
    return texts


def render_answer_view(prompt: str, reference: str, answer: str) -> str:
    """What the judge reads when it scores the answer alone: the person's turn, the facts of the run,
    and the agent's final message. The same view for every arm, so a tool reply cannot show it the arm."""
    return f"PERSON: {prompt}\n\nREFERENCE (what actually happened):\n{reference}\n\nAGENT ANSWER TO THE PERSON:\n{answer}"


def answer_schema(criteria: tuple[dict, ...]) -> dict:
    properties: dict = {}
    for criterion in criteria:
        properties[criterion["id"]] = {"type": "boolean", "description": criterion["text"]}
        properties[f"{criterion['id']}_reason"] = {"type": "string", "description": "One line."}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def judge_prompt(transcript: str, criteria: tuple[dict, ...], *, answer_view: bool = False) -> str:
    listed = "\n".join(f"- {c['id']}: {c['text']}" for c in criteria)
    if answer_view:
        return (
            "Read what a person asked, the facts of what happened, and the answer an AI agent gave the "
            "person. For each criterion answer true if it holds for the agent's answer and false if it "
            "does not. Give a one-line reason for each. The reference is true; judge only the answer's "
            "words.\n\nCRITERIA\n" + listed + "\n\nCASE\n" + transcript
        )
    return (
        "Read the transcript between a person and an AI agent. For each criterion answer true if it "
        "holds in the transcript and false if it does not. Give a one-line reason for each. Judge only "
        "what the transcript shows.\n\nCRITERIA\n" + listed + "\n\nTRANSCRIPT\n" + transcript
    )


def parse_answer(stdout: str, criteria: tuple[dict, ...]) -> dict | None:
    """The structured answer, or None when the output is not the schema's object."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    answer = payload.get("structured_output") if isinstance(payload, dict) else None
    if not isinstance(answer, dict) or not all(
        isinstance(answer.get(c["id"]), bool) for c in criteria
    ):
        return None
    return answer
