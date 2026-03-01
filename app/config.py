"""Application configuration loaded from environment variables."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Meta API
    meta_access_token: str = ""
    meta_ads_api_version: str = "v21.0"
    meta_api_base_url: str = "https://graph.facebook.com"

    # Database
    database_url: str = "sqlite:///./app.db"

    # Whisper
    whisper_model: str = "base"

    # ffmpeg
    ffmpeg_path: str = "ffmpeg"

    # Downloader
    download_timeout_seconds: int = 60
    download_max_retries: int = 3

    # Data storage root
    data_dir: str = "./data"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
