"""Settings read from the environment and `.env`. See `.env.example`."""

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-haiku-5-5"


class Settings(BaseSettings):
    # extra="ignore": a teammate's extra .env keys must not crash every entry point.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://leo:leo@localhost:5432/leo"
    anthropic_api_key: str | None = None
    jev_api_key: str | None = None
    leo_model_extract: str = DEFAULT_MODEL
    leo_model_draft: str = DEFAULT_MODEL
    leo_today: date | None = None
    # LLM response cache (ROADMAP B3). Offline: a cache miss raises instead of calling the API.
    leo_llm_cache_dir: Path = Path("data/llm_cache")
    leo_llm_offline: bool = False
    # Raw-file store (ROADMAP A3): ingested files, content-addressed. The DB holds references.
    leo_raw_dir: Path = Path("var/raw")
    # The API's tenant (ROADMAP C3): the seed's slug is the dataset name.
    leo_tenant: str = "brightwater-v1"
    # Recorded as `action.approver` (no login yet; the schema requires an approver).
    leo_approver: str = "reviewer"
    # Gmail sync (ROADMAP A8). The key encrypts OAuth tokens at rest (Fernet); the synced
    # mailbox gets its own tenant, so it never doubles the seeded one.
    leo_secret_key: str | None = None
    leo_gmail_tenant: str = "gmail-demo"
    leo_google_client_secrets: Path = Path("var/google_client_secret.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
