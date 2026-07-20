from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/localsync.db"
    navidrome_url: str = ""
    navidrome_username: str = ""
    navidrome_password: str = ""
    sync_interval_minutes: int = 60
    jwt_secret_key: str = "your-secret-key-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    class Config:
        env_prefix = ""
        env_file = ".env"


settings = Settings()
