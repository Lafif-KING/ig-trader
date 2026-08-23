"""Configuration and settings for IG Trader using Pydantic."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables or .env file."""

    # IG API Credentials
    ig_api_key: str = ""
    ig_identifier: str = ""
    ig_password: str = ""

    # IG API Configuration
    ig_demo: bool = True
    ig_base_url: str = "https://demo-api.ig.com/gateway/deal"
    ig_expected_demo_account_id: str = ""
    demo_operator_local: bool = False

    # Logging
    log_level: str = "INFO"

    # Session
    session_timeout_seconds: int = 21600  # 6 hours

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


# Global settings instance
settings = Settings()
