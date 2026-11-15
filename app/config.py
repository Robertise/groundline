from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_DB_FILES = {
    "mock": "./data/groundline.db",
    "dry_run": "./data/groundline-dryrun.db",
    "live": "./data/groundline-live.db",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROUNDLINE_", env_file=".env", extra="ignore"
    )
    mode: str = "mock"  # one of: mock, dry_run, live (only mock is used in P1)
    db_path: str = ""  # empty = ./data/<default file for the mode>
    timezone: str = "Asia/Ho_Chi_Minh"
    daily_send_cap: int = 20
    min_seconds_between_sends: float = 30.0
    max_consecutive_errors: int = 3
    send_lock_seconds: int = 600

    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "http://127.0.0.1:8000/callback"
    secret_key: str = ""  # Fernet key for token encryption
    aws_region: str = "ap-southeast-1"
    bedrock_model_id: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_profile: str = ""
    llm_input_usd_per_mtok: float = 1.0  # verify against Bedrock pricing; shown as an estimate
    llm_output_usd_per_mtok: float = 5.0
    fixtures_path: str = "./fixtures/demo_x.json"
    live_allowed_handles: str = ""  # comma-separated usernames; when set, live mode sends only to these

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"mock", "dry_run", "live"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v!r}")
        return v

    @model_validator(mode="after")
    def default_db_path_for_mode(self):
        if not self.db_path:
            self.db_path = DEFAULT_DB_FILES[self.mode]
        return self


def missing_live_settings(s: Settings) -> list[str]:
    if s.mode == "mock":
        return []
    missing = []
    if not s.x_client_id:
        missing.append("x_client_id")
    if not s.x_client_secret:
        missing.append("x_client_secret")
    if not s.secret_key:
        missing.append("secret_key")
    return missing


def get_settings() -> Settings:
    return Settings()
