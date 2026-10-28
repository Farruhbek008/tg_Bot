import html
from typing import List, Optional
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import pytz
from sqlalchemy import delete, select

from config import settings
from database.db import async_session_maker
from database.models import UserNote
from handlers.ui_manager import MAIN_MENU_BUTTON_TEXTS, render_clean_screen
from services.gemini_service import GeminiTutorService

router = Router(name="notes_router")


class NoteStates(StatesGroup):
    waiting_for_note = State()


async def _list_user_notes(user_id: int) -> List[UserNote]:
    async with async_session_maker() as session:
        stmt = (
            select(UserNote)
            .where(UserNote.user_id == user_id)
            .order_by(UserNote.created_at.desc())
            .limit(20)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _get_user_note(note_id: int, user_id: int) -> Optional[UserNote]:
    async with async_session_maker() as session:
        stmt = select(UserNote).where(
            UserNote.id == note_id, UserNote.user_id == user_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _create_user_note(
    user_id: int, content: str, subject_name: Optional[str] = None
) -> UserNote:
    async with async_session_maker() as session:
        note = UserNote(
            user_id=user_id,
            subject_name=subject_name,
            content=content,
        )
        session.add(note)
        await session.commit()
        await session.refresh(note)
        return note


async def _delete_user_note(note_id: int, user_id: int) -> None:
    async with async_session_maker() as session:
        await session.execute(
            delete(UserNote).where(
                UserNote.id == note_id, UserNote.user_id == user_id
            )
        )
        await session.commit()


async def _build_notes_list_view(
    user_id: int, notice: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    notes = await _list_user_notes(user_id)
    tz = pytz.timezone(settings.timezone)

    lines = ["📓 <b>Учебные конспекты и заметки</b>\n"]
    if notice:
        lines.append(f"{notice}\n")

    lines.append(
        "<i>Напишите любой текст в чат, чтобы сохранить новую заметку или конспект (формат «Предмет: текст» автоматически привяжет заметку к дисциплине).</i>\n"
    )

    kb_rows = []
    if notes:
        lines.append("🗂 <b>Ваши сохранённые записи:</b>")
        for idx, n in enumerate(notes[:10], start=1):
            dt_str = (
                n.created_at.astimezone(tz).strftime("%d.%m %H:%M")
                if n.created_at
                else ""
            )
            subj = (
                f"[{html.escape(n.subject_name)}] " if n.subject_name else ""
            )
            preview = html.escape(n.content.replace("\n", " ")[:45])
            lines.append(f"{idx}. <code>{dt_str}</code> {subj}{preview}...")
            btn_title = f"📄 №{idx}. {(n.subject_name or n.content)[:26]}"
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=btn_title, callback_data=f"notes:view:{n.id}"
                    ),
                    InlineKeyboardButton(
                        text="❌", callback_data=f"notes:del:{n.id}"
                    ),
                ]
            )
    else:
        lines.append("ℹ️ У вас пока нет сохранённых заметок.")

    kb_rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:notes"),
            InlineKeyboardButton(
                text="🏠 Главное меню", callback_data="nav:home"
            ),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("notes"))
@router.message(F.text == "📓 Конспекты и заметки")
async def msg_notes_menu(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await state.set_state(NoteStates.waiting_for_note)
    text, kb = await _build_notes_list_view(user_id)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:notes")
async def cb_notes_menu(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    await callback.answer()
    await state.set_state(NoteStates.waiting_for_note)
    text, kb = await _build_notes_list_view(callback.from_user.id)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("notes:view:"))
async def cb_view_note(callback: CallbackQuery, bot: Bot) -> None:
    note_id = int(callback.data.split(":")[2])
    note = await _get_user_note(note_id, callback.from_user.id)
    if not note:
        await callback.answer("Заметка не найдена.", show_alert=True)
        return

    await callback.answer()
    subj_line = (
        f"📘 <b>Предмет:</b> {html.escape(note.subject_name)}\n\n"
        if note.subject_name
        else ""
    )
    body = html.escape(note.content[:3500])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить заметку",
                    callback_data=f"notes:del:{note.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К списку заметок", callback_data="nav:notes"
                ),
                InlineKeyboardButton(
                    text="🏠 Главное меню", callback_data="nav:home"
                ),
            ],
        ]
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=f"📓 <b>Заметка №{note.id}</b>\n{subj_line}{body}",
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("notes:del:"))
async def cb_delete_note(callback: CallbackQuery, bot: Bot) -> None:
    note_id = int(callback.data.split(":")[2])
    await _delete_user_note(note_id, callback.from_user.id)
    await callback.answer("Заметка удалена!")
    text, kb = await _build_notes_list_view(
        callback.from_user.id, notice="🗑 <i>Заметка успешно удалена.</i>"
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "notes:save_ai")
async def cb_save_ai_to_notes(
    callback: CallbackQuery, gemini_service: GeminiTutorService
) -> None:
    last_qa = gemini_service.get_last_qa(callback.from_user.id)
    if not last_qa:
        await callback.answer(
            "Нет последнего ответа ИИ для сохранения.", show_alert=True
        )
        return

    question, answer = last_qa
    await _create_user_note(
        user_id=callback.from_user.id,
        subject_name=f"ИИ-конспект: {question[:40]}",
        content=answer,
    )
    await callback.answer(
        "✅ Ответ ИИ-тьютора сохранён в раздел «Конспекты и заметки»!",
        show_alert=True,
    )


@router.message(
    NoteStates.waiting_for_note,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTON_TEXTS),
)
async def process_new_note_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else 0
    raw = (message.text or "").strip()
    if not raw:
        return

    subject_name: Optional[str] = None
    content = raw
    if ":" in raw and len(raw.split(":", 1)[0]) <= 40:
        cand_subj, cand_body = raw.split(":", 1)
        if cand_body.strip():
            subject_name = cand_subj.strip()
            content = cand_body.strip()

    await _create_user_note(
        user_id=user_id, content=content, subject_name=subject_name
    )
    text, kb = await _build_notes_list_view(
        user_id, notice="✅ <b>Заметка успешно сохранена!</b>"
    )
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )
