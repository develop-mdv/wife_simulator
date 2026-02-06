"""
Admin Bot for runtime configuration management.
Uses python-telegram-bot v21+ in async mode (NOT run_polling!).
"""

import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .settings_manager import SettingsManager
from .db import Database

logger = logging.getLogger(__name__)

# Regex patterns for validation
TIME_PATTERN = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
DURATION_PATTERN = re.compile(r"^(\d+)(m|h)$", re.IGNORECASE)
UNTIL_PATTERN = re.compile(r"^until\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


class AdminBot:
    """Admin bot for managing tg-wife-ai settings via Telegram Bot API."""
    
    def __init__(
        self,
        token: str,
        admin_user_ids: list[int],
        settings: SettingsManager,
        db: Database,
    ):
        self.token = token
        self.admin_user_ids = admin_user_ids
        self.settings = settings
        self.db = db
        
        # State for multi-message input (e.g., style_profile)
        self._awaiting_input: dict[int, str] = {}  # user_id -> setting_key
        self._awaiting_timeout: dict[int, float] = {}  # user_id -> timeout_ts
        
        # Build application
        self.app = Application.builder().token(token).build()
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """Register all command and callback handlers."""
        # Commands
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("on", self._cmd_on))
        self.app.add_handler(CommandHandler("off", self._cmd_off))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("set", self._cmd_set))
        self.app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        self.app.add_handler(CommandHandler("last_sender", self._cmd_last_sender))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        
        # Inline keyboard callbacks
        self.app.add_handler(CallbackQueryHandler(self._callback_handler))
        
        # Text message handler for multi-message input
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._text_handler
        ))
    
    async def _check_access(self, update: Update) -> bool:
        """Check if user has admin access. Returns True if allowed."""
        user = update.effective_user
        if not user:
            return False
        
        if user.id not in self.admin_user_ids:
            # Log unauthorized access attempt
            logger.warning(
                f"⛔ Unauthorized access attempt: user_id={user.id}, "
                f"username=@{user.username or 'N/A'}, "
                f"name={user.full_name}"
            )
            await update.effective_message.reply_text("Нет доступа.")
            return False
        
        return True
    
    def _build_main_menu(self) -> InlineKeyboardMarkup:
        """Build main inline keyboard menu."""
        ai_enabled = self.settings.is_ai_enabled()
        ai_btn_text = "🤖 AI: ON ✅" if ai_enabled else "🤖 AI: OFF ❌"
        ai_btn_action = "toggle_off" if ai_enabled else "toggle_on"
        
        keyboard = [
            [
                InlineKeyboardButton(ai_btn_text, callback_data=ai_btn_action),
                InlineKeyboardButton("⏸ Пауза 30м", callback_data="pause_30m"),
            ],
            [
                InlineKeyboardButton("⏸ Пауза 2ч", callback_data="pause_2h"),
                InlineKeyboardButton("⏸ Пауза 12ч", callback_data="pause_12h"),
            ],
            [InlineKeyboardButton("▶️ Снять паузу", callback_data="resume")],
            [InlineKeyboardButton("🌙 Тихие часы", callback_data="show_quiet")],
            [InlineKeyboardButton("🎯 Target User", callback_data="show_target")],
            [InlineKeyboardButton("🌍 Timezone", callback_data="show_timezone")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")],
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def _format_status(self) -> str:
        """Format current status as text."""
        ai_enabled = self.settings.is_ai_enabled()
        is_paused = self.settings.is_paused()
        pause_remaining = self.settings.get_pause_remaining_seconds()
        
        target_id = self.settings.get_int("target_user_id", 0)
        target_username = self.settings.get_str("target_username", "")
        timezone = self.settings.get_str("timezone", "Europe/Moscow")
        quiet_start = self.settings.get_str("quiet_hours_start", "—")
        quiet_end = self.settings.get_str("quiet_hours_end", "—")
        quiet_mode = self.settings.get_str("quiet_mode", "queue")
        
        # Current time in configured timezone
        try:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            current_time = now.strftime("%H:%M")
        except Exception:
            current_time = "N/A"
        
        lines = [
            "📊 **Статус TG Wife AI**\n",
            f"🤖 AI: {'ON ✅' if ai_enabled else 'OFF ❌'}",
        ]
        
        if is_paused:
            minutes = pause_remaining // 60
            lines.append(f"⏸ Пауза: {minutes} мин. осталось")
        
        lines.extend([
            f"\n🎯 Target: {target_id or target_username or '—'}",
            f"🌍 Timezone: {timezone}",
            f"🕐 Сейчас: {current_time}",
            f"\n🌙 Тихие часы: {quiet_start or '—'} – {quiet_end or '—'}",
            f"📋 Режим: {quiet_mode}",
        ])
        
        # Last activity
        last_activity = self.db.get_last_activity_ts()
        if last_activity:
            try:
                tz = ZoneInfo(timezone)
                dt = datetime.fromtimestamp(last_activity, tz)
                lines.append(f"\n⏱ Последняя активность: {dt.strftime('%H:%M:%S')}")
            except Exception:
                pass
        
        return "\n".join(lines)
    
    # ========================
    # Command Handlers
    # ========================
    
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not await self._check_access(update):
            return
        
        text = self._format_status()
        await update.message.reply_text(
            text,
            reply_markup=self._build_main_menu(),
            parse_mode="Markdown"
        )
    
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        if not await self._check_access(update):
            return
        
        text = self._format_status()
        await update.message.reply_text(text, parse_mode="Markdown")
    
    async def _cmd_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /on command - enable AI."""
        if not await self._check_access(update):
            return
        
        self.settings.set("ai_enabled", "true")
        await update.message.reply_text("✅ Готово: AI=ON")
    
    async def _cmd_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /off command - disable AI."""
        if not await self._check_access(update):
            return
        
        self.settings.set("ai_enabled", "false")
        await update.message.reply_text("✅ Готово: AI=OFF")
    
    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pause command."""
        if not await self._check_access(update):
            return
        
        if not context.args:
            await update.message.reply_text(
                "Использование:\n"
                "/pause 30m — пауза на 30 минут\n"
                "/pause 2h — пауза на 2 часа\n"
                "/pause until 23:00 — пауза до 23:00"
            )
            return
        
        arg = " ".join(context.args)
        duration_seconds = self._parse_duration(arg)
        
        if duration_seconds is None:
            await update.message.reply_text(
                "❌ Неверный формат.\n"
                "Примеры: 30m, 2h, until 23:00"
            )
            return
        
        pause_until = self.settings.set_pause(duration_seconds)
        
        # Format end time
        try:
            tz = self.settings.get_timezone()
            end_dt = datetime.fromtimestamp(pause_until, tz)
            end_time = end_dt.strftime("%H:%M")
        except Exception:
            end_time = "N/A"
        
        await update.message.reply_text(f"✅ Готово: автоответы приостановлены до {end_time}")
    
    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /resume command - clear pause."""
        if not await self._check_access(update):
            return
        
        self.settings.clear_pause()
        await update.message.reply_text("✅ Готово: пауза снята")
    
    async def _cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /set command."""
        if not await self._check_access(update):
            return
        
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "Использование: /set <key> <value>\n\n"
                "Доступные ключи:\n"
                "• target_id — ID целевого пользователя\n"
                "• target_username — username без @\n"
                "• quiet_start — начало тихих часов (HH:MM)\n"
                "• quiet_end — конец тихих часов (HH:MM)\n"
                "• timezone — часовой пояс (Europe/Moscow)\n"
                "• quiet_mode — ignore или queue\n"
                "• context_turns — кол-во сообщений в контексте\n"
                "• rate_limit_count — макс. ответов\n"
                "• rate_limit_window — период (сек)\n"
                "• model — модель Gemini\n"
                "• style_profile — (введите без значения)"
            )
            return
        
        key = context.args[0].lower()
        value = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        
        # Key mapping
        key_map = {
            "target_id": "target_user_id",
            "target_username": "target_username",
            "quiet_start": "quiet_hours_start",
            "quiet_end": "quiet_hours_end",
            "timezone": "timezone",
            "quiet_mode": "quiet_mode",
            "context_turns": "context_turns",
            "rate_limit_count": "rate_limit_count",
            "rate_limit_window": "rate_limit_window",
            "model": "model_name",
            "style_profile": "style_profile",
        }
        
        setting_key = key_map.get(key)
        if not setting_key:
            await update.message.reply_text(f"❌ Неизвестный ключ: {key}")
            return
        
        # Special handling for style_profile (multi-line input)
        if setting_key == "style_profile" and not value:
            self._awaiting_input[update.effective_user.id] = setting_key
            self._awaiting_timeout[update.effective_user.id] = time.time() + 60
            await update.message.reply_text(
                "Отправьте следующим сообщением текст style_profile.\n"
                "(60 секунд на ввод)"
            )
            return
        
        # Validate and set
        error = self._validate_setting(setting_key, value)
        if error:
            await update.message.reply_text(f"❌ {error}")
            return
        
        self.settings.set(setting_key, value)
        await update.message.reply_text(f"✅ Готово: {setting_key}={value[:50]}{'...' if len(value) > 50 else ''}")
    
    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /whoami command - show user's ID."""
        if not await self._check_access(update):
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👤 Ваш Telegram ID: `{user.id}`\n"
            f"Username: @{user.username or 'N/A'}",
            parse_mode="Markdown"
        )
    
    async def _cmd_last_sender(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /last_sender command."""
        if not await self._check_access(update):
            return
        
        sender_id = self.db.get_last_sender_id()
        if sender_id:
            await update.message.reply_text(f"📨 Последний отправитель: `{sender_id}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("📨 Пока нет входящих сообщений")
    
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not await self._check_access(update):
            return
        
        help_text = """
📖 **Команды админ-бота**

/start — меню и статус
/status — текущий статус
/on — включить AI
/off — выключить AI
/pause <время> — пауза (30m, 2h, until 23:00)
/resume — снять паузу
/set <key> <value> — изменить настройку
/whoami — ваш Telegram ID
/last_sender — ID последнего отправителя
/help — эта справка

**Примеры /set:**
/set target_id 123456789
/set timezone Europe/Amsterdam
/set quiet_start 23:00
/set quiet_end 08:00
/set quiet_mode queue
/set style_profile (затем ввести текст)
        """
        await update.message.reply_text(help_text.strip(), parse_mode="Markdown")
    
    # ========================
    # Callback Handler
    # ========================
    
    async def _callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard callbacks."""
        query = update.callback_query
        if not query:
            return
        
        # Check access
        user = query.from_user
        if user.id not in self.admin_user_ids:
            logger.warning(f"⛔ Unauthorized callback: user_id={user.id}")
            await query.answer("Нет доступа", show_alert=True)
            return
        
        await query.answer()
        data = query.data
        
        if data == "toggle_on":
            self.settings.set("ai_enabled", "true")
            await query.edit_message_text(
                self._format_status(),
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        elif data == "toggle_off":
            self.settings.set("ai_enabled", "false")
            await query.edit_message_text(
                self._format_status(),
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        elif data.startswith("pause_"):
            duration_map = {
                "pause_30m": 30 * 60,
                "pause_2h": 2 * 60 * 60,
                "pause_12h": 12 * 60 * 60,
            }
            seconds = duration_map.get(data, 30 * 60)
            self.settings.set_pause(seconds)
            await query.edit_message_text(
                self._format_status(),
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        elif data == "resume":
            self.settings.clear_pause()
            await query.edit_message_text(
                self._format_status(),
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        elif data == "show_quiet":
            quiet_start = self.settings.get_str("quiet_hours_start", "—")
            quiet_end = self.settings.get_str("quiet_hours_end", "—")
            quiet_mode = self.settings.get_str("quiet_mode", "queue")
            await query.message.reply_text(
                f"🌙 **Тихие часы**\n\n"
                f"Начало: {quiet_start or '—'}\n"
                f"Конец: {quiet_end or '—'}\n"
                f"Режим: {quiet_mode}\n\n"
                f"Изменить:\n"
                f"/set quiet_start HH:MM\n"
                f"/set quiet_end HH:MM\n"
                f"/set quiet_mode ignore|queue",
                parse_mode="Markdown"
            )
        
        elif data == "show_target":
            target_id = self.settings.get_int("target_user_id", 0)
            target_username = self.settings.get_str("target_username", "")
            await query.message.reply_text(
                f"🎯 **Target User**\n\n"
                f"ID: {target_id or '—'}\n"
                f"Username: {target_username or '—'}\n\n"
                f"Изменить:\n"
                f"/set target_id 123456789\n"
                f"/set target_username username",
                parse_mode="Markdown"
            )
        
        elif data == "show_timezone":
            timezone = self.settings.get_str("timezone", "Europe/Moscow")
            try:
                tz = ZoneInfo(timezone)
                now = datetime.now(tz)
                current_time = now.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                current_time = "N/A"
            
            await query.message.reply_text(
                f"🌍 **Timezone**\n\n"
                f"Текущий: {timezone}\n"
                f"Время: {current_time}\n\n"
                f"Изменить:\n"
                f"/set timezone Europe/Amsterdam",
                parse_mode="Markdown"
            )
        
        elif data == "refresh":
            await query.edit_message_text(
                self._format_status(),
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
    
    # ========================
    # Text Handler (for multi-message input)
    # ========================
    
    async def _text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text messages for multi-message input."""
        if not await self._check_access(update):
            return
        
        user_id = update.effective_user.id
        
        # Check if awaiting input
        if user_id not in self._awaiting_input:
            return
        
        # Check timeout
        if time.time() > self._awaiting_timeout.get(user_id, 0):
            del self._awaiting_input[user_id]
            if user_id in self._awaiting_timeout:
                del self._awaiting_timeout[user_id]
            await update.message.reply_text("⏰ Время истекло. Попробуйте снова.")
            return
        
        setting_key = self._awaiting_input[user_id]
        value = update.message.text.strip()
        
        # Clean up
        del self._awaiting_input[user_id]
        if user_id in self._awaiting_timeout:
            del self._awaiting_timeout[user_id]
        
        # Validate and set
        error = self._validate_setting(setting_key, value)
        if error:
            await update.message.reply_text(f"❌ {error}")
            return
        
        self.settings.set(setting_key, value)
        await update.message.reply_text(f"✅ Готово: {setting_key} обновлён")
    
    # ========================
    # Helpers
    # ========================
    
    def _parse_duration(self, arg: str) -> Optional[int]:
        """Parse duration string to seconds."""
        arg = arg.strip()
        
        # Try "30m", "2h" format
        match = DURATION_PATTERN.match(arg)
        if match:
            amount = int(match.group(1))
            unit = match.group(2).lower()
            if unit == "m":
                return amount * 60
            elif unit == "h":
                return amount * 3600
        
        # Try "until 23:00" format
        match = UNTIL_PATTERN.match(arg)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            
            tz = self.settings.get_timezone()
            now = datetime.now(tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If target is in the past, assume tomorrow
            if target <= now:
                target += timedelta(days=1)
            
            return int((target - now).total_seconds())
        
        return None
    
    def _validate_setting(self, key: str, value: str) -> Optional[str]:
        """Validate setting value. Returns error message or None if valid."""
        if key in ("quiet_hours_start", "quiet_hours_end"):
            if value and not TIME_PATTERN.match(value):
                return f"Неверный формат времени. Используйте HH:MM (например, 23:00)"
        
        elif key == "timezone":
            try:
                ZoneInfo(value)
            except Exception:
                return f"Неверный часовой пояс: {value}. Используйте IANA формат (Europe/Moscow)"
        
        elif key == "quiet_mode":
            if value not in ("ignore", "queue"):
                return "quiet_mode должен быть 'ignore' или 'queue'"
        
        elif key == "target_user_id":
            if value:
                try:
                    uid = int(value)
                    if uid <= 0:
                        return "target_user_id должен быть положительным числом"
                except ValueError:
                    return "target_user_id должен быть числом"
        
        elif key == "context_turns":
            try:
                turns = int(value)
                if not (1 <= turns <= 100):
                    return "context_turns должен быть от 1 до 100"
            except ValueError:
                return "context_turns должен быть числом"
        
        elif key == "rate_limit_count":
            try:
                count = int(value)
                if not (1 <= count <= 20):
                    return "rate_limit_count должен быть от 1 до 20"
            except ValueError:
                return "rate_limit_count должен быть числом"
        
        elif key == "rate_limit_window":
            try:
                window = int(value)
                if not (10 <= window <= 300):
                    return "rate_limit_window должен быть от 10 до 300 секунд"
            except ValueError:
                return "rate_limit_window должен быть числом"
        
        return None


def create_admin_bot(
    token: Optional[str],
    admin_user_ids: list[int],
    settings: SettingsManager,
    db: Database,
) -> Optional[Application]:
    """
    Create admin bot Application if configured.
    
    Returns None if token is missing or no admin users configured.
    """
    if not token:
        logger.warning("⚠️ ADMIN_BOT_TOKEN не задан — админ-бот отключён")
        return None
    
    if not admin_user_ids:
        logger.warning("⚠️ ADMIN_USER_IDS пуст — админ-бот отключён")
        return None
    
    bot = AdminBot(token, admin_user_ids, settings, db)
    logger.info(f"✓ Админ-бот создан для {len(admin_user_ids)} админов")
    return bot.app
