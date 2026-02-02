# app/config.py
from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    env: Literal["dev", "stage", "prod"] = Field(
        validation_alias=AliasChoices("ENV", "APP_ENV")
    )
    debug: bool = False
    log_level: str = "INFO"
    access_log: bool = True
    sql_echo: bool = True
    auto_init_db: bool = True

    # Gemini API settings
    gemini_api_key: Optional[str] = None  # General/image generation key
    gemini_summarization_api_key: Optional[str] = (
        None  # Separate key for RSS summarization
    )

    # Image generation settings
    image_gen_size: str = "512"  # Options: "512", "1K", "2K"
    image_gen_quality: str = "standard"  # Options: "draft", "standard", "high"
    default_image_style: str = Field(
        default="default",
        validation_alias=AliasChoices("DEFAULT_IMAGE_STYLE", "IMAGE_STYLE_DEFAULT"),
        description="Global default player image style for UI rendering.",
    )

    # S3 storage settings
    s3_bucket_name: Optional[str] = None
    s3_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
    )
    s3_access_key_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_access_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    s3_public_url_base: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("S3_PUBLIC_URL_BASE", "CDN_PUBLIC_URL_BASE"),
        description="Optional base URL (CDN) for serving S3 objects.",
    )
    s3_upload_acl: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "S3_UPLOAD_ACL",
        ),
        description=(
            "Optional S3 ACL to apply on upload (e.g., 'public-read'). "
            "If unset, objects rely on bucket policy/permissions for readability."
        ),
    )
    image_storage_local: bool = False  # True = local filesystem (dev only)

    # Email settings (for user invitations and password resets)
    resend_api_key: Optional[str] = None
    email_from_address: str = "noreply@draftguru.dev"
    app_base_url: str = "http://localhost:8000"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev" or self.debug is True

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
