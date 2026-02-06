"""
Telethon client manager for multi-user support.
Manages multiple Telethon clients, one per user.
"""

import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import Optional, Callable, Awaitable
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import User
import google.generativeai as genai

from .user_data import UserData, UserState
from .db import Database

logger = logging.getLogger(__name__)


class TelethonManager:
    """Manages multiple Telethon clients for multi-user support."""
    
    def __init__(self, db: Database, gemini_api_key: str):
        self.db = db
        self.gemini_api_key = gemini_api_key
        
        # Active clients: user_id -> TelegramClient
        self._clients: dict[int, TelegramClient] = {}
        
        # Queue processor tasks
        self._queue_tasks: dict[int, asyncio.Task] = {}
        
        # Configure Gemini (global)
        genai.configure(api_key=gemini_api_key)
    
    async def start_client_for_user(self, user: UserData) -> bool:
        """Start Telethon client for a user. Returns True on success."""
        if user.user_id in self._clients:
            logger.warning(f"Client already running for user {user.user_id}")
            return True
        
        if not user.session_string or not user.api_id or not user.api_hash:
            logger.error(f"User {user.user_id} missing credentials")
            return False
        
        try:
            client = TelegramClient(
                StringSession(user.session_string),
                user.api_id,
                user.api_hash,
            )
            
            await client.connect()
            
            if not await client.is_user_authorized():
                logger.error(f"User {user.user_id} session invalid")
                return False
            
            # Register message handler
            @client.on(events.NewMessage)
            async def handler(event):
                await self._handle_message(user.user_id, event)
            
            self._clients[user.user_id] = client
            
            # Start queue processor
            self._queue_tasks[user.user_id] = asyncio.create_task(
                self._queue_processor_loop(user.user_id)
            )
            
            logger.info(f"✓ Started client for user {user.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start client for user {user.user_id}: {e}")
            return False
    
    async def stop_client_for_user(self, user_id: int) -> None:
        """Stop Telethon client for a user."""
        if user_id in self._queue_tasks:
            self._queue_tasks[user_id].cancel()
            try:
                await self._queue_tasks[user_id]
            except asyncio.CancelledError:
                pass
            del self._queue_tasks[user_id]
        
        if user_id in self._clients:
            await self._clients[user_id].disconnect()
            del self._clients[user_id]
            logger.info(f"Stopped client for user {user_id}")
    
    async def stop_all_clients(self) -> None:
        """Stop all running clients."""
        user_ids = list(self._clients.keys())
        for user_id in user_ids:
            await self.stop_client_for_user(user_id)
    
    def get_client(self, user_id: int) -> Optional[TelegramClient]:
        """Get client for user if running."""
        return self._clients.get(user_id)
    
    async def start_all_configured_users(self) -> int:
        """Start clients for all configured users. Returns count started."""
        users = self.db.get_all_configured_users()
        started = 0
        for user in users:
            if await self.start_client_for_user(user):
                started += 1
        return started
    
    # ========================
    # Auth Flow (for onboarding)
    # ========================
    
    async def send_code(self, user: UserData) -> tuple[bool, str, Optional[str], Optional[str]]:
        """
        Send authentication code.
        Returns: (success, message, phone_code_hash, session_string)
        """
        try:
            client = TelegramClient(
                StringSession(),
                user.api_id,
                user.api_hash,
            )
            await client.connect()
            
            result = await client.send_code_request(user.phone)
            
            # Return session for later use (don't disconnect yet, but save session)
            session_string = client.session.save()
            
            await client.disconnect()
            
            return True, "Код отправлен", result.phone_code_hash, session_string
            
        except Exception as e:
            logger.error(f"Send code error: {e}")
            return False, f"Ошибка: {e}", None, None
    
    async def sign_in(self, user: UserData, code: str, phone_code_hash: str, session_string: str) -> tuple[bool, str, bool, Optional[str]]:
        """
        Sign in with code.
        Returns: (success, message, needs_2fa, new_session_string)
        """
        try:
            client = TelegramClient(
                StringSession(session_string),
                user.api_id,
                user.api_hash,
            )
            await client.connect()
            
            try:
                await client.sign_in(
                    user.phone,
                    code,
                    phone_code_hash=phone_code_hash
                )
                
                # Save updated session
                new_session = client.session.save()
                user.session_string = new_session
                self.db.save_user(user)
                
                await client.disconnect()
                return True, "Авторизация успешна!", False, new_session
                
            except SessionPasswordNeededError:
                # 2FA required - return session for next step
                new_session = client.session.save()
                await client.disconnect()
                return False, "Требуется 2FA пароль", True, new_session
                
        except Exception as e:
            logger.error(f"Sign in error: {e}")
            return False, f"Ошибка: {e}", False, None
    
    async def sign_in_2fa(self, user: UserData, password: str, session_string: str) -> tuple[bool, str]:
        """Sign in with 2FA password."""
        try:
            client = TelegramClient(
                StringSession(session_string),
                user.api_id,
                user.api_hash,
            )
            await client.connect()
            
            await client.sign_in(password=password)
            
            user.session_string = client.session.save()
            self.db.save_user(user)
            
            await client.disconnect()
            return True, "Авторизация успешна!"
            
        except Exception as e:
            logger.error(f"2FA sign in error: {e}")
            return False, f"Ошибка: {e}"
    
    async def resolve_username(self, user: UserData, username: str) -> tuple[bool, Optional[int], str]:
        """
        Resolve username to user ID.
        Returns: (success, user_id, display_name)
        """
        try:
            client = TelegramClient(
                StringSession(user.session_string),
                user.api_id,
                user.api_hash,
            )
            await client.connect()
            
            entity = await client.get_entity(username)
            
            if isinstance(entity, User):
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                await client.disconnect()
                return True, entity.id, name or username
            else:
                await client.disconnect()
                return False, None, "Это не пользователь"
                
        except Exception as e:
            logger.error(f"Resolve username error: {e}")
            return False, None, f"Ошибка: {e}"
    
    # ========================
    # Message Handling
    # ========================
    
    async def _handle_message(self, owner_user_id: int, event: events.NewMessage.Event) -> None:
        """Handle incoming message for a user."""
        if not event.is_private:
            return
        
        # Get fresh user data
        user = self.db.get_user(owner_user_id)
        if not user or not user.is_configured():
            return
        
        sender = await event.get_sender()
        if not isinstance(sender, User):
            return
        
        chat_id = event.chat_id
        message_id = event.id
        
        # Handle outgoing messages - manual override pause
        if event.out:
            if sender.id == user.target_user_id or chat_id == user.target_user_id:
                # User typing manually - auto pause 15 min
                user.pause_until_ts = int(asyncio.get_event_loop().time()) + 15 * 60
                self.db.save_user(user)
                logger.info(f"User {owner_user_id}: manual override, paused 15 min")
            return
        
        # Only respond to target user
        if sender.id != user.target_user_id:
            return
        
        message_text = event.raw_text.strip() if event.raw_text else ""
        if not message_text:
            return
        
        # Dedup
        if self.db.is_message_processed(owner_user_id, chat_id, message_id):
            return
        
        logger.info(f"User {owner_user_id} 📨: {message_text[:50]}...")
        
        # Update last activity
        user.last_activity_ts = int(asyncio.get_event_loop().time())
        self.db.save_user(user)
        
        # Check if should respond
        should, reason = user.should_respond()
        if not should:
            logger.info(f"User {owner_user_id}: skipping ({reason})")
            return
        
        # Check quiet hours
        if self._is_quiet_hours(user):
            if user.quiet_mode == "ignore":
                logger.info(f"User {owner_user_id}: quiet hours (ignore)")
                return
            else:
                logger.info(f"User {owner_user_id}: quiet hours (queue)")
                self.db.add_pending_message(owner_user_id, chat_id, message_id, message_text)
                return
        
        # Save message
        self.db.add_message(owner_user_id, "user", message_text, chat_id, message_id)
        
        # Generate and send response
        try:
            response = await self._generate_response(user, message_text)
            logger.info(f"User {owner_user_id} 💬: {response[:50]}...")
            
            client = self._clients.get(owner_user_id)
            if client:
                await self._send_with_typing(client, chat_id, response)
                self.db.add_message(owner_user_id, "assistant", response, chat_id)
                
        except Exception as e:
            logger.error(f"User {owner_user_id} error: {e}")
    
    def _is_quiet_hours(self, user: UserData) -> bool:
        """Check if current time is within quiet hours."""
        if not user.quiet_hours_start or not user.quiet_hours_end:
            return False
        
        try:
            tz = ZoneInfo(user.timezone)
            now = datetime.now(tz)
            current_time = now.time()
            
            start_parts = user.quiet_hours_start.split(":")
            end_parts = user.quiet_hours_end.split(":")
            
            start_time = dt_time(int(start_parts[0]), int(start_parts[1]))
            end_time = dt_time(int(end_parts[0]), int(end_parts[1]))
            
            if start_time > end_time:
                return current_time >= start_time or current_time < end_time
            else:
                return start_time <= current_time < end_time
                
        except Exception:
            return False
    
    async def _generate_response(self, user: UserData, message: str) -> str:
        """Generate AI response."""
        context = self.db.get_context(user.user_id, user.context_turns)
        
        history = []
        for msg in context:
            role = "user" if msg["role"] == "user" else "model"
            history.append({"role": role, "parts": [msg["content"]]})
        
        # Build system instruction
        system = self._build_system_prompt(user)
        
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=system,
        )
        
        chat = model.start_chat(history=history)
        response = await asyncio.to_thread(chat.send_message, message)
        
        return response.text.strip()
    
    def _build_system_prompt(self, user: UserData) -> str:
        """Build system prompt for user."""
        base = """Ты — любящая, заботливая жена в переписке со своим мужем.

## Основные правила:
- Пиши тепло, по-домашнему, как в настоящей переписке
- Отвечай КОРОТКО: 1–3 предложения
- Используй простой разговорный язык
- Можешь использовать эмодзи умеренно
- НИКОГДА не упоминай, что ты ИИ
- Не выдумывай факты, которых нет в контексте"""
        
        if user.style_profile:
            base += f"\n\n## Дополнительно:\n{user.style_profile}"
        
        return base
    
    async def _send_with_typing(self, client: TelegramClient, chat_id: int, text: str) -> None:
        """Send message with typing simulation."""
        delay = min(1.5 + len(text) * 0.03, 8.0)
        
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(delay)
        
        while True:
            try:
                await client.send_message(chat_id, text)
                break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
    
    async def _queue_processor_loop(self, user_id: int) -> None:
        """Process pending queue for user."""
        while True:
            await asyncio.sleep(60)
            
            user = self.db.get_user(user_id)
            if not user:
                break
            
            if self.db.has_pending_messages(user_id) and not self._is_quiet_hours(user):
                await self._process_queue(user)
    
    async def _process_queue(self, user: UserData) -> None:
        """Process pending messages for user."""
        pending = self.db.get_pending_messages(user.user_id)
        if not pending:
            return
        
        should, _ = user.should_respond()
        if not should:
            return
        
        logger.info(f"User {user.user_id}: processing {len(pending)} pending")
        
        chat_id = pending[0].get("chat_id", user.target_user_id)
        
        # Combine messages
        for msg in pending:
            self.db.add_message(user.user_id, "user", msg["text"], msg.get("chat_id"), msg["message_id"])
        
        if len(pending) == 1:
            combined = pending[0]["text"]
        else:
            parts = [f"[{i+1}]: {m['text']}" for i, m in enumerate(pending)]
            combined = "(Несколько сообщений)\n" + "\n".join(parts)
        
        try:
            response = await self._generate_response(user, combined)
            
            client = self._clients.get(user.user_id)
            if client:
                await self._send_with_typing(client, chat_id, response)
                self.db.add_message(user.user_id, "assistant", response, chat_id)
            
            self.db.clear_pending_messages(user.user_id)
            
        except Exception as e:
            logger.error(f"Queue processing error for {user.user_id}: {e}")
