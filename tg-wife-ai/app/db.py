"""
Database module for storing message history, pending messages, and settings.
Uses SQLite for persistent storage.
"""

import sqlite3
import time
from datetime import datetime
from typing import Optional, Any
from contextlib import contextmanager


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
            
            # Message history table (with chat_id for proper dedup)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    chat_id INTEGER,
                    message_id INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Pending incoming messages (for quiet hours queue mode)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_incoming (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
            """)
            
            # State table for tracking last processed message
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # Settings table for runtime configuration
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)
            
            # Migration: add chat_id column if missing (for existing DBs)
            cursor.execute("PRAGMA table_info(messages)")
            columns = [row[1] for row in cursor.fetchall()]
            if "chat_id" not in columns:
                cursor.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")
            
            # Migration: add chat_id to pending_incoming if missing
            cursor.execute("PRAGMA table_info(pending_incoming)")
            columns = [row[1] for row in cursor.fetchall()]
            if "chat_id" not in columns:
                cursor.execute("ALTER TABLE pending_incoming ADD COLUMN chat_id INTEGER DEFAULT 0")
    
    # ========================
    # Message History Methods
    # ========================
    
    def add_message(self, role: str, text: str, chat_id: Optional[int] = None, message_id: Optional[int] = None) -> None:
        """Add a message to history."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (role, text, chat_id, message_id) VALUES (?, ?, ?, ?)",
                (role, text, chat_id, message_id)
            )
    
    def get_context(self, limit: int) -> list[dict]:
        """Get recent messages for context (oldest first)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, text, timestamp 
                FROM messages 
                ORDER BY id DESC 
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            
            # Reverse to get chronological order
            return [
                {"role": dict(row)["role"], "content": dict(row)["text"]}
                for row in reversed(rows)
            ]
    
    def is_message_processed(self, chat_id: int, message_id: int) -> bool:
        """Check if a message has already been processed (deduplication by chat_id + message_id)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ? AND role = 'user' LIMIT 1",
                (chat_id, message_id)
            )
            return cursor.fetchone() is not None
    
    # ========================
    # Pending Queue Methods
    # ========================
    
    def add_pending_message(self, chat_id: int, message_id: int, text: str) -> None:
        """Add a message to pending queue (quiet hours)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO pending_incoming (chat_id, message_id, text) VALUES (?, ?, ?)",
                    (chat_id, message_id, text)
                )
            except sqlite3.IntegrityError:
                # Already exists, ignore
                pass
    
    def get_pending_messages(self) -> list[dict]:
        """Get all pending messages (oldest first)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT chat_id, message_id, text, timestamp 
                FROM pending_incoming 
                ORDER BY id ASC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def clear_pending_messages(self) -> None:
        """Clear all pending messages after processing."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_incoming")
    
    def has_pending_messages(self) -> bool:
        """Check if there are any pending messages."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM pending_incoming LIMIT 1")
            return cursor.fetchone() is not None
    
    def get_pending_count(self) -> int:
        """Get count of pending messages."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pending_incoming")
            return cursor.fetchone()[0]
    
    # ========================
    # Settings Methods
    # ========================
    
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value from database."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default
    
    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value in database."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO settings (key, value, updated_ts) 
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_ts = ?
            """, (key, value, int(time.time()), value, int(time.time())))
    
    def get_all_settings(self) -> dict[str, str]:
        """Get all settings as a dictionary."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM settings")
            return {row["key"]: row["value"] for row in cursor.fetchall()}
    
    def delete_setting(self, key: str) -> None:
        """Delete a setting from database."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM settings WHERE key = ?", (key,))
    
    # ========================
    # State Methods
    # ========================
    
    def get_last_sender_id(self) -> Optional[int]:
        """Get the last incoming message sender ID."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM state WHERE key = 'last_sender_id'")
            row = cursor.fetchone()
            return int(row[0]) if row else None
    
    def set_last_sender_id(self, sender_id: int) -> None:
        """Set the last incoming message sender ID."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO state (key, value) VALUES ('last_sender_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (str(sender_id), str(sender_id)))
    
    def get_last_activity_ts(self) -> Optional[int]:
        """Get timestamp of last activity."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM state WHERE key = 'last_activity_ts'")
            row = cursor.fetchone()
            return int(row[0]) if row else None
    
    def set_last_activity_ts(self, ts: Optional[int] = None) -> None:
        """Set timestamp of last activity."""
        if ts is None:
            ts = int(time.time())
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO state (key, value) VALUES ('last_activity_ts', ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (str(ts), str(ts)))
