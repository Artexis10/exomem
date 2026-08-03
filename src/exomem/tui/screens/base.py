"""Base screen: breakpoints, chrome, cell budgets, and worker discipline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.binding import Binding
from textual.screen import Screen

from ..backend import BackendError
from ..widgets import AppFooter


class ExomemScreen(Screen):
    """Responsive base for every screen.

    Breakpoints follow the drawn frames: below 100 columns everything is a
    single column and detail opens full-screen; at 100+ a side pane appears
    beside the list. 80×24 stays fully usable — it is the base target, not a
    degraded mode.
    """

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-standard"), (120, "-wide")]

    SCREEN_TITLE = ""
    #: Default footer keys; states may replace them via `set_footer`.
    FOOTER_KEYS: tuple[tuple[str, str], ...] = ()

    BINDINGS = [
        Binding("escape", "app.back", "back", show=False),
        Binding("question_mark", "app.help", "help", show=False),
        Binding("f1", "app.help", "help", show=False),
        Binding("u", "refresh", "refresh", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._generations: dict[str, int] = {}
        self._footer_set = False

    # ------------------------------------------------------------------ #
    # Chrome
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        """Base chrome.

        Textual dispatches mount handlers to every class in the MRO — the
        subclass first, then this one — so screens must not chain up
        explicitly, and the default footer must not clobber one a subclass
        already chose.
        """
        if not self.app.skin.color:
            self.add_class("-no-color")
        if not self._footer_set:
            self.set_footer(list(self.FOOTER_KEYS))

    def set_footer(self, keys: list[tuple[str, str]]) -> None:
        self._footer_set = True
        footer = self.query(AppFooter)
        if footer:
            footer.first(AppFooter).set_keys(keys)

    #: Width of the side pane in the CSS; budgets are derived from it.
    SIDE_PANE_WIDTH = 58

    def content_budget(self) -> int:
        """Cells available to content: screen width minus padding and gutter."""
        return max(24, (self.size.width or 80) - 4)

    def list_budget(self) -> int:
        """Cells for the left list — narrower once a side pane is showing.

        Budgeting against the screen when a pane is open is what makes rows
        wrap, and a wrapped list row destroys the alignment the queue depends
        on. Every list therefore fits to the column it actually occupies.
        """
        if not self.side_pane_open():
            return self.content_budget()
        return max(24, (self.size.width or 80) - self.SIDE_PANE_WIDTH - 4)

    def detail_budget(self) -> int:
        """Cells inside the side pane (or the whole screen when it collapses).

        Derived from the pane's fixed CSS width minus its own left padding.
        Measuring the mounted widget instead looks more honest and is worse:
        the pane reports zero before layout settles, so the budget — and the
        stored golden frame — depended on when the paint happened.
        """
        if not self.side_pane_open():
            return self.content_budget()
        return self.SIDE_PANE_WIDTH - 2

    def side_pane_open(self) -> bool:
        """True when the layout is wide enough for a persistent side pane."""
        return self.has_class("-standard") or self.has_class("-wide")

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

    # ------------------------------------------------------------------ #
    # Refresh (`u` is standardized on every data screen)
    # ------------------------------------------------------------------ #
    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        """Re-read this screen's data. No-op on screens that hold none."""

    # Help overlay content: this screen's own footer keys, then its bindings.
    def help_rows(self) -> list[tuple[str, str]]:
        rows: dict[str, str] = {key: verb for key, verb in self.FOOTER_KEYS}
        for binding in self.BINDINGS:
            if isinstance(binding, Binding) and binding.description:
                rows.setdefault(binding.key, binding.description)
        return list(rows.items())
