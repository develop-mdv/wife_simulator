"""
Main module for TG Wife AI (Multi-user edition).
Runs the Admin Bot and manages Telethon clients for multiple users.
"""

import asyncio
import logging
import sys
import os

from .config import get_config
from .db import Database
from .telethon_manager import TelethonManager
from .admin_bot import create_admin_bot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Reduce noise from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.INFO)


async def main() -> None:
    """Main entry point."""
    logger.info("🚀 Starting TG Wife AI (Multi-User Mode)...")
    
    config = get_config()
    
    # Ensure data directory exists
    os.makedirs(config.data_dir, exist_ok=True)
    
    # Initialize components
    db = Database(config.db_path)
    tm = TelethonManager(db, config.gemini_api_key)
    
    # Start configured clients
    logger.info("Initializing user clients...")
    started_count = await tm.start_all_configured_users()
    logger.info(f"✓ Started {started_count} user client(s)")
    
    # Create and start Admin Bot
    admin_app = create_admin_bot(config.admin_bot_token, db, tm)
    
    logger.info("🔧 Starting Admin Bot...")
    await admin_app.initialize()
    await admin_app.start()
    
    # Start polling
    logger.info("✅ System is ready! Admin Bot is listening.")
    
    try:
        # Run polling for admin bot
        # Note: We use updater.start_polling() which creates a task, 
        # so we need to keep the main loop alive.
        await admin_app.updater.start_polling()
        
        # Keep alive forever
        while True:
            await asyncio.sleep(3600)
            
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Critical error: {e}")
    finally:
        logger.info("Shutting down...")
        await admin_app.updater.stop()
        await admin_app.stop()
        await admin_app.shutdown()
        await tm.stop_all_clients()
        logger.info("👋 Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
