import html
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from handlers.ui_manager import render_clean_screen, try_delete_message
from services.gemini_service import GeminiTutorService
from services.hemis_client import HemisClient

router = Router(name="start_router")


def get_main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📅 Расписание"),
                KeyboardButton(text="📝 Задания и дедлайны"),
            ],
            [
                KeyboardButton(text="📚 Ресурсы предметов"),
                KeyboardButton(text="📊 Баллы и посещаемость"),
            ],
            [
                KeyboardButton(text="🤖 ИИ-тьютор"),
                KeyboardButton(text="⚙️ Выбор ИИ-модели"),
            ],
            [
                KeyboardButton(text="⏰ Напоминания"),
                KeyboardButton(text="📓 Конспекты и заметки"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел меню...",
    )


def get_home_inline_kb(
    sem_num: int, ai_short_label: str = "⚡ Groq"
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Расписание занятий", callback_data="nav:schedule"
                ),
                InlineKeyboardButton(
                    text="📝 Задания и дедлайны", callback_data="nav:tasks"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📚 Ресурсы предметов", callback_data="nav:subjects"
                ),
                InlineKeyboardButton(
                    text="📊 Баллы, GPA и пропуски", callback_data="nav:perf"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤖 ИИ-тьютор", callback_data="nav:tutor"
                ),
                InlineKeyboardButton(
                    text=f"⚙️ ИИ: {ai_short_label}", callback_data="ai:models"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏰ Умные напоминания", callback_data="nav:reminders"
                ),
                InlineKeyboardButton(
                    text="📓 Конспекты и заметки", callback_data="nav:notes"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🎓 Учебный семестр: {sem_num}-й семестр",
                    callback_data="sem:menu",
                ),
            ],
        ]
    )


def _build_welcome_text(sem_num: int, ai_badge: str) -> str:
    return (
        "🎓 <b>Персональный учебный ассистент студента (HEMIS + ИИ)</b>\n\n"
        f"📌 Активный семестр: <b>{sem_num}-й семестр</b>\n"
        f"🤖 Текущая ИИ-модель: <b>{html.escape(ai_badge)}</b>\n\n"
        "Выберите нужный раздел кнопками ниже (все разделы, расписание, задания и материалы полностью на русском языке):"
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(
    message: Message,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    gemini_service: GeminiTutorService,
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    await hemis_client.load_user_semester_from_db(user_id)
    await gemini_service.load_user_prefs(user_id)

    sem_num = hemis_client.get_user_semester_number(user_id)
    ai_info = gemini_service.get_user_ai_summary(user_id)

    await try_delete_message(message)
    await bot.send_message(
        chat_id=message.chat.id,
        text="⚡️ <i>Панель быстрого доступа активирована.</i>",
        reply_markup=get_main_reply_kb(),
    )
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=_build_welcome_text(sem_num, ai_info["badge"]),
        reply_markup=get_home_inline_kb(sem_num, ai_info["provider_short"]),
    )


@router.callback_query(F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    gemini_service: GeminiTutorService,
) -> None:
    await state.clear()
    await callback.answer()
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    await gemini_service.load_user_prefs(user_id)

    sem_num = hemis_client.get_user_semester_number(user_id)
    ai_info = gemini_service.get_user_ai_summary(user_id)

    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=_build_welcome_text(sem_num, ai_info["badge"]),
        reply_markup=get_home_inline_kb(sem_num, ai_info["provider_short"]),
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "sem:menu")
async def cb_semester_menu(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer()
    await hemis_client.load_user_semester_from_db(callback.from_user.id)
    current_sem = hemis_client.get_user_semester_number(callback.from_user.id)

    rows = []
    for row_start in (1, 5):
        btn_row = []
        for s in range(row_start, row_start + 4):
            mark = "✅ " if s == current_sem else ""
            btn_row.append(
                InlineKeyboardButton(
                    text=f"{mark}{s}-й сем.",
                    callback_data=f"sem:set:{s}",
                )
            )
        rows.append(btn_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад в главное меню", callback_data="nav:home"
            )
        ]
    )

    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            "🎓 <b>Выбор учебного семестра</b>\n\n"
            f"Сейчас выбран: <b>{current_sem}-й семестр</b>.\n"
            "Выберите семестр, чтобы фильтровать дисциплины, учебные ресурсы, задания и посещаемость строго по нему:"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("sem:set:"))
async def cb_semester_set(
    callback: CallbackQuery,
    bot: Bot,
    hemis_client: HemisClient,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = callback.from_user.id
    sem_num = int(callback.data.split(":")[2])
    sem_code = hemis_client.set_user_semester_by_number(user_id, sem_num)
    await hemis_client.save_user_semester_to_db(user_id, sem_code)
    await gemini_service.load_user_prefs(user_id)
    ai_info = gemini_service.get_user_ai_summary(user_id)

    await callback.answer(f"Установлен {sem_num}-й семестр!")
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=_build_welcome_text(sem_num, ai_info["badge"]),
        reply_markup=get_home_inline_kb(sem_num, ai_info["provider_short"]),
        prefer_message=callback.message,
    )
