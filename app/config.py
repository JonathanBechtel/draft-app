# app/config.py
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./draftguru.db"
    secret_key: str = "changeme"

    env: Literal["dev","test","prod"] = "dev"
    debug: bool = False                # optional override
    log_level: str = "INFO"
    access_log: bool = True
    sql_echo: bool = False             # show SQL in logs (dev only)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev" or self.debug is True
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",          # no prefix
        case_sensitive=False    # <-- accept LOG_LEVEL as log_level, etc.
    )

settings = Settings()
