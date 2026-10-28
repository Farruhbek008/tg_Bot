from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.ui_manager import render_clean_screen
from services.gemini_service import GeminiTutorService

router = Router(name="ai_tutor_router")


class TutorStates(StatesGroup):
    chatting = State()


def get_tutor_controls_kb(return_task_idx: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if return_task_idx is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к карточке задания",
                    callback_data=f"task:view:{return_task_idx}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="tutor:clear"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


TUTOR_WELCOME_TEXT = (
    "🎓 <b>Режим академического ИИ-тьютора (Gemini) активирован!</b>\n\n"
    "Напишите любой вопрос по учебе (базы данных, компьютерные сети, дискретные структуры, "
    "линейная алгебра, электроника, программирование) или попросите разобрать тему/лабораторную.\n\n"
    "<i>Все ваши сообщения и ответы будут аккуратно обновляться в этом окне, не засоряя чат.</i>"
)


@router.message(Command("tutor"))
@router.message(F.text == "🤖 ИИ-тьютор (Gemini)")
async def msg_start_tutor(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=TUTOR_WELCOME_TEXT,
        reply_markup=get_tutor_controls_kb(),
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:tutor")
async def cb_start_tutor(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=TUTOR_WELCOME_TEXT,
        reply_markup=get_tutor_controls_kb(),
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "tutor:clear")
async def cb_clear_tutor_context(
    callback: CallbackQuery, state: FSMContext, bot: Bot, gemini_service: GeminiTutorService
) -> None:
    gemini_service.clear_history(callback.from_user.id)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await callback.answer("Контекст диалога очищен!")
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text="🧹 <b>История диалога сброшена!</b>\n\nЗадайте новый вопрос по любому учебному предмету:",
        reply_markup=get_tutor_controls_kb(),
        prefer_message=callback.message,
    )


@router.message(TutorStates.chatting, F.text)
async def handle_tutor_question(
    message: Message, state: FSMContext, bot: Bot, gemini_service: GeminiTutorService
) -> None:
    if message.text in {
        "📅 Расписание",
        "📝 Задания и дедлайны",
        "📚 Ресурсы предметов",
        "📊 Баллы и посещаемость",
        "⏰ Напоминания",
    }:
        await state.clear()
        return

    user_id = message.from_user.id if message.from_user else 0
    user_question = message.text
    data = await state.get_data()
    assignment_context = data.get("assignment_context")
    return_task_idx = data.get("return_task_idx")

    thinking_screen = await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=(
            f"💬 <b>Ваш вопрос:</b> <i>{user_question}</i>\n\n"
            "🤔 <i>Академический тьютор Gemini готовит ответ...</i>"
        ),
        reply_markup=get_tutor_controls_kb(return_task_idx),
        user_trigger_msg=message,
    )

    answer = await gemini_service.ask_tutor(
        user_id=user_id,
        question=user_question,
        assignment_context=assignment_context,
    )

    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=(
            f"💬 <b>Вопрос:</b> <i>{user_question}</i>\n\n"
            f"🎓 <b>Ответ тьютора:</b>\n{answer}"
        ),
        reply_markup=get_tutor_controls_kb(return_task_idx),
        prefer_message=thinking_screen,
    )
