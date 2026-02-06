"""
User data model and storage for multi-user support.
Each user has their own Telethon session and settings.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class UserState(Enum):
    """User onboarding/operation states."""
    # Onboarding states
    NEW = "new"                          # Just started, show welcome
    AWAITING_API_ID = "awaiting_api_id"
    AWAITING_API_HASH = "awaiting_api_hash"
    AWAITING_PHONE = "awaiting_phone"
    AWAITING_CODE = "awaiting_code"
    AWAITING_2FA = "awaiting_2fa"
    AWAITING_TARGET = "awaiting_target"
    
    # Operational states
    READY = "ready"                      # Fully configured, can operate
    AWAITING_SETTING = "awaiting_setting"  # Waiting for setting value input


@dataclass
class UserData:
    """Data model for a single user."""
    user_id: int
    
    # Telegram API credentials
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    phone: Optional[str] = None
    
    # Session data (serialized StringSession)
    session_string: Optional[str] = None
    
    # Target user
    target_user_id: Optional[int] = None
    target_username: Optional[str] = None
    target_name: Optional[str] = None  # Display name
    
    # State
    state: UserState = UserState.NEW
    pending_setting: Optional[str] = None  # Which setting we're waiting for
    
    # Settings
    ai_enabled: bool = False
    pause_until_ts: int = 0
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    quiet_mode: str = "queue"
    timezone: str = "Europe/Moscow"
    context_turns: int = 40
    style_profile: str = ""
    
    # Metadata
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_activity_ts: int = 0
    
    # Temporary data (not persisted)
    phone_code_hash: Optional[str] = None  # For Telethon auth
    
    def is_configured(self) -> bool:
        """Check if user has completed onboarding."""
        return (
            self.api_id is not None and
            self.api_hash is not None and
            self.session_string is not None and
            self.target_user_id is not None
        )
    
    def is_paused(self) -> bool:
        """Check if AI is currently paused."""
        if self.pause_until_ts <= 0:
            return False
        return time.time() < self.pause_until_ts
    
    def should_respond(self) -> tuple[bool, str]:
        """Check if AI should respond to messages."""
        if not self.ai_enabled:
            return False, "AI выключен"
        if self.is_paused():
            remaining = int((self.pause_until_ts - time.time()) / 60)
            return False, f"Пауза ({remaining} мин.)"
        return True, "OK"
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "user_id": self.user_id,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "phone": self.phone,
            "session_string": self.session_string,
            "target_user_id": self.target_user_id,
            "target_username": self.target_username,
            "target_name": self.target_name,
            "state": self.state.value,
            "pending_setting": self.pending_setting,
            "ai_enabled": 1 if self.ai_enabled else 0,
            "pause_until_ts": self.pause_until_ts,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "quiet_mode": self.quiet_mode,
            "timezone": self.timezone,
            "context_turns": self.context_turns,
            "style_profile": self.style_profile,
            "created_at": self.created_at,
            "last_activity_ts": self.last_activity_ts,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "UserData":
        """Create from dictionary."""
        return cls(
            user_id=data["user_id"],
            api_id=data.get("api_id"),
            api_hash=data.get("api_hash"),
            phone=data.get("phone"),
            session_string=data.get("session_string"),
            target_user_id=data.get("target_user_id"),
            target_username=data.get("target_username"),
            target_name=data.get("target_name"),
            state=UserState(data.get("state", "new")),
            pending_setting=data.get("pending_setting"),
            ai_enabled=bool(data.get("ai_enabled", 0)),
            pause_until_ts=data.get("pause_until_ts", 0),
            quiet_hours_start=data.get("quiet_hours_start"),
            quiet_hours_end=data.get("quiet_hours_end"),
            quiet_mode=data.get("quiet_mode", "queue"),
            timezone=data.get("timezone", "Europe/Moscow"),
            context_turns=data.get("context_turns", 40),
            style_profile=data.get("style_profile", ""),
            created_at=data.get("created_at", int(time.time())),
            last_activity_ts=data.get("last_activity_ts", 0),
        )
