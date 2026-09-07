"""
Application configuration settings.

Conforms to UltrionTech-Backend-Template config/ specification.
Preserves 100% backward compatibility with existing environment variables
while adding target architecture settings (asyncpg URL, cutover toggles).
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for HMS application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database settings
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_db: str = "HMSstage"
    postgres_sslmode: str = "require"
    database_url: str | None = None

    # JWT / Auth settings
    jwt_secret: str = "change-me-in-production-ultrion-hms"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    # Super admin credentials
    super_admin_email: str = "ultriohms@ultriontech.com"
    super_admin_password: str = "UltrionHMS"

    # CORS settings
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://hms.ultriontech.com,"
        "https://ashy-plant-02ffd7210.azurestaticapps.net"
    )

    # Server settings
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    # Migration / Controlled cutover feature flags (default False = legacy behavior)
    use_migrated_vitals: bool = False
    use_migrated_analytics: bool = False
    use_migrated_patients: bool = False
    use_migrated_appointments: bool = False
    use_migrated_doctors: bool = False
    use_migrated_beds: bool = False
    use_migrated_inpatient: bool = False
    use_migrated_billing: bool = False
    use_migrated_laboratory: bool = False
    use_migrated_dms: bool = False
    use_migrated_radiology: bool = False
    use_migrated_ot: bool = False
    use_migrated_equipment: bool = False
    use_migrated_pharmacy: bool = False
    use_migrated_mis: bool = False
    use_migrated_masters: bool = False
    use_migrated_admin: bool = False
    use_migrated_hospitals: bool = False


    @property
    def sqlalchemy_database_url(self) -> str:
        """Synchronous SQLAlchemy connection URL using psycopg2."""
        if self.database_url and "psycopg2" in self.database_url:
            return self.database_url
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?sslmode={self.postgres_sslmode}"
        )

    @property
    def asyncpg_database_url(self) -> str:
        """Asynchronous SQLAlchemy connection URL using asyncpg."""
        password = quote_plus(self.postgres_password)
        # asyncpg uses ssl parameter directly (e.g. ssl=require)
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?ssl={self.postgres_sslmode}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse comma-separated origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
