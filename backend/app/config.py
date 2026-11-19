from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    aiops_version: str = "0.2.4"
    aiops_hostname: str = "aiops.local"

    postgres_db: str = "aiops"
    postgres_user: str = "aiops"
    postgres_password: str = "aiops"
    database_host: str = "postgres"
    database_port: int = 5432
    database_url_override: str | None = None

    redis_url: str = "redis://redis:6379/0"

    secret_key: str = "dev-secret"
    fernet_key: str = ""
    session_hours: int = 12

    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    llm_timeout_seconds: int = 240

    fixtures_dir: str = "/data/fixtures"
    geoip_dir: str = "/data/geoip"
    logproxy_url: str = "http://logproxy:2375"
    update_dir: str = "/data/update"
    aiops_update_repo: str = "mbechc/AI-Assisted-Ops"
    aiops_update_channel: str = "production"
    aiops_connectivity_interval_min: int = 15
    compose_project: str = Field("aiops", validation_alias=AliasChoices("compose_project_name", "compose_project"))

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.database_host}:{self.database_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
