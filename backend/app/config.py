from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/localsync.db"
    navidrome_url: str = ""
    navidrome_username: str = ""
    navidrome_password: str = ""
    sync_interval_minutes: int = 60

    class Config:
        env_prefix = ""
        env_file = ".env"


settings = Settings()
