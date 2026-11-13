from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROUNDLINE_", env_file=".env", extra="ignore"
    )
    mode: str = "mock"  # one of: mock, dry_run, live (only mock is used in P1)
    db_path: str = "./data/groundline.db"
    timezone: str = "Asia/Ho_Chi_Minh"
    daily_send_cap: int = 20
    min_seconds_between_sends: float = 30.0
    max_consecutive_errors: int = 3
    send_lock_seconds: int = 600

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"mock", "dry_run", "live"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v!r}")
        return v


def get_settings() -> Settings:
    return Settings()
