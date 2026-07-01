from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str
    secret_key: str = "change-me-in-production"
    admin_token: str = "change-me-admin-token"
    app_env: str = "development"
    app_url: str = "http://localhost:8000"

    payos_client_id: str = ""
    payos_api_key: str = ""
    payos_checksum_key: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()