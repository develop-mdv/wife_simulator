"""
Main module for TG Wife AI.
Handles Telegram client, message processing, Gemini integration, and admin bot.
"""

import asyncio
import logging
import sys
import time
from datetime import datetime, time as dt_time
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import User
import google.generativeai as genai

from .config import get_config, Config
from .db import Database
from .prompt import build_instructions, format_pending_messages
from .rate_limit import RateLimiter
from .settings_manager import SettingsManager
from .admin_bot import create_admin_bot


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class WifeBot:
    """Main bot class handling all message processing."""
    
    def __init__(self, config: Config, settings: SettingsManager, db: Database):
        self.config = config
        self.settings = settings
        self.db = db
        self.rate_limiter = RateLimiter(
            max_count=config.rate_limit_count,
            window_seconds=config.rate_limit_window_sec,
        )
        
        # Configure Gemini
        genai.configure(api_key=config.gemini_api_key)
        self.model = genai.GenerativeModel(
            model_name=config.model_name,
            system_instruction=build_instructions(),
        )
        
        # Telethon client
        self.client = TelegramClient(
            config.session_path,
            config.tg_api_id,
            config.tg_api_hash,
        )
        
        # Resolved target user ID (set during startup or from settings)
        self.target_user_id: Optional[int] = None
        
        # Queue processor task
        self._queue_processor_task: Optional[asyncio.Task] = None
    
    def _get_target_user_id(self) -> Optional[int]:
        """Get target user ID from settings (priority) or config."""
        # Settings has priority
        settings_id = self.settings.get_int("target_user_id", 0)
        if settings_id > 0:
            return settings_id
        return self.config.target_user_id
    
    def _get_target_username(self) -> Optional[str]:
        """Get target username from settings (priority) or config."""
        settings_username = self.settings.get_str("target_username", "")
        if settings_username:
            return settings_username
        return self.config.target_username
    
    async def resolve_target_user(self) -> None:
        """Resolve target user from username if needed."""
        self.target_user_id = self._get_target_user_id()
        
        if self.target_user_id:
            logger.info(f"✓ Using target user ID: {self.target_user_id}")
            return
        
        target_username = self._get_target_username()
        if target_username:
            logger.info(f"Resolving username: @{target_username}")
            try:
                entity = await self.client.get_entity(target_username)
                if isinstance(entity, User):
                    self.target_user_id = entity.id
                    # Save resolved ID to settings
                    self.settings.set("target_user_id", self.target_user_id)
                    logger.info(f"✓ Resolved @{target_username} to user ID: {self.target_user_id}")
                else:
                    logger.error(f"❌ @{target_username} is not a user")
                    sys.exit(1)
            except Exception as e:
                logger.error(f"❌ Failed to resolve @{target_username}: {e}")
                sys.exit(1)
    
    def is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        quiet_start = self.settings.get_str("quiet_hours_start", "")
        quiet_end = self.settings.get_str("quiet_hours_end", "")
        
        if not quiet_start or not quiet_end:
            return False
        
        try:
            tz = self.settings.get_timezone()
            now = datetime.now(tz)
            current_time = now.time()
            
            start_parts = quiet_start.split(":")
            end_parts = quiet_end.split(":")
            
            start_time = dt_time(int(start_parts[0]), int(start_parts[1]))
            end_time = dt_time(int(end_parts[0]), int(end_parts[1]))
            
            # Handle overnight quiet hours (e.g., 23:00 - 08:00)
            if start_time > end_time:
                return current_time >= start_time or current_time < end_time
            else:
                return start_time <= current_time < end_time
                
        except Exception as e:
            logger.warning(f"Error checking quiet hours: {e}")
            return False
    
    def calculate_typing_delay(self, text: str) -> float:
        """Calculate typing delay based on response length."""
        base_delay = 1.5
        per_char_delay = 0.03
        min_delay = 1.5
        max_delay = 8.0
        
        delay = base_delay + len(text) * per_char_delay
        return max(min_delay, min(max_delay, delay))
    
    async def generate_response(self, user_message: str) -> str:
        """Generate response using Gemini API."""
        # Get conversation context
        context_turns = self.settings.get_int("context_turns", 40)
        context = self.db.get_context(context_turns)
        
        # Build chat history for Gemini format
        history = []
        for msg in context:
            role = "user" if msg["role"] == "user" else "model"
            history.append({"role": role, "parts": [msg["content"]]})
        
        try:
            # Create chat with history
            chat = self.model.start_chat(history=history)
            
            # Send current message
            response = await asyncio.to_thread(
                chat.send_message, user_message
            )
            
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            raise
    
    async def send_message_with_typing(self, chat_id: int, text: str) -> None:
        """Send message with typing simulation."""
        delay = self.calculate_typing_delay(text)
        
        # Start typing action
        async with self.client.action(chat_id, "typing"):
            await asyncio.sleep(delay)
        
        # Send message with retry on flood
        while True:
            try:
                await self.client.send_message(chat_id, text)
                break
            except FloodWaitError as e:
                logger.warning(f"FloodWait: sleeping {e.seconds}s")
                await asyncio.sleep(e.seconds)
    
    async def handle_message(self, event: events.NewMessage.Event) -> None:
        """Handle incoming message from target user."""
        # Filter: only private chats
        if not event.is_private:
            return
        
        # Get sender
        sender = await event.get_sender()
        if not isinstance(sender, User):
            return
        
        chat_id = event.chat_id
        message_id = event.id
        
        # Track last sender for /last_sender command
        self.db.set_last_sender_id(sender.id)
        
        # Handle outgoing messages - manual override pause
        if event.out:
            # Check if this is a message to target user
            current_target = self._get_target_user_id()
            if sender.id == current_target or chat_id == current_target:
                # User is manually typing to target - set auto-pause
                self.settings.set_manual_override_pause()
            return
        
        # Filter: only target user
        current_target = self._get_target_user_id()
        if sender.id != current_target:
            return
        
        # Filter: ignore empty messages (service messages, etc.)
        message_text = event.raw_text.strip() if event.raw_text else ""
        if not message_text:
            return
        
        # Deduplication check (chat_id + message_id)
        if self.db.is_message_processed(chat_id, message_id):
            logger.debug(f"Message {message_id} already processed, skipping")
            return
        
        logger.info(f"📨 Received: {message_text[:50]}{'...' if len(message_text) > 50 else ''}")
        
        # Update last activity
        self.db.set_last_activity_ts()
        
        # Check if should respond (AI enabled, not paused)
        should_respond, reason = self.settings.should_respond()
        if not should_respond:
            logger.info(f"🚫 Skipping response: {reason}")
            return
        
        # Check quiet hours
        if self.is_quiet_hours():
            quiet_mode = self.settings.get_str("quiet_mode", "queue")
            if quiet_mode == "ignore":
                logger.info("🌙 Quiet hours (ignore mode) - skipping message")
                return
            else:  # queue mode
                logger.info("🌙 Quiet hours (queue mode) - saving to pending")
                self.db.add_pending_message(chat_id, message_id, message_text)
                return
        
        # Rate limit check
        if not await self.rate_limiter.acquire():
            wait_time = self.rate_limiter.time_until_available()
            logger.warning(f"⏱️ Rate limited, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            await self.rate_limiter.wait_and_acquire()
        
        # Save incoming message
        self.db.add_message("user", message_text, chat_id, message_id)
        
        # Generate and send response
        try:
            response = await self.generate_response(message_text)
            logger.info(f"💬 Response: {response[:50]}{'...' if len(response) > 50 else ''}")
            
            await self.send_message_with_typing(event.chat_id, response)
            
            # Save outgoing message
            self.db.add_message("assistant", response, chat_id)
            
        except Exception as e:
            logger.error(f"❌ Error processing message: {e}")
    
    async def process_pending_queue(self) -> None:
        """Process pending messages after quiet hours end."""
        if not self.db.has_pending_messages():
            return
        
        # Check if should respond
        should_respond, reason = self.settings.should_respond()
        if not should_respond:
            logger.info(f"🚫 Queue processing skipped: {reason}")
            return
        
        pending = self.db.get_pending_messages()
        count = len(pending)
        logger.info(f"📤 Processing {count} pending message(s) from quiet hours")
        
        # Rate limit check for queue flush
        if not await self.rate_limiter.acquire():
            wait_time = self.rate_limiter.time_until_available()
            logger.info(f"⏱️ Rate limited, waiting {wait_time:.1f}s before queue flush")
            await asyncio.sleep(wait_time)
            await self.rate_limiter.wait_and_acquire()
        
        # Get chat_id from first pending message
        chat_id = pending[0].get("chat_id", self._get_target_user_id())
        
        # Save all pending messages to history
        for msg in pending:
            self.db.add_message("user", msg["text"], msg.get("chat_id"), msg["message_id"])
        
        # Format combined message
        combined_text = format_pending_messages(pending)
        
        try:
            response = await self.generate_response(combined_text)
            logger.info(f"💬 Queue response: {response[:50]}{'...' if len(response) > 50 else ''}")
            
            await self.send_message_with_typing(chat_id, response)
            
            # Save outgoing message
            self.db.add_message("assistant", response, chat_id)
            
            # Clear pending queue
            self.db.clear_pending_messages()
            
        except Exception as e:
            logger.error(f"❌ Error processing pending queue: {e}")
    
    async def queue_processor_loop(self) -> None:
        """Background task to check and process pending queue."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            
            if self.db.has_pending_messages() and not self.is_quiet_hours():
                await self.process_pending_queue()
    
    async def start(self) -> None:
        """Start the bot."""
        logger.info("🚀 Starting TG Wife AI...")
        
        # Connect and resolve target user
        await self.client.start()
        logger.info("✓ Telegram client connected")
        
        await self.resolve_target_user()
        
        # Register message handler
        @self.client.on(events.NewMessage)
        async def message_handler(event):
            await self.handle_message(event)
        
        # Start queue processor if queue mode enabled
        quiet_mode = self.settings.get_str("quiet_mode", "queue")
        if quiet_mode == "queue":
            self._queue_processor_task = asyncio.create_task(self.queue_processor_loop())
            logger.info("✓ Queue processor started")
        
        # Log configuration
        model_name = self.settings.get_str("model_name", self.config.model_name)
        context_turns = self.settings.get_int("context_turns", 40)
        logger.info(f"📋 Config: model={model_name}, context_turns={context_turns}")
        
        quiet_start = self.settings.get_str("quiet_hours_start", "")
        quiet_end = self.settings.get_str("quiet_hours_end", "")
        if quiet_start and quiet_end:
            logger.info(f"🌙 Quiet hours: {quiet_start} - {quiet_end} ({quiet_mode} mode)")
        
        rate_count = self.settings.get_int("rate_limit_count", 4)
        rate_window = self.settings.get_int("rate_limit_window", 30)
        logger.info(f"⏱️ Rate limit: {rate_count} messages per {rate_window}s")
        
        ai_status = "ON" if self.settings.is_ai_enabled() else "OFF"
        logger.info(f"🤖 AI: {ai_status}")
        
        logger.info("✅ Bot is running! Listening for messages...")
        
        # Run until disconnected
        await self.client.run_until_disconnected()
    
    async def stop(self) -> None:
        """Stop the bot gracefully."""
        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass
        
        await self.client.disconnect()
        logger.info("👋 Bot stopped")


async def main() -> None:
    """Main entry point."""
    config = get_config()
    
    # Initialize database
    db = Database(config.db_path)
    
    # Initialize settings manager with ENV defaults
    settings = SettingsManager(db, config.to_settings_dict())
    
    # Create wife bot
    wife_bot = WifeBot(config, settings, db)
    
    # Create admin bot (returns Application or None)
    admin_app = create_admin_bot(
        token=config.admin_bot_token,
        admin_user_ids=config.admin_user_ids,
        settings=settings,
        db=db,
    )
    
    try:
        if admin_app:
            # Run both bots - PTB in async mode (NOT run_polling!)
            logger.info("🔧 Starting admin bot...")
            await admin_app.initialize()
            await admin_app.start()
            await admin_app.updater.start_polling()
            logger.info("✓ Admin bot started")
            
            try:
                # Wife bot blocks until disconnected
                await wife_bot.start()
            finally:
                # Cleanup admin bot
                logger.info("Stopping admin bot...")
                await admin_app.updater.stop()
                await admin_app.stop()
                await admin_app.shutdown()
        else:
            # Run only wife bot
            await wife_bot.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await wife_bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
