"""CL-Bench task environment served over JSON lines.

This file runs under CL-Bench's own interpreter, with the pinned upstream
checkout as the working directory, so CL-Bench's dependencies never enter
Exomem's environment. It imports nothing from this repository.

Protocol: one JSON object per line on stdin, one reply per line on stdout.

- ``{"op": "open", "task": ..., "variant": ..., "schedule": ..., "seed": ..., "run_index": ...}``
  builds the task and returns the first turn.
- ``{"op": "step", "action": {...}}`` validates the action against the turn's
  response schema. A valid action advances the task; an invalid one returns
  ``{"valid": false, "error": ...}`` and leaves the task where it was.
- ``{"op": "respond", "text": ...}`` does the same for an agent's raw reply,
  parsed by upstream's own Claude-system parser.
- ``{"op": "timeout", "timeout_seconds": ..., "elapsed_seconds": ...}`` ends
  the current instance as a timeout, through the task's own fallback.
- ``{"op": "evaluate"}`` returns the task's own result.

Scoring, feedback text and the answer check stay upstream; this server only
moves their values across the process boundary.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


def _turn(query) -> dict:
    from src.systems.utils.structured_output import schema_to_prompt_instruction

    return {
        "prompt": query.prompt,
        "schema_instruction": schema_to_prompt_instruction(query.response_schema),
        "instance_id": query.instance_id,
        "instance_index": query.instance_index,
        "metadata": {k: v for k, v in (query.metadata or {}).items() if k != "db_path"},
    }


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class Server:
    def __init__(self) -> None:
        self.task = None
        self.query = None

    def open(self, req: dict) -> dict:
        from src.registry import get_task_class

        kwargs = {
            key: req[key]
            for key in ("variant", "schedule", "seed", "run_index", "num_instances")
            if req.get(key) is not None
        }
        self.task = get_task_class(req["task"])(**kwargs)
        from src.systems.claude.system import _DEFAULT_MEMORY_INSTRUCTION

        self.query = self.task.reset()
        return {
            # The file-memory control arm's instruction, as upstream's Claude system authors it.
            "file_memory_instruction": _DEFAULT_MEMORY_INSTRUCTION,
            "turn": _turn(self.query),
            "instances": len(self.task.instances),
            "response_timeout_seconds": self.task.get_response_timeout_seconds(self.query),
        }

    def step(self, req: dict) -> dict:
        from pydantic import ValidationError
        from src.interface import Response

        try:
            action = self.query.response_schema.model_validate(req["action"])
        except ValidationError as exc:
            return {"valid": False, "error": str(exc)}
        return self._advance(self.task.step(Response(action=action)))

    def respond(self, req: dict) -> dict:
        """Parse an agent's raw reply with upstream's own Claude-system parser, then step."""
        from src.interface import Response
        from src.systems.claude.system import _parse_action_text

        try:
            action = _parse_action_text(req["text"], self.query.response_schema)
        except Exception as exc:  # noqa: BLE001 - upstream raises several types; all mean "no valid action"
            return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
        return self._advance(self.task.step(Response(action=action)))

    def timeout(self, req: dict) -> dict:
        fallback = self.task.get_timeout_fallback_response(
            self.query,
            timeout_seconds=float(req["timeout_seconds"]),
            elapsed_seconds=float(req["elapsed_seconds"]),
        )
        return self._advance(self.task.step(fallback))

    def _advance(self, result) -> dict:
        self.query = result.next_query
        return {
            "valid": True,
            "observation": result.observation.content,
            "instance_complete": result.observation.instance_complete,
            "done": result.done,
            "outcome": _jsonable(result.instance_outcome),
            "turn": _turn(result.next_query) if result.next_query is not None else None,
        }

    def evaluate(self, _req: dict) -> dict:
        result = self.task.evaluate()
        # ``score`` is a property, so asdict() leaves it out.
        return {"result": {**_jsonable(result), "score": result.score}}


def main() -> None:
    sys.path.insert(0, str(Path.cwd()))
    server = Server()
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            reply = getattr(server, req["op"])(req)
        except Exception as exc:  # noqa: BLE001 - the driver records the fault; it is never a scored loss
            reply = {"fault": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
