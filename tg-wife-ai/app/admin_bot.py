"""
Admin Bot with Multi-User Support and Onboarding Flow.
Uses python-telegram-bot v21+ with ConversationHandler.
"""

import os
import re
import time
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, KeyboardButtonRequestUsers, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from .user_data import UserData, UserState
from .db import Database
from .telethon_manager import TelethonManager

logger = logging.getLogger(__name__)

# Conversation states
(
    STATE_ONBOARDING_API_ID,
    STATE_ONBOARDING_API_HASH,
    STATE_ONBOARDING_PHONE,
    STATE_ONBOARDING_CODE,
    STATE_ONBOARDING_2FA,
    STATE_ONBOARDING_TARGET,
    STATE_MAIN_MENU,
    STATE_SETTINGS_INPUT,
) = range(8)


class AdminBot:
    """Multi-user Admin Bot."""
    
    def __init__(self, token: str, db: Database, telethon_manager: TelethonManager):
        self.token = token
        self.db = db
        self.tm = telethon_manager
        
        self.app = Application.builder().token(token).build()
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """Register all handlers."""
        
        # Onboarding Conversation
        onboarding_handler = ConversationHandler(
            entry_points=[CommandHandler("start", self._cmd_start)],
            states={
                STATE_ONBOARDING_API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_api_id)],
                STATE_ONBOARDING_API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_api_hash)],
                STATE_ONBOARDING_PHONE: [MessageHandler(filters.TEXT | filters.CONTACT, self._handle_phone)],
                STATE_ONBOARDING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_code)],
                STATE_ONBOARDING_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_2fa)],
                STATE_ONBOARDING_TARGET: [
                    MessageHandler(filters.StatusUpdate.USERS_SHARED, self._handle_user_shared),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_target)
                ],
                
                STATE_MAIN_MENU: [
                    CallbackQueryHandler(self._menu_callback),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._unknown_text)
                ],
                
                STATE_SETTINGS_INPUT: [
                    CallbackQueryHandler(self._settings_callback),
                    MessageHandler(filters.StatusUpdate.USERS_SHARED, self._handle_target_change_shared),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_setting_input)
                ],
            },
            fallbacks=[
                CommandHandler("start", self._cmd_start),
                CommandHandler("cancel", self._cmd_cancel),
            ],
            per_message=False
        )
        
        self.app.add_handler(onboarding_handler)
    
    # ========================
    # Helpers
    # ========================
    
    def _get_user(self, telegram_user) -> UserData:
        """Get or create user."""
        user = self.db.get_user(telegram_user.id)
        if not user:
            user = UserData(user_id=telegram_user.id)
            self.db.save_user(user)
        return user
    
    async def _send_main_menu(self, update: Update, user: UserData, edit: bool = False) -> None:
        """Send main menu."""
        status = "✅ AI ВКЛЮЧЕН" if user.ai_enabled else "❌ AI ВЫКЛЮЧЕН"
        if user.is_paused():
            minutes = int((user.pause_until_ts - time.time()) / 60)
            status = f"⏸ ПАУЗА ({minutes} мин)"
        
        text = (
            f"📊 **Панель управления**\n\n"
            f"Статус: **{status}**\n"
            f"Цель: {user.target_name or user.target_username or user.target_user_id}\n"
            f"Тихие часы: {user.quiet_hours_start or '—'} – {user.quiet_hours_end or '—'}\n"
            f"Timezone: {user.timezone}"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("🤖 Включить AI", callback_data="toggle_on") 
                if not user.ai_enabled else 
                InlineKeyboardButton("🛑 Выключить AI", callback_data="toggle_off")
            ],
            [
                InlineKeyboardButton("⏸ 15м", callback_data="pause_15m"),
                InlineKeyboardButton("⏸ 1ч", callback_data="pause_1h"),
                InlineKeyboardButton("▶️ Снять паузу", callback_data="resume")
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu"),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh")
            ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
            except Exception:
                await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
            
    # ========================
    # Entry Point & Onboarding
    # ========================
    
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start command - entry point."""
        user = self._get_user(update.effective_user)
        
        # If already configured, go to main menu
        if user.is_configured():
            await self._send_main_menu(update, user)
            return STATE_MAIN_MENU
        
        # Start onboarding
        text = (
            "👋 **Привет! Это TG Wife AI.**\n\n"
            "Я помогу настроить персонального AI-ассистента, который сможет отвечать "
            "на сообщения в Telegram вместо тебя (например, мужу/жене), пока ты занят(а).\n\n"
            "**Как это работает:**\n"
            "1. Мы подключим твой Telegram аккаунт (через официальный API)\n"
            "2. Ты выберешь человека, которому нужно отвечать\n"
            "3. Бот будет работать в фоновом режиме\n\n"
            "Давай начнём настройку! Это займёт 2 минуты."
        )
        
        keyboard = [[InlineKeyboardButton("🚀 Начать настройку", callback_data="start_setup")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
        return STATE_ONBOARDING_API_ID  # Use callback to transition effectively, but handler expects state
    
    # We actually need a callback handler for the "Start" button to trigger the next step properly
    # Handling this within states is tricky with mixed entry points. 
    # Let's simplify: /start checks state. If setup needed, ask for API ID immediately after welcome text.
    
    # Actually, let's make _cmd_start return the first state directly if we print the API prompt.
    
    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel current operation."""
        await update.message.reply_text("❌ Действие отменено. Напиши /start чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    async def _cancel_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel via inline button."""
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("❌ Настройка отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
        
    async def _settings_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle callbacks in settings input state."""
        query = update.callback_query
        await query.answer()
        data = query.data
        user = self._get_user(update.effective_user)
        
        if data == "back_to_settings":
            user.pending_setting = None
            self.db.save_user(user)
            await self._send_settings_menu(update, user, edit=True)
            return STATE_MAIN_MENU
        
        return STATE_SETTINGS_INPUT

    # ========================
    # Onboarding Steps
    # ========================

    async def _start_setup_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Callback from 'Start Setup' button (optional implementation details)."""
        # Included for completeness if we used callback transistion
        pass

    async def _handle_api_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle API_ID input (or start of flow)."""
        # Check if this is actually the /start message trigger
        # If user just typed /start, we sent welcome. Now we expect API ID.
        # But wait, user might not have seen the prompt yet if we didn't send it in /start.
        
        # Let's refine flow:
        # /start -> Welcome msg -> "Enter API ID"
        pass
        
    # Redefining _cmd_start to be smoother
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update.effective_user)
        if user.is_configured():
            await self._send_main_menu(update, user)
            return STATE_MAIN_MENU

        # Get assets path (relative to this file)
        assets_dir = Path(__file__).parent / "assets"
        
        # Send instruction images if they exist
        images = [
            ("tg_phone.png", "📱 Введи номер телефона"),
            ("Confirmation code.png", "🔐 Введи код из Telegram"),
            ("Your Telegram Core.png", "👉 Нажми API development tools"),
            ("api_id_api_hash.png", "📋 Скопируй api_id и api_hash"),
        ]
        
        media_group = []
        for filename, caption in images:
            img_path = assets_dir / filename
            if img_path.exists():
                media_group.append(InputMediaPhoto(media=open(img_path, 'rb'), caption=caption))
        
        if media_group:
            try:
                await update.message.reply_media_group(media_group)
            except Exception as e:
                logger.warning(f"Could not send instruction images: {e}")

        await update.message.reply_text(
            "👋 **Привет! Настроим твоего AI-ассистента.**\n\n"
            "**Шаг 1 из 4: Telegram API**\n"
            "Для работы нужны API ID и API Hash.\n\n"
            "📖 **Как получить** (см. картинки выше):\n"
            "1️⃣ Открой https://my.telegram.org\n"
            "2️⃣ Введи номер телефона → получи код в Telegram\n"
            "3️⃣ Нажми **«API development tools»**\n"
            "4️⃣ Заполни форму (App title: `WifeAI`)\n"
            "5️⃣ Скопируй **App api_id** (числа)\n\n"
            "👇 **Введи api_id:**",
            parse_mode="Markdown"
        )
        return STATE_ONBOARDING_API_ID

    async def _handle_api_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if not text.isdigit():
            await update.message.reply_text("❌ API ID должен состоять только из цифр. Попробуй еще раз:")
            return STATE_ONBOARDING_API_ID
            
        context.user_data['api_id'] = int(text)
        await update.message.reply_text(
            "✅ Принято.\n\n"
            "👇 **Теперь введи App api_hash (длинная строка):**"
        )
        return STATE_ONBOARDING_API_HASH

    async def _handle_api_hash(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if len(text) < 10:
            await update.message.reply_text("❌ Похоже на ошибку. Hash должен быть длинным. Попробуй еще раз:")
            return STATE_ONBOARDING_API_HASH
            
        context.user_data['api_hash'] = text
        
        button = KeyboardButton(text="📱 Отправить мой номер", request_contact=True)
        markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
        
        await update.message.reply_text(
            "**Шаг 2 из 4: Авторизация**\n\n"
            "Данные API приняты. Теперь нужно войти в аккаунт.\n"
            "Нажми кнопку ниже или введи номер телефона (например +79001234567):",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return STATE_ONBOARDING_PHONE

    async def _handle_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update.effective_user)
        
        if update.message.contact:
            phone = update.message.contact.phone_number
        else:
            phone = update.message.text.strip()
        
        # Save credentials to user DB temporarily (or permanently)
        user.api_id = context.user_data['api_id']
        user.api_hash = context.user_data['api_hash']
        user.phone = phone
        self.db.save_user(user)
        
        await update.message.reply_text(
            "🔄 Отправляю код подтверждения...",
            reply_markup=ReplyKeyboardRemove()
        )
        
        # Trigger Telethon send_code
        success, msg, phone_code_hash, session_string = await self.tm.send_code(user)
        
        if not success:
            await update.message.reply_text(f"❌ Ошибка отправки кода: {msg}\nПроверь данные и начни заново: /start")
            return ConversationHandler.END
        
        # Store auth data in context (persists across handlers)
        context.user_data['phone_code_hash'] = phone_code_hash
        context.user_data['session_string'] = session_string
            
        await update.message.reply_text(
            "📩 **Код отправлен!**\n"
            "Он придет в Telegram (на твоем устройстве).\n\n"
            "👇 Введи код сюда (например: 12345):",
            parse_mode="Markdown"
        )
        return STATE_ONBOARDING_CODE

    async def _handle_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update.effective_user)
        code = update.message.text.strip()
        
        await update.message.reply_text("🔄 Проверяю код...")
        
        # Get auth data from context
        phone_code_hash = context.user_data.get('phone_code_hash')
        session_string = context.user_data.get('session_string')
        
        if not phone_code_hash or not session_string:
            await update.message.reply_text("❌ Сессия устарела. Начни заново: /start")
            return ConversationHandler.END
        
        success, msg, needs_2fa, new_session = await self.tm.sign_in(
            user, code, phone_code_hash, session_string
        )
        
        # Update session in context for 2FA step
        if new_session:
            context.user_data['session_string'] = new_session
        
        if needs_2fa:
            await update.message.reply_text(
                "🔐 **Требуется облачный пароль (2FA).**\n"
                "👇 Введи свой пароль от двухэтапной аутентификации:",
                parse_mode="Markdown"
            )
            return STATE_ONBOARDING_2FA
            
        if not success:
            await update.message.reply_text(f"❌ Ошибка: {msg}\nПопробуй ввести код еще раз:")
            return STATE_ONBOARDING_CODE
        
        # Auth success
        await self._ask_for_target(update, context)
        return STATE_ONBOARDING_TARGET

    async def _handle_2fa(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update.effective_user)
        password = update.message.text.strip()
        
        await update.message.reply_text("🔄 Проверяю пароль...")
        
        session_string = context.user_data.get('session_string')
        if not session_string:
            await update.message.reply_text("❌ Сессия устарела. Начни заново: /start")
            return ConversationHandler.END
        
        success, msg = await self.tm.sign_in_2fa(user, password, session_string)
        
        if not success:
            await update.message.reply_text(f"❌ Ошибка: {msg}\nПопробуй еще раз:")
            return STATE_ONBOARDING_2FA
            
        await self._ask_for_target(update, context)
        return STATE_ONBOARDING_TARGET

    async def _ask_for_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Helper to ask for target user using Telegram's user picker."""
        # Use KeyboardButtonRequestUsers for user picker
        user_picker = KeyboardButtonRequestUsers(
            request_id=1,  # Unique ID to identify this request
            user_is_bot=False,
            max_quantity=1
        )
        markup = ReplyKeyboardMarkup(
            [[KeyboardButton(text="👤 Выбрать пользователя", request_users=user_picker)]],
            one_time_keyboard=True,
            resize_keyboard=True
        )
        await update.message.reply_text(
            "✅ **Авторизация успешна!**\n\n"
            "**Шаг 3 из 4: Выбор цели**\n"
            "Кому я должен отвечать?\n\n"
            "👇 **Нажми кнопку и выбери человека из списка:**\n"
            "_(или напиши @username вручную)_",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        
        # Start client with the session we obtained during auth
        user = self._get_user(update.effective_user)
        session_string = context.user_data.get('session_string')
        if session_string:
            user.session_string = session_string
            self.db.save_user(user)
        await self.tm.start_client_for_user(user)

    async def _handle_user_shared(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle user selection from Telegram's user picker."""
        user = self._get_user(update.effective_user)
        
        users_shared = update.message.users_shared
        if not users_shared or not users_shared.users:
            await update.message.reply_text("❌ Ошибка выбора. Попробуй ещё раз:")
            return STATE_ONBOARDING_TARGET
        
        shared_user = users_shared.users[0]
        target_id = shared_user.user_id
        
        # Try to get name via Telethon
        target_name = None
        try:
            client = self.tm.get_client(user.user_id)
            if client:
                entity = await client.get_entity(target_id)
                target_name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
        except Exception as e:
            logger.warning(f"Could not resolve user {target_id}: {e}")
            target_name = f"User {target_id}"
        
        # Save target
        user.target_user_id = target_id
        user.target_username = None
        user.target_name = target_name or f"User {target_id}"
        user.state = UserState.READY
        self.db.save_user(user)
        
        await update.message.reply_text(
            "🎉 **Настройка завершена!**\n\n"
            f"Теперь я буду помогать общаться с: **{user.target_name}**\n"
            "По умолчанию AI-ответы **выключены**, чтобы ты мог(ла) проверить настройки.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        
        await self._send_main_menu(update, user)
        return STATE_MAIN_MENU

    async def _handle_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle manual username input for target."""
        user = self._get_user(update.effective_user)
        
        # Username input
        username = update.message.text.strip()
        if username.startswith("@"):
            username = username[1:]
        
        await update.message.reply_text("🔄 Ищу пользователя...", reply_markup=ReplyKeyboardRemove())
        
        # Use Telethon to resolve
        success, tid, tname = await self.tm.resolve_username(user, username)
        if not success:
            await update.message.reply_text(f"❌ Не могу найти @{username}. Попробуй выбрать через кнопку.")
            return STATE_ONBOARDING_TARGET
        
        target_id = tid
        target_name = tname
        target_username = username
            
        if not target_id:
             await update.message.reply_text(f"❌ Не удалось определить ID пользователя. Попробуйте отправить контакт.")
             return STATE_ONBOARDING_TARGET
             
        # Save target
        user.target_user_id = target_id
        user.target_username = target_username
        user.target_name = target_name
        user.state = UserState.READY
        self.db.save_user(user)
        
        await update.message.reply_text(
            "🎉 **Настройка завершена!**\n\n"
            f"Теперь я буду помогать общаться с: **{target_name}**\n"
            "По умолчанию AI-ответы **выключены**, чтобы ты мог(ла) проверить настройки.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        
        await self._send_main_menu(update, user)
        return STATE_MAIN_MENU

    # ========================
    # Main Menu Handlers
    # ========================

    async def _menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        user = self._get_user(update.effective_user)
        
        if data == "toggle_on":
            user.ai_enabled = True
            self.db.save_user(user)
            await self._send_main_menu(update, user, edit=True)
            
        elif data == "toggle_off":
            user.ai_enabled = False
            self.db.save_user(user)
            await self._send_main_menu(update, user, edit=True)
            
        elif data.startswith("pause_"):
            parts = data.split("_")
            duration = parts[1]
            seconds = 0
            if duration == "15m": seconds = 15*60
            elif duration == "1h": seconds = 60*60
            
            user.pause_until_ts = int(time.time()) + seconds
            self.db.save_user(user)
            await self._send_main_menu(update, user, edit=True)
            
        elif data == "resume":
            user.pause_until_ts = 0
            self.db.save_user(user)
            await self._send_main_menu(update, user, edit=True)
            
        elif data == "refresh":
            await self._send_main_menu(update, user, edit=True)
            
        elif data == "settings_menu":
            await self._send_settings_menu(update, user, edit=True)
            
        elif data.startswith("set_"):
            return await self._handle_setting_selection(update, context, user, data)
            
        elif data == "back_to_main":
            await self._send_main_menu(update, user, edit=True)
            return STATE_MAIN_MENU
        
        elif data == "back_to_settings":
            user.pending_setting = None
            self.db.save_user(user)
            await self._send_settings_menu(update, user, edit=True)
            return STATE_MAIN_MENU
            
        return STATE_MAIN_MENU

    async def _unknown_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle unknown text in main menu."""
        await update.message.reply_text("Используйте кнопки меню 👇")
        user = self._get_user(update.effective_user)
        await self._send_main_menu(update, user)
        return STATE_MAIN_MENU

    # ========================
    # Settings Handlers
    # ========================

    async def _send_settings_menu(self, update: Update, user: UserData, edit: bool = False) -> None:
        """Send settings menu."""
        target_display = user.target_name or user.target_username or str(user.target_user_id)
        kb = [
            [InlineKeyboardButton(f"🎯 Цель: {target_display}", callback_data="set_target")],
            [InlineKeyboardButton(f"🌍 Часовой пояс: {user.timezone}", callback_data="set_timezone")],
            [InlineKeyboardButton(f"🌙 Тихие часы: {user.quiet_hours_start or 'Выкл'}", callback_data="set_quiet")],
            [InlineKeyboardButton("🎨 Профиль стиля", callback_data="set_style")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
        ]
        text = "⚙️ **Настройки**\nВыберите, что хотите изменить:"
        markup = InlineKeyboardMarkup(kb)
        
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

    async def _handle_setting_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user: UserData, data: str) -> int:
        """Enter setting input mode."""
        setting = data.replace("set_", "")
        user.pending_setting = setting
        self.db.save_user(user)
        
        if setting == "target":
            # Use user picker for target change
            user_picker = KeyboardButtonRequestUsers(
                request_id=2,  # Different ID for settings
                user_is_bot=False,
                max_quantity=1
            )
            reply_kb = ReplyKeyboardMarkup(
                [[KeyboardButton(text="👤 Выбрать пользователя", request_users=user_picker)]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
            await update.callback_query.message.reply_text(
                "🎯 **Смена цели**\n\n"
                "👇 Нажми кнопку и выбери нового человека:\n"
                "_(или напиши @username вручную)_",
                reply_markup=reply_kb,
                parse_mode="Markdown"
            )
            return STATE_SETTINGS_INPUT
        
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="back_to_settings")]])
        
        if setting == "timezone":
            text = "🌍 Введите часовой пояс (например, `Europe/Moscow`):"
        elif setting == "quiet":
            text = "🌙 Введите тихие часы в формате `Start-End` (например, `23:00-08:00`), или `off` чтобы выключить:"
        elif setting == "style":
            current = user.style_profile or "Стандартный"
            text = f"🎨 **Текущий стиль:**\n{current}\n\n👇 Отправьте новый текст описания стиля:"
        else:
            text = "Введите значение:"
            
        await update.callback_query.message.reply_text(text, reply_markup=back_kb, parse_mode="Markdown")
        return STATE_SETTINGS_INPUT

    async def _handle_setting_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update.effective_user)
        setting = user.pending_setting
        value = update.message.text.strip()
        
        if setting == "target":
            # Handle target change via username
            username = value.lstrip("@")
            await update.message.reply_text("🔄 Ищу пользователя...")
            success, tid, tname = await self.tm.resolve_username(user, username)
            if not success:
                await update.message.reply_text(f"❌ Не могу найти @{username}. Проверь имя:")
                return STATE_SETTINGS_INPUT
            user.target_user_id = tid
            user.target_username = username
            user.target_name = tname
            await update.message.reply_text(f"✅ Цель изменена на **{tname}**", parse_mode="Markdown")
                
        elif setting == "timezone":
            try:
                ZoneInfo(value)
                user.timezone = value
                await update.message.reply_text(f"✅ Часовой пояс изменен на {value}")
            except:
                await update.message.reply_text("❌ Неверный формат. Попробуйте еще раз (например Europe/Moscow):")
                return STATE_SETTINGS_INPUT
                
        elif setting == "quiet":
            if value.lower() == "off":
                user.quiet_hours_start = None
                user.quiet_hours_end = None
                await update.message.reply_text("✅ Тихие часы выключены")
            else:
                parts = value.split("-")
                if len(parts) == 2 and all(":" in p for p in parts):
                    user.quiet_hours_start = parts[0].strip()
                    user.quiet_hours_end = parts[1].strip()
                    await update.message.reply_text(f"✅ Тихие часы: {user.quiet_hours_start} - {user.quiet_hours_end}")
                else:
                    await update.message.reply_text("❌ Неверный формат. Используйте HH:MM-HH:MM (например 23:00-08:00):")
                    return STATE_SETTINGS_INPUT
                    
        elif setting == "style":
            user.style_profile = value
            await update.message.reply_text("✅ Стиль обновлен!")
            
        user.pending_setting = None
        self.db.save_user(user)
        
        await self._send_main_menu(update, user)
        return STATE_MAIN_MENU

    async def _handle_target_change_shared(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle user picker selection when changing target from settings."""
        user = self._get_user(update.effective_user)
        
        users_shared = update.message.users_shared
        if not users_shared or not users_shared.users:
            await update.message.reply_text("❌ Ошибка выбора. Попробуй ещё раз:")
            return STATE_SETTINGS_INPUT
        
        shared_user = users_shared.users[0]
        target_id = shared_user.user_id
        
        # Try to get name via Telethon
        target_name = None
        try:
            client = self.tm.get_client(user.user_id)
            if client:
                entity = await client.get_entity(target_id)
                target_name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
        except Exception as e:
            logger.warning(f"Could not resolve user {target_id}: {e}")
            target_name = f"User {target_id}"
        
        # Save target
        user.target_user_id = target_id
        user.target_username = None
        user.target_name = target_name or f"User {target_id}"
        user.pending_setting = None
        self.db.save_user(user)
        
        await update.message.reply_text(
            f"✅ Цель изменена на **{user.target_name}**",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        
        await self._send_main_menu(update, user)
        return STATE_MAIN_MENU

def create_admin_bot(token: str, db: Database, tm: TelethonManager) -> Application:
    """Create and configure admin bot."""
    bot = AdminBot(token, db, tm)
    return bot.app
