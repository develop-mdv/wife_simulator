"""
Configuration module for tg-wife-ai.
Simplified for multi-user mode: only Admin Bot Token and Gemini API Key are required globally.
Personal credentials are provided by users via bot.
"""

import os
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    # Required global credentials
    gemini_api_key: str
    admin_bot_token: str
    
    # Paths
    data_dir: str = "/app/data"
    
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


def load_config() -> Config:
    """Load and validate configuration from environment variables."""
    
    gemini_api_key = _get_env("GEMINI_API_KEY", required=True)
    admin_bot_token = _get_env("ADMIN_BOT_TOKEN", required=True)
    
    # Optional warnings for deprecated vars
    if os.environ.get("TG_API_ID"):
        print("ℹ️ Note: TG_API_ID in .env is ignored in multi-user mode. Users provide it via bot.", file=sys.stderr)
    
    config = Config(
        gemini_api_key=gemini_api_key,
        admin_bot_token=admin_bot_token,
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
