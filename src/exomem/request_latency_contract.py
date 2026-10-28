"""The packaged latency bars and their owning measurement contracts."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    start: str
    end: str


class LatencyBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    id: str
    surface: str
    operations: tuple[str, ...]
    profile: str
    boundary: Boundary
    population: str
    # These are the observation protocol's terminal codes, not inferred meanings.
    outcome_inclusion: tuple[Literal["ok", "error", "refused", "abstained"], ...]
    reference: str
    percentile: float = Field(gt=0, le=100)
    # Comparison and establishment are closed fields of this measurement protocol.
    comparison: Literal["<", "<="]
    threshold_ms: float | None = Field(ge=0)
    window_seconds: float = Field(gt=0)
    min_samples: int = Field(gt=0)
    source_coverage: str
    requirement: str
    provenance: str
    establishment: Literal["established", "provisional", "diagnostic", "pending"]
    observe: bool = False
    deep: bool = False

    @model_validator(mode="after")
    def pending_threshold(self) -> LatencyBar:
        if (self.threshold_ms is None) != (self.establishment == "pending"):
            raise ValueError("Only pending bars have a null threshold")
        return self

    def covers(self, tool: str, deep: bool) -> bool:
        """Whether this bar certifies a call to `tool` with this `deep` flag.

        The one coverage rule: bar selection, the canary's pre-connection check and
        the summarizer's match all ask it.
        """
        # '*' is the contract schema's fallback operation token, never a word classifier.
        return self.deep == deep and (tool in self.operations or "*" in self.operations)


class LatencyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str
    references: dict[str, dict[str, Any]]
    bars: tuple[LatencyBar, ...]

    @model_validator(mode="after")
    def unique_ids(self) -> LatencyContract:
        if len({bar.id for bar in self.bars}) != len(self.bars):
            raise ValueError("Latency bar identities must be unique")
        if any(bar.reference not in self.references for bar in self.bars):
            raise ValueError("Every bar must resolve its reference")
        return self

    def bar(self, bar_id: str) -> LatencyBar:
        return next(bar for bar in self.bars if bar.id == bar_id)

    def observation_bar(self, *, surface: str, tool: str, deep: bool) -> LatencyBar | None:
        covering = [
            bar
            for bar in self.bars
            if bar.observe and bar.surface == surface and bar.covers(tool, deep)
        ]
        # A bar that lists the tool beats one that covers it only as the fallback.
        return min(covering, key=lambda bar: tool not in bar.operations, default=None)


@lru_cache(maxsize=1)
def load_contract() -> LatencyContract:
    return LatencyContract.model_validate_json(
        files("exomem").joinpath("request_latency_contract.json").read_text(encoding="utf-8")
    )
