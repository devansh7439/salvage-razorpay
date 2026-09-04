"""Runtime configuration.

Every external dependency is optional. The system runs end to end with no
credentials at all - the Razorpay client falls back to deterministic fixtures
and the LLM adapter falls back to templates - so a reviewer can clone the repo
and see the full pipeline work without signing up for anything.

Adding credentials upgrades those paths to live calls without a code change.
That is not just a convenience: it means the demo cannot fail in front of a
panel because a network call timed out.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


class Settings(BaseSettings):
    """Configuration, read from the environment or a local `.env`.

    Attributes:
        razorpay_key_id: Test Mode key id. Absent means fixture mode.
        razorpay_key_secret: Test Mode key secret.
        razorpay_webhook_secret: Shared secret for webhook signature
            verification. Absent means signature checks are skipped, which is
            logged loudly because it is unsafe outside local development.
        llm_base_url: OpenAI-compatible chat-completions base URL. Works with
            Groq, OpenRouter, Together, Gemini's compatibility endpoint, local
            Ollama, and others - the contract is the same across all of them.
        llm_api_key: Bearer token for that endpoint.
        llm_model: Model identifier to request.
        llm_reasoning_effort: Optional reasoning budget hint ("low"/"medium"/
            "high"). Only sent when set, because it is not part of the base
            OpenAI-compatible contract and some providers reject unknown fields.
            Needed for reasoning models, which otherwise spend the whole token
            budget thinking and return empty content.
        database_path: SQLite file location.
        model_path: Serialised propensity model.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: str = ""

    database_path: Path = DATA_DIR / "salvage.db"
    model_path: Path = DATA_DIR / "recovery_model.joblib"

    @property
    def razorpay_live(self) -> bool:
        """Whether real Razorpay API calls are possible."""
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def llm_live(self) -> bool:
        """Whether real LLM calls are possible."""
        return bool(self.llm_base_url and self.llm_api_key)


settings = Settings()
