from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = Field(alias="BOT_TOKEN")
    admin_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, alias="ADMIN_IDS"
    )

    headless: bool = Field(default=True, alias="HEADLESS")
    browser_channel: str | None = Field(default=None, alias="BROWSER_CHANNEL")

    collect_days: int = Field(default=7, alias="COLLECT_DAYS")
    max_sessions: int = Field(default=20, alias="MAX_SESSIONS")
    sweep_minutes: int = Field(default=10, alias="SWEEP_MINUTES")

    db_path: str = Field(default=str(DATA_DIR / "bot.sqlite3"), alias="DB_PATH")
    proxies_file: str = Field(default=str(BASE_DIR / "proxies.json"), alias="PROXIES_FILE")
    sessions_dir: str = Field(default=str(SESSIONS_DIR), alias="SESSIONS_DIR")

    qa_token: str = Field(default="", alias="QA_TOKEN")

    # Unlocks the Push-collection features. Users run /unlock <key>.
    access_key: str = Field(default="", alias="ACCESS_KEY")

    # Live browser control (screencast) web server
    webcontrol_host: str = Field(default="0.0.0.0", alias="WEBCONTROL_HOST")
    webcontrol_port: int = Field(default=8080, alias="WEBCONTROL_PORT")
    public_url: str = Field(default="http://localhost:8080", alias="PUBLIC_URL")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        return v

    @field_validator("browser_channel", mode="before")
    @classmethod
    def _empty_channel(cls, v):
        if v is None or not str(v).strip():
            return None
        return str(v).strip()

    @property
    def collect_seconds(self) -> int:
        return int(self.collect_days * 24 * 3600)


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
