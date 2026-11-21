"""One source for moves over closed read-response states and hook compatibility."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import activation_result, capabilities, working_set_resolve

# Protocol prose bound, reserved before activation packet caching.
MAX_CHARS = 240
POINTER = "Follow `next_action` when present."


def templates(activate: str, read: str, search: str) -> dict[str, str]:
    """State names and transitions are fixed by the read-next-action contract."""
    return {
        activation_result.INDEX_WARMING: f"Recall is still warming; this is not an empty search. Use {search} for relevant stored material.",
        "ambiguous": f"These candidates need a choice. Call {activate} again with the original turn and `anchor` set to the ref you mean.",
        "method_reset": "Discard held methods before reuse. Read a current method source for its scope and exclusions before applying it within existing authority.",
        "task_read": f"Use {read} to open a task_context read_ref before relying on it. Read method scope and exclusions before applying it within existing authority.",
        "review_due": "Apply the served stance with its review caveat; its check date has passed, so verify it before relying on it.",
        "slice_empty": f"Map and slice cover compiled material only. Use {search} for captured Sources; an empty slice is not proof of absence.",
        "related": f"Related pages (not loaded). Use {read} to read a relevant ref before relying on it.",
        "search_miss": "Empty recall means not found in that query/scope. Try related terms or scope='vault'. Raw Records/Planning remain excluded; inspect/query collections.",
    }


def state(packet: Mapping[str, Any], *, search: bool = False) -> str | None:
    if (packet.get("abstention") or {}).get("reason") == activation_result.INDEX_WARMING:
        return activation_result.INDEX_WARMING
    if search:
        return "search_miss" if not packet.get("hits", packet.get("results", packet.get("items"))) else None
    # Anchor selection has no effect in maps mode; slice leads must not acquire that promise.
    if "map" not in packet:
        reason = (packet.get("abstention") or {}).get("reason")
        worded = any(
            item.get("status") == working_set_resolve.RETRIEVAL_NAMED_STATUS
            or working_set_resolve.WORDED_CONTACT_KINDS.intersection(item.get("evidence") or ())
            for item in packet.get("anchors") or () if isinstance(item, Mapping)
        )
        if reason == "ambiguous" or (reason == "unresolved" and worded):
            return "ambiguous"
    task = packet.get("task_context") or {}
    if (task.get("methods") or {}).get("reset"):
        return "method_reset"
    if any(item.get("review_due") for item in (packet.get("stance") or {}).get("items", ())):
        return "review_due"
    if task.get("candidates") or (task.get("methods") or {}).get("items"):
        return "task_read"
    if (packet.get("turn_slice") or {}).get("state") == "empty":
        return "slice_empty"
    if "map" not in packet and (packet.get("abstention") or {}).get("reason") == "unresolved" and packet.get("related"):
        return "related"
    return None


def move(packet: Mapping[str, Any], *, search: bool = False) -> dict[str, Any] | None:
    selected = state(packet, search=search)
    if selected is None:
        return None
    command = {activation_result.INDEX_WARMING: "search_memory", "ambiguous": "activate_context", "slice_empty": "search_memory", "related": "read_memory", "task_read": "read_memory"}.get(selected)
    if command is not None:
        tools = capabilities.active_tools(command)
        active = capabilities.current_active_surface()
        # All names here are command-registry aliases, not interpretations of a user message.
        names = (command, *tools, capabilities.active_spell_call(command, {})[0])
        if not tools or (active is not None and not active.callable_commands.intersection(names)):
            return None
    text = templates(
        capabilities.active_call_text("activate_context", {}),
        capabilities.active_call_text("read_memory", {}),
        capabilities.active_call_text("search_memory", {}),
    )[selected]
    if len(text) > MAX_CHARS:
        raise ValueError("next-action prose exceeds its protocol bound")
    result = {"state": selected, "say": text}
    if command is not None:
        result["tool"] = capabilities.active_spell_call(command, {})[0]
    # These moves require an agent choice; none has complete arguments yet.
    return result


def attach(packet: dict[str, Any], *, search: bool = False) -> None:
    try:
        action = move(packet, search=search)
        if action is not None:
            budget = packet.get("budget")
            if budget is not None:
                used = budget.get("used_chars", 0)
                if used + len(action["say"]) > budget["limit_chars"]:
                    raise ValueError("no room for the reserved next action")
                budget["used_chars"] = used + len(action["say"])
            packet["next_action"] = action
    except Exception:  # noqa: BLE001 - an optional move never fails the read
        packet.setdefault("diagnostics", []).append("next_action_unavailable")
