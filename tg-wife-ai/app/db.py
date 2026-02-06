"""
Database module for storing message history and pending messages.
Uses SQLite for persistent storage.
"""

import sqlite3
from datetime import datetime
from typing import Optional
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
            
            # Message history table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    message_id INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Pending incoming messages (for quiet hours queue mode)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_incoming (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # State table for tracking last processed message
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
    
    def add_message(self, role: str, text: str, message_id: Optional[int] = None) -> None:
        """Add a message to history."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (role, text, message_id) VALUES (?, ?, ?)",
                (role, text, message_id)
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
    
    def is_message_processed(self, message_id: int) -> bool:
        """Check if a message has already been processed (deduplication)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = ? AND role = 'user' LIMIT 1",
                (message_id,)
            )
            return cursor.fetchone() is not None
    
    def add_pending_message(self, message_id: int, text: str) -> None:
        """Add a message to pending queue (quiet hours)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO pending_incoming (message_id, text) VALUES (?, ?)",
                    (message_id, text)
                )
            except sqlite3.IntegrityError:
                # Already exists, ignore
                pass
    
    def get_pending_messages(self) -> list[dict]:
        """Get all pending messages (oldest first)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT message_id, text, timestamp 
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
