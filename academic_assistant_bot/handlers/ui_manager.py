import logging
from typing import Dict, Optional, Set

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

# Все тексты кнопок нижнего меню быстрого доступа (ReplyKeyboard),
# чтобы текстовые FSM-хэндлеры (ИИ-тьютор, Напоминания, Заметки) не перехватывали переходы по меню
MAIN_MENU_BUTTON_TEXTS: Set[str] = {
    "📅 Расписание",
    "📝 Задания и дедлайны",
    "📚 Ресурсы предметов",
    "📊 Баллы и посещаемость",
    "🤖 ИИ-тьютор",
    "🤖 ИИ-тьютор (Gemini)",
    "⚙️ Выбор ИИ-модели",
    "⏰ Напоминания",
    "📓 Конспекты и заметки",
}

# Хранит ID последнего активного «чистого экрана» для каждого чата
_ACTIVE_SCREEN_MSG_ID: Dict[int, int] = {}


def register_active_message(chat_id: int, message_id: int) -> None:
    """Регистрирует ID сообщения (например, отправленного документа) как текущий активный экран."""
    _ACTIVE_SCREEN_MSG_ID[chat_id] = message_id


async def try_delete_message(message: Optional[Message]) -> None:
    """Безопасно удаляет сообщение из чата, игнорируя ошибки прав/времени."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception as exc:
        logger.debug(
            "Could not delete message %s: %s",
            getattr(message, "message_id", None),
            exc,
        )
    finally:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        msg_id = getattr(message, "message_id", None)
        if chat_id is not None and _ACTIVE_SCREEN_MSG_ID.get(chat_id) == msg_id:
            _ACTIVE_SCREEN_MSG_ID.pop(chat_id, None)


async def render_clean_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    prefer_message: Optional[Message] = None,
    user_trigger_msg: Optional[Message] = None,
) -> Message:
    """
    Обновляет текущее окно бота (Single-Message UI), не засоряя чат новыми сообщениями.
    """
    if len(text) > 4090:
        text = text[:4020] + "\n\n<i>... (текст сокращён)</i>"

    if user_trigger_msg is not None:
        await try_delete_message(user_trigger_msg)

    if prefer_message is not None:
        has_media = bool(
            getattr(prefer_message, "document", None)
            or getattr(prefer_message, "photo", None)
            or getattr(prefer_message, "video", None)
            or getattr(prefer_message, "audio", None)
        )
        if not has_media:
            try:
                edited = await prefer_message.edit_text(
                    text=text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                if isinstance(edited, Message):
                    _ACTIVE_SCREEN_MSG_ID[chat_id] = edited.message_id
                    return edited
                _ACTIVE_SCREEN_MSG_ID[chat_id] = prefer_message.message_id
                return prefer_message
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    _ACTIVE_SCREEN_MSG_ID[chat_id] = prefer_message.message_id
                    return prefer_message
                logger.debug(
                    "Cannot edit prefer_message in place, recreating: %s", exc
                )
            except Exception as exc:
                logger.debug(
                    "Unexpected error editing prefer_message, recreating: %s",
                    exc,
                )

        await try_delete_message(prefer_message)

    prev_msg_id = _ACTIVE_SCREEN_MSG_ID.pop(chat_id, None)
    if prev_msg_id is not None and (
        prefer_message is None
        or prev_msg_id != getattr(prefer_message, "message_id", None)
    ):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
        except Exception:
            pass

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    _ACTIVE_SCREEN_MSG_ID[chat_id] = sent.message_id
    return sent