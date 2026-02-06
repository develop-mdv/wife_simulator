"""
Configuration module for tg-wife-ai.
Loads and validates all environment variables.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class Config:
    # Required Telegram credentials
    tg_api_id: int
    tg_api_hash: str
    
    # Required Gemini credentials
    gemini_api_key: str
    
    # Target user (one of these must be set)
    target_user_id: Optional[int]
    target_username: Optional[str]
    
    # OpenAI settings
    model_name: str = "gemini-2.5-flash"
    style_profile: str = ""
    
    # Context settings
    context_turns: int = 40
    
    # Timezone and quiet hours
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Moscow"))
    quiet_hours_start: Optional[str] = None  # "23:00"
    quiet_hours_end: Optional[str] = None    # "08:00"
    quiet_mode: str = "queue"  # "ignore" or "queue"
    
    # Rate limiting
    rate_limit_count: int = 4
    rate_limit_window_sec: int = 30
    
    # Paths
    data_dir: str = "/app/data"
    
    @property
    def session_path(self) -> str:
        return os.path.join(self.data_dir, "telegram")
    
    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "messages.db")


def _get_env(key: str, required: bool = False, default: Optional[str] = None) -> Optional[str]:
    """Get environment variable with optional requirement check."""
    value = os.environ.get(key, default)
    if required and not value:
        print(f"❌ Error: Required environment variable {key} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _get_int_env(key: str, default: int) -> int:
    """Get integer environment variable."""
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"❌ Error: {key} must be an integer, got: {value}", file=sys.stderr)
        sys.exit(1)


def load_config() -> Config:
    """Load and validate configuration from environment variables."""
    
    # Required credentials
    tg_api_id_str = _get_env("TG_API_ID", required=True)
    try:
        tg_api_id = int(tg_api_id_str)
    except (ValueError, TypeError):
        print(f"❌ Error: TG_API_ID must be an integer, got: {tg_api_id_str}", file=sys.stderr)
        sys.exit(1)
    
    tg_api_hash = _get_env("TG_API_HASH", required=True)
    gemini_api_key = _get_env("GEMINI_API_KEY", required=True)
    
    # Target user - at least one must be set
    target_user_id_str = _get_env("TARGET_USER_ID")
    target_username = _get_env("TARGET_USERNAME")
    
    target_user_id = None
    if target_user_id_str:
        try:
            target_user_id = int(target_user_id_str)
        except ValueError:
            print(f"❌ Error: TARGET_USER_ID must be an integer, got: {target_user_id_str}", file=sys.stderr)
            sys.exit(1)
    
    if not target_user_id and not target_username:
        print("❌ Error: Either TARGET_USER_ID or TARGET_USERNAME must be set.", file=sys.stderr)
        sys.exit(1)
    
    # Timezone
    tz_name = _get_env("TIMEZONE", default="Europe/Moscow")
    try:
        timezone = ZoneInfo(tz_name)
    except Exception as e:
        print(f"❌ Error: Invalid timezone '{tz_name}': {e}", file=sys.stderr)
        sys.exit(1)
    
    # Quiet mode validation
    quiet_mode = _get_env("QUIET_MODE", default="queue")
    if quiet_mode not in ("ignore", "queue"):
        print(f"❌ Error: QUIET_MODE must be 'ignore' or 'queue', got: {quiet_mode}", file=sys.stderr)
        sys.exit(1)
    
    # Style profile (can be multiline)
    style_profile = _get_env("STYLE_PROFILE", default="")
    
    config = Config(
        tg_api_id=tg_api_id,
        tg_api_hash=tg_api_hash,
        gemini_api_key=gemini_api_key,
        target_user_id=target_user_id,
        target_username=target_username,
        model_name=_get_env("MODEL_NAME", default="gemini-2.5-flash"),
        style_profile=style_profile,
        context_turns=_get_int_env("CONTEXT_TURNS", 40),
        timezone=timezone,
        quiet_hours_start=_get_env("QUIET_HOURS_START"),
        quiet_hours_end=_get_env("QUIET_HOURS_END"),
        quiet_mode=quiet_mode,
        rate_limit_count=_get_int_env("RATE_LIMIT_COUNT", 4),
        rate_limit_window_sec=_get_int_env("RATE_LIMIT_WINDOW_SEC", 30),
        data_dir=_get_env("DATA_DIR", default="/app/data"),
    )
    
    return config


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance, loading it if necessary."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
