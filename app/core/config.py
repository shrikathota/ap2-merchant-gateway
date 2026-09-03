from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "ap2-merchant-gateway"
    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change_me_to_a_random_secret_at_least_32_chars"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ap2_gateway"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Razorpay
    razorpay_key_id: str = "rzp_test_XXXXXXXXXXXX"
    razorpay_key_secret: str = "your_razorpay_secret_here"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
