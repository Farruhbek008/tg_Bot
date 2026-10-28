from datetime import datetime, timedelta
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
import pytz

from config import settings
from handlers.ui_manager import render_clean_screen
from services.hemis_client import HemisClient

router = Router(name="schedule_router")


def get_schedule_inline_kb(active_tab: str = "today") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Сегодня" if active_tab == "today" else "Сегодня"),
                    callback_data="sched:today",
                ),
                InlineKeyboardButton(
                    text=("✅ Завтра" if active_tab == "tomorrow" else "Завтра"),
                    callback_data="sched:tomorrow",
                ),
                InlineKeyboardButton(
                    text=("✅ Неделя" if active_tab == "week" else "Неделя"),
                    callback_data="sched:week",
                ),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sched:{active_tab}"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home"),
            ],
        ]
    )


@router.message(Command("schedule"))
@router.message(F.text == "📅 Расписание")
async def msg_schedule_today(
    message: Message, bot: Bot, hemis_client: HemisClient
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    tz = pytz.timezone(settings.timezone)
    today = datetime.now(tz).date()
    lessons = await hemis_client.get_schedule(target_date=today, user_id=user_id)
    text = hemis_client.format_schedule_list(
        lessons, f"Расписание на сегодня ({today.strftime('%d.%m.%Y')})"
    )
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=get_schedule_inline_kb("today"),
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:schedule")
@router.callback_query(F.data.startswith("sched:"))
async def cb_schedule_switch(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer("Загружаю расписание...")
    user_id = callback.from_user.id
    action = "today" if callback.data == "nav:schedule" else callback.data.split(":", 1)[1]
    tz = pytz.timezone(settings.timezone)
    today = datetime.now(tz).date()

    if action == "today":
        lessons = await hemis_client.get_schedule(target_date=today, user_id=user_id)
        text = hemis_client.format_schedule_list(
            lessons, f"Расписание на сегодня ({today.strftime('%d.%m.%Y')})"
        )
    elif action == "tomorrow":
        tomorrow = today + timedelta(days=1)
        lessons = await hemis_client.get_schedule(target_date=tomorrow, user_id=user_id)
        text = hemis_client.format_schedule_list(
            lessons, f"Расписание на завтра ({tomorrow.strftime('%d.%m.%Y')})"
        )
    else:
        start_w, end_w, week_map = await hemis_client.get_week_schedule(user_id=user_id)
        header = f"Расписание на неделю ({start_w.strftime('%d.%m')} — {end_w.strftime('%d.%m.%Y')})"
        if not week_map:
            text = f"📅 <b>{header}</b>\n\nДанных о занятиях на текущую неделю не найдено."
        else:
            blocks = [f"📅 <b>{header}</b>"]
            for day_title, day_lessons in week_map.items():
                blocks.append(hemis_client.format_schedule_list(day_lessons, day_title))
            text = "\n\n".join(blocks)

    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=get_schedule_inline_kb(action),
        prefer_message=callback.message,
    )
