"""
Database module for multi-user support.
Stores user data, messages, and pending messages per user.
"""

import sqlite3
import time
from typing import Optional
from contextlib import contextmanager

from .user_data import UserData, UserState


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def _get_conn(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def _init_db(self):
        """Initialize database tables."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    api_id INTEGER,
                    api_hash TEXT,
                    phone TEXT,
                    session_string TEXT,
                    target_user_id INTEGER,
                    target_username TEXT,
                    target_name TEXT,
                    state TEXT DEFAULT 'new',
                    pending_setting TEXT,
                    ai_enabled INTEGER DEFAULT 0,
                    pause_until_ts INTEGER DEFAULT 0,
                    quiet_hours_start TEXT,
                    quiet_hours_end TEXT,
                    quiet_mode TEXT DEFAULT 'queue',
                    timezone TEXT DEFAULT 'Europe/Moscow',
                    context_turns INTEGER DEFAULT 40,
                    style_profile TEXT DEFAULT '',
                    created_at INTEGER,
                    last_activity_ts INTEGER DEFAULT 0
                )
            """)
            
            # Message history table (per user)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    chat_id INTEGER,
                    message_id INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
                )
            """)
            
            # Pending incoming messages (per user, for quiet hours)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_incoming (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, chat_id, message_id),
                    FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
                )
            """)
            
            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_owner ON messages(owner_user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_owner ON pending_incoming(owner_user_id)")
    
    # ========================
    # User Methods
    # ========================
    
    def get_user(self, user_id: int) -> Optional[UserData]:
        """Get user by ID."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return UserData.from_dict(dict(row))
            return None
    
    def save_user(self, user: UserData) -> None:
        """Save or update user."""
        data = user.to_dict()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (
                    user_id, api_id, api_hash, phone, session_string,
                    target_user_id, target_username, target_name, state, pending_setting,
                    ai_enabled, pause_until_ts, quiet_hours_start, quiet_hours_end,
                    quiet_mode, timezone, context_turns, style_profile,
                    created_at, last_activity_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    api_id = excluded.api_id,
                    api_hash = excluded.api_hash,
                    phone = excluded.phone,
                    session_string = excluded.session_string,
                    target_user_id = excluded.target_user_id,
                    target_username = excluded.target_username,
                    target_name = excluded.target_name,
                    state = excluded.state,
                    pending_setting = excluded.pending_setting,
                    ai_enabled = excluded.ai_enabled,
                    pause_until_ts = excluded.pause_until_ts,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    quiet_mode = excluded.quiet_mode,
                    timezone = excluded.timezone,
                    context_turns = excluded.context_turns,
                    style_profile = excluded.style_profile,
                    last_activity_ts = excluded.last_activity_ts
            """, (
                data["user_id"], data["api_id"], data["api_hash"], data["phone"],
                data["session_string"], data["target_user_id"], data["target_username"],
                data["target_name"], data["state"], data["pending_setting"],
                data["ai_enabled"], data["pause_until_ts"], data["quiet_hours_start"],
                data["quiet_hours_end"], data["quiet_mode"], data["timezone"],
                data["context_turns"], data["style_profile"], data["created_at"],
                data["last_activity_ts"]
            ))
    
    def get_all_configured_users(self) -> list[UserData]:
        """Get all users who have completed setup."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM users 
                WHERE state = 'ready' AND session_string IS NOT NULL
            """)
            return [UserData.from_dict(dict(row)) for row in cursor.fetchall()]
    
    def delete_user(self, user_id: int) -> None:
        """Delete user and their data."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE owner_user_id = ?", (user_id,))
            cursor.execute("DELETE FROM pending_incoming WHERE owner_user_id = ?", (user_id,))
            cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    
    # ========================
    # Message History Methods
    # ========================
    
    def add_message(self, owner_user_id: int, role: str, text: str, 
                    chat_id: Optional[int] = None, message_id: Optional[int] = None) -> None:
        """Add a message to history."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (owner_user_id, role, text, chat_id, message_id) VALUES (?, ?, ?, ?, ?)",
                (owner_user_id, role, text, chat_id, message_id)
            )
    
    def get_context(self, owner_user_id: int, limit: int) -> list[dict]:
        """Get recent messages for context (oldest first)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, text, timestamp 
                FROM messages 
                WHERE owner_user_id = ?
                ORDER BY id DESC 
                LIMIT ?
            """, (owner_user_id, limit))
            rows = cursor.fetchall()
            
            return [
                {"role": dict(row)["role"], "content": dict(row)["text"]}
                for row in reversed(rows)
            ]
    
    def is_message_processed(self, owner_user_id: int, chat_id: int, message_id: int) -> bool:
        """Check if a message has already been processed."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT 1 FROM messages 
                   WHERE owner_user_id = ? AND chat_id = ? AND message_id = ? AND role = 'user' 
                   LIMIT 1""",
                (owner_user_id, chat_id, message_id)
            )
            return cursor.fetchone() is not None
    
    # ========================
    # Pending Queue Methods
    # ========================
    
    def add_pending_message(self, owner_user_id: int, chat_id: int, message_id: int, text: str) -> None:
        """Add a message to pending queue."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO pending_incoming (owner_user_id, chat_id, message_id, text) VALUES (?, ?, ?, ?)",
                    (owner_user_id, chat_id, message_id, text)
                )
            except sqlite3.IntegrityError:
                pass  # Already exists
    
    def get_pending_messages(self, owner_user_id: int) -> list[dict]:
        """Get all pending messages for user."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT chat_id, message_id, text, timestamp 
                FROM pending_incoming 
                WHERE owner_user_id = ?
                ORDER BY id ASC
            """, (owner_user_id,))
            return [dict(row) for row in cursor.fetchall()]
    
    def clear_pending_messages(self, owner_user_id: int) -> None:
        """Clear all pending messages for user."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_incoming WHERE owner_user_id = ?", (owner_user_id,))
    
    def has_pending_messages(self, owner_user_id: int) -> bool:
        """Check if user has pending messages."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM pending_incoming WHERE owner_user_id = ? LIMIT 1", (owner_user_id,))
            return cursor.fetchone() is not None
