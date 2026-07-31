"""Base classes shared by TUI screens: breakpoints, chrome, worker discipline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.binding import Binding
from textual.screen import Screen

from ..backend import BackendError


class ExomemScreen(Screen):
    """Responsive base: breakpoint classes + back/help chrome + safe workers."""

    # 80 columns must stay fully usable; panels appear with real width.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-standard"), (120, "-wide")]

    SCREEN_TITLE = ""

    BINDINGS = [
        Binding("escape", "app.back", "back", show=False),
        Binding("question_mark", "app.help", "help"),
        Binding("f1", "app.help", "help", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._generations: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Worker discipline: single-flight per group + late-result drop.
    # ------------------------------------------------------------------ #
    def run_backend(
        self,
        job: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BackendError], None],
        *,
        group: str = "default",
    ) -> None:
        """Run a synchronous backend call off-thread; results honor generations.

        Generations are per group: a newer submission in the SAME group (or a
        `supersede()`) makes the older result a no-op, while unrelated groups
        (e.g. a write-back while a search runs) never cancel each other. The
        cooperative-cancellation reality of thread workers means a superseded
        call may still complete — dropping its result is the true cancel.
        """
        generation = self._generations.get(group, 0) + 1
        self._generations[group] = generation

        def work() -> None:
            try:
                result = job()
            except BackendError as error:
                self.app.call_from_thread(self._deliver, group, generation, on_error, error)
                return
            except Exception as error:  # noqa: BLE001 — surface, never crash the UI thread
                wrapped = BackendError("INTERNAL", str(error))
                self.app.call_from_thread(self._deliver, group, generation, on_error, wrapped)
                return
            self.app.call_from_thread(self._deliver, group, generation, on_success, result)

        self.run_worker(work, thread=True, group=group, exclusive=True)

    def supersede(self, group: str | None = None) -> None:
        """Invalidate in-flight results — one group, or all when unspecified."""
        if group is None:
            for name in list(self._generations):
                self._generations[name] += 1
            return
        self._generations[group] = self._generations.get(group, 0) + 1

    def _deliver(
        self,
        group: str,
        generation: int,
        callback: Callable[[Any], None],
        payload: Any,
    ) -> None:
        if generation != self._generations.get(group) or not self.is_attached:
            return
        callback(payload)

    # Help overlay content: the screen's own visible bindings, deduplicated
    # (a screen may override an inherited key with a priority binding).
    def help_rows(self) -> list[tuple[str, str]]:
        rows: dict[str, str] = {}
        for binding in self.BINDINGS:
            if isinstance(binding, Binding) and binding.description:
                rows[binding.key] = binding.description
        return list(rows.items())
