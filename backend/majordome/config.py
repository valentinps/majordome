"""Runtime configuration, read from environment variables (prefix MAJORDOME_)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAJORDOME_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://majordome:majordome@localhost:5432/majordome"

    # Matrix (bridged chat apps): Majordome is a passive appservice, see ingest/matrix.py.
    matrix_homeserver: str = "http://synapse:8008"
    matrix_server_name: str = "majordome.internal"
    matrix_user: str = "me"  # the user's Matrix account (only used to recognise it)
    matrix_as_token: str = ""
    matrix_hs_token: str = ""
    matrix_appservice_port: int = 8009
    bridge_status_token: str = ""  # shared secret in the bridges' status_endpoint URL
    # Bridge bots and ghosts: the appservice namespace, and who room state is read as.
    matrix_bridge_users: str = (
        r"^@(whatsappbot|discordbot|whatsapp_[^:]+|discord_[^:]+):majordome\.internal$"
    )

    # Email accounts definition (JSON list) and OAuth token storage.
    email_accounts_file: Path = Path("/run/secrets/email/accounts.json")
    oauth_token_dir: Path = Path("/var/lib/majordome/oauth")

    # Web UI
    ui_password_hash: str = ""  # argon2 hash; overrides the file below when set
    ui_password_hash_file: Path = Path("/run/secrets/ui/password_hash")  # `majordome set-password`
    session_secret: str = ""
    session_max_age_s: int = 7 * 24 * 3600
    web_dist: Path = Path("/app/web")


@lru_cache
def settings() -> Settings:
    return Settings()
