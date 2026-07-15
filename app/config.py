from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_db: str = "HMSstage"
    postgres_sslmode: str = "require"
    database_url: str | None = None

    jwt_secret: str = "change-me-in-production-ultrion-hms"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    super_admin_email: str = "ultriohms@ultriontech.com"
    super_admin_password: str = "UltrionHMS"

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    @property
    def sqlalchemy_database_url(self) -> str:
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?sslmode={self.postgres_sslmode}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
