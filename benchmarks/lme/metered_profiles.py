"""Explicit diagnostic model contracts; historical reader/judge stays pinned."""

from dataclasses import asdict, dataclass

JUDGE_MODEL = "gpt-4o-2024-08-06"
SOL_MODEL = "gpt-5.6-sol"
GLM_MODEL = "z-ai/glm-5.3-flash"
GLM_WIRE_MODEL = "z-ai/glm-5.3-flash-20260826"
GLM_TOKENIZER_SHA256 = "19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d"


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
    provider_name: str = "OpenAI"
    provider_slug: str = "openai"
    tokenizer_sha256: str | None = None
    canonical_model: str | None = None
    response_model_alias: str | None = None
    provider_metadata_required: bool = False

    def wire_model(self, transport: str) -> str:
        if self.canonical_model is not None:
            return self.canonical_model
        return f"openai/{self.model}" if transport == "openrouter" and "/" not in self.model else self.model


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
    if model == GLM_MODEL and transport == "openrouter":
        # Regular prices, independent of the temporary September promotion.
        return ModelProfile(model, .15, .03, .15, .5, "high",
            "https://openrouter.ai/z-ai/glm-5.3-flash", tokenizer="huggingface-json",
            tokenizer_source="https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/"
                "eb9eb208eb0d988989d07a6a12d0fdeb5f52574a/tokenizer.json",
            provider_name="Z.AI", provider_slug="z-ai/fp8",
            tokenizer_sha256=GLM_TOKENIZER_SHA256, canonical_model=GLM_WIRE_MODEL,
            response_model_alias=GLM_MODEL,
            provider_metadata_required=True)
    raise ValueError("verified model/transport profile is unavailable")


def model_contract(agent_model: str, transport: str) -> dict:
    """Frozen role/rate identities; a change requires a fresh prepared run."""
    return {"agent": asdict(model_profile(agent_model, transport)),
            "judge": asdict(model_profile(JUDGE_MODEL, transport))}
