from datetime import datetime
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
import pytz

from config import settings
from handlers.ui_manager import render_clean_screen
from services.reminder_service import ReminderService

router = Router(name="reminders_router")


class ReminderStates(StatesGroup):
    waiting_for_text = State()


async def _build_reminders_view(
    user_id: int, reminder_service: ReminderService, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    active = await reminder_service.list_active_reminders(user_id=user_id)
    tz = pytz.timezone(settings.timezone)

    lines = ["⏰ <b>Умные напоминания (Asia/Tashkent)</b>\n"]
    if notice:
        lines.append(f"{notice}\n")

    lines.extend(
        [
            "Напишите в чат любую фразу с временем, например:",
            "• <i>«напомни в 8 вечера отправить презентацию»</i>",
            "• <i>«напомни завтра в 09:30 сдать лабораторную по сетям»</i>",
            "• <i>«через 30 минут повторить конспект по базам данных»</i>\n",
        ]
    )

    kb_rows = []
    if active:
        lines.append("📋 <b>Ваши активные напоминания:</b>")
        for idx, rem in enumerate(active, start=1):
            dt_local = rem.remind_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
            lines.append(f"{idx}. 🕒 <code>{dt_local}</code> — <b>{rem.task_text}</b>")
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"❌ Удалить #{idx} ({rem.task_text[:22]})",
                        callback_data=f"rem:del:{rem.id}",
                    )
                ]
            )
    else:
        lines.append("ℹ️ У вас пока нет активных напоминаний.")

    kb_rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:reminders"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("remind"))
@router.message(F.text == "⏰ Напоминания")
async def msg_reminders_menu(
    message: Message, state: FSMContext, bot: Bot, reminder_service: ReminderService
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await state.set_state(ReminderStates.waiting_for_text)
    text, kb = await _build_reminders_view(user_id, reminder_service)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:reminders")
async def cb_reminders_menu(
    callback: CallbackQuery, state: FSMContext, bot: Bot, reminder_service: ReminderService
) -> None:
    await callback.answer()
    await state.set_state(ReminderStates.waiting_for_text)
    text, kb = await _build_reminders_view(callback.from_user.id, reminder_service)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("rem:del:"))
async def cb_delete_reminder(
    callback: CallbackQuery, bot: Bot, reminder_service: ReminderService
) -> None:
    rem_id = int(callback.data.split(":")[2])
    await reminder_service.delete_reminder(reminder_id=rem_id, user_id=callback.from_user.id)
    await callback.answer("Напоминание удалено!")
    text, kb = await _build_reminders_view(
        callback.from_user.id, reminder_service, notice="🗑 <i>Напоминание успешно удалено.</i>"
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.message(F.text.lower().startswith("напомни"))
@router.message(ReminderStates.waiting_for_text, F.text)
async def process_natural_reminder(
    message: Message, state: FSMContext, bot: Bot, reminder_service: ReminderService
) -> None:
    if message.text in {
        "📅 Расписание",
        "📝 Задания и дедлайны",
        "📚 Ресурсы предметов",
        "📊 Баллы и посещаемость",
        "🤖 ИИ-тьютор (Gemini)",
    }:
        await state.clear()
        return

    user_id = message.from_user.id if message.from_user else 0
    parsed = await reminder_service.parse_natural_language(message.text)
    if not parsed:
        text, kb = await _build_reminders_view(
            user_id,
            reminder_service,
            notice="⚠️ <b>Не удалось распознать точное время.</b> Попробуйте написать: <i>«напомни сегодня в 20:00 отправить презентацию»</i>.",
        )
        await render_clean_screen(
            bot=bot,
            chat_id=message.chat.id,
            text=text,
            reply_markup=kb,
            user_trigger_msg=message,
        )
        return

    remind_at, task_text = parsed
    tz = pytz.timezone(settings.timezone)
    now = datetime.now(tz)

    if remind_at <= now:
        text, kb = await _build_reminders_view(
            user_id,
            reminder_service,
            notice="⚠️ <b>Указанное время уже прошло!</b> Укажите время в будущем.",
        )
        await render_clean_screen(
            bot=bot,
            chat_id=message.chat.id,
            text=text,
            reply_markup=kb,
            user_trigger_msg=message,
        )
        return

    reminder = await reminder_service.create_reminder(
        user_id=user_id,
        chat_id=message.chat.id,
        task_text=task_text,
        remind_at=remind_at,
    )
    formatted_dt = reminder.remind_at.astimezone(tz).strftime("%d.%m.%Y в %H:%M")
    notice = (
        f"✅ <b>Напоминание сохранено!</b>\n"
        f"📌 Задача: <b>{reminder.task_text}</b> | 🕒 <code>{formatted_dt}</code>"
    )
    text, kb = await _build_reminders_view(user_id, reminder_service, notice=notice)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )
