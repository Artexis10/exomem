"""Explicit diagnostic model contracts; historical reader/judge stays pinned."""

from dataclasses import asdict, dataclass

JUDGE_MODEL = "gpt-4o-2024-08-06"
SOL_MODEL = "gpt-5.6-sol"


@dataclass(frozen=True)
class ModelProfile:
    model: str
    input_rate: float
    cached_rate: float
    cache_write_rate: float
    output_rate: float
    reasoning_effort: str | None
    pricing_source: str
    pricing_verified_on: str = "2026-09-08"
    tokenizer: str = "o200k_base"
    tokenizer_source: str = (
        "https://github.com/openai/tiktoken/blob/"
        "212b893ba940cba53476851103d2e5c1d0020c6e/tiktoken/model.py"
    )

    def wire_model(self, transport: str) -> str:
        return f"openai/{self.model}" if transport == "openrouter" else self.model


def model_profile(model: str, transport: str) -> ModelProfile:
    if transport not in {"openai", "openrouter"}:
        raise ValueError("unknown metered transport")
    if model == JUDGE_MODEL:
        return ModelProfile(model, 2.5, 1.25, 2.5, 10, None,
                            "https://developers.openai.com/api/docs/models/gpt-4o")
    if model == SOL_MODEL and transport == "openrouter":
        # Promotional standard OpenAI endpoint rates, below the 272k threshold.
        # The 128k accounting envelope remains enforced by the backend.
        return ModelProfile(model, 2, .2, 2.5, 10, "low",
                            "https://openrouter.ai/openai/gpt-5.6-sol")
    raise ValueError("verified model/transport profile is unavailable")


def model_contract(agent_model: str, transport: str) -> dict:
    """Frozen role/rate identities; a change requires a fresh prepared run."""
    return {"agent": asdict(model_profile(agent_model, transport)),
            "judge": asdict(model_profile(JUDGE_MODEL, transport))}
