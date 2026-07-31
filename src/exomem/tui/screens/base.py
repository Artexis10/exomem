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
        self._generation = 0

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
    ) -> int:
        """Run a synchronous backend call off-thread; results honor generations.

        A newer submission (or `supersede()`) makes older results no-ops — the
        cooperative-cancellation reality of thread workers means the old call
        may still complete, so dropping its result is the true cancel.
        """
        self._generation += 1
        generation = self._generation

        def work() -> None:
            try:
                result = job()
            except BackendError as error:
                self.app.call_from_thread(self._deliver, generation, on_error, error)
                return
            except Exception as error:  # noqa: BLE001 — surface, never crash the UI thread
                wrapped = BackendError("INTERNAL", str(error))
                self.app.call_from_thread(self._deliver, generation, on_error, wrapped)
                return
            self.app.call_from_thread(self._deliver, generation, on_success, result)

        self.run_worker(work, thread=True, group=group, exclusive=True)
        return generation

    def supersede(self) -> None:
        """Invalidate any in-flight backend results for this screen."""
        self._generation += 1

    def _deliver(self, generation: int, callback: Callable[[Any], None], payload: Any) -> None:
        if generation != self._generation or not self.is_attached:
            return
        callback(payload)

    # Help overlay content: the screen's own visible bindings.
    def help_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for binding in self.BINDINGS:
            if isinstance(binding, Binding) and binding.description:
                rows.append((binding.key, binding.description))
        return rows
