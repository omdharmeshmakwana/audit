from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FAIBLE_", env_file=REPO_ROOT / ".env")

    database_url: str = "postgresql+psycopg:///faible"
    data_dir: Path = REPO_ROOT / "data"
    # Phase 0 only: accept capture-all dumps from the extension.
    discovery_enabled: bool = True
    # HS256 key for extension JWTs; when unset, a random key is persisted to data/jwt_secret.
    jwt_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
