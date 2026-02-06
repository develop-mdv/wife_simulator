"""
Settings Manager for runtime configuration.
Loads defaults from ENV, overlays DB values, and provides typed access.
"""

import time
import logging
from typing import Optional, Any
from zoneinfo import ZoneInfo

from .db import Database

logger = logging.getLogger(__name__)


class SettingsManager:
    """
    Central class for runtime configuration management.
    
    Priority: DB settings > ENV defaults
    Changes made via admin bot are persisted to DB.
    """
    
    # Default values (fallbacks if neither DB nor ENV has value)
    DEFAULTS = {
        "ai_enabled": "false",  # OFF by default, enable via admin bot
        "pause_until_ts": "0",
        "target_user_id": "",
        "target_username": "",
        "quiet_hours_start": "",
        "quiet_hours_end": "",
        "timezone": "Europe/Moscow",
        "quiet_mode": "queue",
        "context_turns": "40",
        "rate_limit_count": "4",
        "rate_limit_window": "30",
        "model_name": "gemini-2.5-flash",
        "style_profile": "",
        "manual_override_pause_minutes": "15",
    }
    
    def __init__(self, db: Database, env_config: Optional[dict] = None):
        """
        Initialize settings manager.
        
        Args:
            db: Database instance for persistence
            env_config: Optional dict of ENV defaults to apply
        """
        self.db = db
        self._cache: dict[str, str] = {}
        
        # Load defaults
        self._cache = self.DEFAULTS.copy()
        
        # Apply ENV overrides
        if env_config:
            for key, value in env_config.items():
                if value is not None and value != "":
                    self._cache[key] = str(value)
        
        # Apply DB overrides (highest priority)
        db_settings = self.db.get_all_settings()
        for key, value in db_settings.items():
            self._cache[key] = value
        
        logger.info(f"✓ SettingsManager initialized with {len(db_settings)} DB overrides")
    
    def get_str(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get string setting value."""
        value = self._cache.get(key)
        if value is None or value == "":
            return default
        return value
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer setting value."""
        value = self._cache.get(key)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError:
            logger.warning(f"Invalid int value for {key}: {value}")
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean setting value."""
        value = self._cache.get(key)
        if value is None or value == "":
            return default
        return value.lower() in ("true", "1", "yes", "on")
    
    def get_timezone(self) -> ZoneInfo:
        """Get timezone as ZoneInfo object."""
        tz_name = self.get_str("timezone", "Europe/Moscow")
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning(f"Invalid timezone {tz_name}, using Europe/Moscow")
            return ZoneInfo("Europe/Moscow")
    
    def set(self, key: str, value: Any) -> None:
        """Set a setting value (writes to DB and cache)."""
        str_value = str(value)
        self._cache[key] = str_value
        self.db.set_setting(key, str_value)
        logger.info(f"Setting updated: {key}={str_value[:50]}{'...' if len(str_value) > 50 else ''}")
    
    def delete(self, key: str) -> None:
        """Delete a setting (removes from DB, reverts to default in cache)."""
        self.db.delete_setting(key)
        if key in self.DEFAULTS:
            self._cache[key] = self.DEFAULTS[key]
        elif key in self._cache:
            del self._cache[key]
    
    def reload_from_db(self) -> None:
        """Reload all settings from DB."""
        db_settings = self.db.get_all_settings()
        # Reset to defaults first
        self._cache = self.DEFAULTS.copy()
        # Apply DB values
        for key, value in db_settings.items():
            self._cache[key] = value
        logger.info("Settings reloaded from DB")
    
    def get_all(self) -> dict[str, str]:
        """Get all current settings."""
        return self._cache.copy()
    
    # ========================
    # Convenience Methods
    # ========================
    
    def is_ai_enabled(self) -> bool:
        """Check if AI responses are enabled."""
        return self.get_bool("ai_enabled", True)
    
    def is_paused(self) -> bool:
        """Check if currently paused."""
        pause_until = self.get_int("pause_until_ts", 0)
        if pause_until <= 0:
            return False
        return time.time() < pause_until
    
    def get_pause_remaining_seconds(self) -> int:
        """Get remaining pause time in seconds."""
        pause_until = self.get_int("pause_until_ts", 0)
        if pause_until <= 0:
            return 0
        remaining = pause_until - time.time()
        return max(0, int(remaining))
    
    def set_pause(self, duration_seconds: int) -> int:
        """Set pause for specified duration. Returns pause end timestamp."""
        pause_until = int(time.time()) + duration_seconds
        self.set("pause_until_ts", pause_until)
        return pause_until
    
    def clear_pause(self) -> None:
        """Clear any active pause."""
        self.set("pause_until_ts", 0)
    
    def set_manual_override_pause(self) -> None:
        """Set auto-pause after manual message (outgoing from user account)."""
        minutes = self.get_int("manual_override_pause_minutes", 15)
        if minutes > 0:
            pause_until = int(time.time()) + (minutes * 60)
            self.set("pause_until_ts", pause_until)
            logger.info(f"🤫 Manual override: auto-paused for {minutes} minutes")
    
    def should_respond(self) -> tuple[bool, str]:
        """
        Check if bot should respond to messages.
        
        Returns:
            Tuple of (should_respond: bool, reason: str)
        """
        if not self.is_ai_enabled():
            return False, "AI отключен"
        
        if self.is_paused():
            remaining = self.get_pause_remaining_seconds()
            minutes = remaining // 60
            return False, f"Пауза ({minutes} мин. осталось)"
        
        return True, "OK"
