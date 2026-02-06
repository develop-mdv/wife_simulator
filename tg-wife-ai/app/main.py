"""
Main module for TG Wife AI.
Handles Telegram client, message processing, and Gemini integration.
"""

import asyncio
import logging
import sys
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
    
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
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
        
        # Resolved target user ID (set during startup)
        self.target_user_id: Optional[int] = config.target_user_id
        
        # Queue processor task
        self._queue_processor_task: Optional[asyncio.Task] = None
    
    async def resolve_target_user(self) -> None:
        """Resolve target user from username if needed."""
        if self.target_user_id:
            logger.info(f"✓ Using target user ID: {self.target_user_id}")
            return
        
        if self.config.target_username:
            logger.info(f"Resolving username: @{self.config.target_username}")
            try:
                entity = await self.client.get_entity(self.config.target_username)
                if isinstance(entity, User):
                    self.target_user_id = entity.id
                    logger.info(f"✓ Resolved @{self.config.target_username} to user ID: {self.target_user_id}")
                else:
                    logger.error(f"❌ @{self.config.target_username} is not a user")
                    sys.exit(1)
            except Exception as e:
                logger.error(f"❌ Failed to resolve @{self.config.target_username}: {e}")
                sys.exit(1)
    
    def is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        if not self.config.quiet_hours_start or not self.config.quiet_hours_end:
            return False
        
        try:
            now = datetime.now(self.config.timezone)
            current_time = now.time()
            
            start_parts = self.config.quiet_hours_start.split(":")
            end_parts = self.config.quiet_hours_end.split(":")
            
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
        context = self.db.get_context(self.config.context_turns)
        
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
        
        # Filter: ignore outgoing messages
        if event.out:
            return
        
        # Get sender
        sender = await event.get_sender()
        if not isinstance(sender, User):
            return
        
        # Filter: only target user
        if sender.id != self.target_user_id:
            return
        
        # Filter: ignore empty messages (service messages, etc.)
        message_text = event.raw_text.strip() if event.raw_text else ""
        if not message_text:
            return
        
        # Deduplication check
        if self.db.is_message_processed(event.id):
            logger.debug(f"Message {event.id} already processed, skipping")
            return
        
        logger.info(f"📨 Received: {message_text[:50]}{'...' if len(message_text) > 50 else ''}")
        
        # Check quiet hours
        if self.is_quiet_hours():
            if self.config.quiet_mode == "ignore":
                logger.info("🌙 Quiet hours (ignore mode) - skipping message")
                return
            else:  # queue mode
                logger.info("🌙 Quiet hours (queue mode) - saving to pending")
                self.db.add_pending_message(event.id, message_text)
                return
        
        # Rate limit check
        if not await self.rate_limiter.acquire():
            wait_time = self.rate_limiter.time_until_available()
            logger.warning(f"⏱️ Rate limited, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            await self.rate_limiter.wait_and_acquire()
        
        # Save incoming message
        self.db.add_message("user", message_text, event.id)
        
        # Generate and send response
        try:
            response = await self.generate_response(message_text)
            logger.info(f"💬 Response: {response[:50]}{'...' if len(response) > 50 else ''}")
            
            await self.send_message_with_typing(event.chat_id, response)
            
            # Save outgoing message
            self.db.add_message("assistant", response)
            
        except Exception as e:
            logger.error(f"❌ Error processing message: {e}")
    
    async def process_pending_queue(self) -> None:
        """Process pending messages after quiet hours end."""
        if not self.db.has_pending_messages():
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
        
        # Save all pending messages to history
        for msg in pending:
            self.db.add_message("user", msg["text"], msg["message_id"])
        
        # Format combined message
        combined_text = format_pending_messages(pending)
        
        try:
            response = await self.generate_response(combined_text)
            logger.info(f"💬 Queue response: {response[:50]}{'...' if len(response) > 50 else ''}")
            
            await self.send_message_with_typing(self.target_user_id, response)
            
            # Save outgoing message
            self.db.add_message("assistant", response)
            
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
        if self.config.quiet_mode == "queue":
            self._queue_processor_task = asyncio.create_task(self.queue_processor_loop())
            logger.info("✓ Queue processor started")
        
        # Log configuration
        logger.info(f"📋 Config: model={self.config.model_name}, context_turns={self.config.context_turns}")
        if self.config.quiet_hours_start and self.config.quiet_hours_end:
            logger.info(f"🌙 Quiet hours: {self.config.quiet_hours_start} - {self.config.quiet_hours_end} ({self.config.quiet_mode} mode)")
        logger.info(f"⏱️ Rate limit: {self.config.rate_limit_count} messages per {self.config.rate_limit_window_sec}s")
        
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
    bot = WifeBot(config)
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
