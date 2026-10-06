"""Runtime configuration, read from environment variables (prefix MAJORDOME_)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAJORDOME_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://majordome:majordome@localhost:5432/majordome"

    # Matrix (bridged chat apps)
    matrix_homeserver: str = "http://synapse:8008"
    matrix_server_name: str = "majordome.internal"
    matrix_user: str = "me"
    matrix_password: str = ""
    # Invites are auto-accepted only from users matching this regex (bridge bots and ghosts).
    matrix_trusted_inviters: str = (
        r"^@(whatsappbot|discordbot|whatsapp_[^:]+|discord_[^:]+):majordome\.internal$"
    )

    # Email accounts definition (JSON list) and OAuth token storage.
    email_accounts_file: Path = Path("/run/secrets/email.json")
    oauth_token_dir: Path = Path("/var/lib/majordome/oauth")

    # Web UI
    ui_password_hash: str = ""  # argon2 hash; set with `majordome set-password`
    session_secret: str = ""
    session_max_age_s: int = 7 * 24 * 3600
    web_dist: Path = Path("/app/web")


@lru_cache
def settings() -> Settings:
    return Settings()
