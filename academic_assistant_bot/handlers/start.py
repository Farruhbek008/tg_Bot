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
                KeyboardButton(text="🤖 ИИ-тьютор (Gemini)"),
                KeyboardButton(text="⏰ Напоминания"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел меню...",
    )


def get_home_inline_kb(sem_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Расписание пар", callback_data="nav:schedule"),
                InlineKeyboardButton(text="📝 Задания (Topshiriqlar)", callback_data="nav:tasks"),
            ],
            [
                InlineKeyboardButton(text="📚 Ресурсы предметов", callback_data="nav:subjects"),
                InlineKeyboardButton(text="📊 Баллы, GPA и пропуски", callback_data="nav:perf"),
            ],
            [
                InlineKeyboardButton(text="🤖 ИИ-тьютор (Gemini)", callback_data="nav:tutor"),
                InlineKeyboardButton(text="⏰ Умные напоминания", callback_data="nav:reminders"),
            ],
            [
                InlineKeyboardButton(
                    text=f"🎓 Выбран: {sem_num}-семестр (Сменить семестр)",
                    callback_data="sem:menu",
                )
            ],
        ]
    )


def _build_welcome_text(sem_num: int) -> str:
    return (
        "🎓 <b>Персональный учебный ассистент (HEMIS + Gemini AI)</b>\n"
        f"📌 Активный семестр: <b>{sem_num}-семестр</b>\n\n"
        "Выберите нужный раздел кнопками ниже (все окна обновляются внутри одного сообщения, не засоряя чат):"
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(
    message: Message, state: FSMContext, bot: Bot, hemis_client: HemisClient
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    sem_num = hemis_client.get_user_semester_number(user_id)

    # При первом /start отправляем компактную нижнюю клавиатуру быстрого доступа и главное inline-меню
    await try_delete_message(message)
    await bot.send_message(
        chat_id=message.chat.id,
        text="⚡️ <i>Панель быстрого доступа активирована.</i>",
        reply_markup=get_main_reply_kb(),
    )
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=_build_welcome_text(sem_num),
        reply_markup=get_home_inline_kb(sem_num),
    )


@router.callback_query(F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery, state: FSMContext, bot: Bot, hemis_client: HemisClient
) -> None:
    await state.clear()
    await callback.answer()
    sem_num = hemis_client.get_user_semester_number(callback.from_user.id)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=_build_welcome_text(sem_num),
        reply_markup=get_home_inline_kb(sem_num),
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "sem:menu")
async def cb_semester_menu(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer()
    current_sem = hemis_client.get_user_semester_number(callback.from_user.id)

    rows = []
    for row_start in (1, 5):
        btn_row = []
        for s in range(row_start, row_start + 4):
            mark = "✅ " if s == current_sem else ""
            btn_row.append(
                InlineKeyboardButton(
                    text=f"{mark}{s}-сем",
                    callback_data=f"sem:set:{s}",
                )
            )
        rows.append(btn_row)

    rows.append([InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="nav:home")])

    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            "🎓 <b>Выбор учебного семестра</b>\n\n"
            f"Сейчас выбран: <b>{current_sem}-семестр</b>.\n"
            "Выберите семестр, чтобы фильтровать предметы, ресурсы, задания и посещаемость строго по нему:"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("sem:set:"))
async def cb_semester_set(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    sem_num = int(callback.data.split(":")[2])
    hemis_client.set_user_semester_by_number(callback.from_user.id, sem_num)
    await callback.answer(f"Установлен {sem_num}-семестр!")
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=_build_welcome_text(sem_num),
        reply_markup=get_home_inline_kb(sem_num),
        prefer_message=callback.message,
    )
