# app/config.py
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    env: Literal["dev", "stage", "prod"]
    debug: bool = False
    log_level: str = "INFO"
    access_log: bool = True
    sql_echo: bool = True
    auto_init_db: bool = True

    # Image generation settings
    gemini_api_key: Optional[str] = None
    image_gen_size: str = "512"  # Options: "512", "1K", "2K"
    image_gen_quality: str = "standard"  # Options: "draft", "standard", "high"

    # S3 storage settings
    s3_bucket_name: Optional[str] = None
    s3_region: str = "us-east-1"
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    image_storage_local: bool = False  # True = local filesystem (dev only)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev" or self.debug is True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
