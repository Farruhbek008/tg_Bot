from typing import Any, Dict, List, Optional
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.ai_tutor import TutorStates, get_tutor_controls_kb
from handlers.ui_manager import render_clean_screen, try_delete_message
from services.file_storage import FileStorageService
from services.gemini_service import GeminiTutorService
from services.hemis_client import HemisClient

router = Router(name="assignments_router")

_ASSIGNMENTS_CACHE: Dict[int, List[Dict[str, Any]]] = {}


def _build_assignments_list_view(
    assignments: List[Dict[str, Any]], sem_num: int
) -> tuple[str, InlineKeyboardMarkup]:
    if not assignments:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="tasks:list"),
                    InlineKeyboardButton(text="🎓 Сменить семестр", callback_data="sem:menu"),
                ],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )
        return (
            f"📝 <b>Задания и дедлайны ({sem_num}-семестр)</b>\n\n"
            "✅ В выбранном семестре заданий пока не найдено (либо проверьте раздел «📚 Ресурсы предметов»).",
            kb,
        )

    lines = [
        f"📝 <b>Задания по предметам ({sem_num}-семестр):</b>",
        "<i>Нажмите на нужное задание ниже, чтобы открыть подробности, получить прикрепленный файл и разобрать его с ИИ-тьютором:</i>\n",
    ]
    kb_rows = []

    for idx, item in enumerate(assignments[:12]):
        t_type = f" ({item['training_type']})" if item.get("training_type") else ""
        lines.append(
            f"<b>{idx + 1}. {item['subject']}</b>{t_type}\n"
            f"   📌 {item['title']}\n"
            f"   ⏳ Muddati (Дедлайн): <code>{item['deadline']}</code> | {item['status']}"
        )
        btn_label = f"📂 #{idx + 1}. {item['subject'][:22]} — {item['deadline']}"
        kb_rows.append(
            [InlineKeyboardButton(text=btn_label, callback_data=f"task:view:{idx}")]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить список", callback_data="tasks:list"),
            InlineKeyboardButton(text="🎓 Семестр", callback_data="sem:menu"),
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _get_task_detail_kb(task_idx: int, has_files: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🤖 Обсудить задание с ИИ-тьютором",
                callback_data=f"task:ai:{task_idx}",
            )
        ]
    ]
    if has_files:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Скачать файл задания ещё раз",
                    callback_data=f"task:dl:{task_idx}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад к списку заданий", callback_data="tasks:list"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("assignments"))
@router.message(F.text == "📝 Задания и дедлайны")
async def msg_assignments_list(
    message: Message, bot: Bot, hemis_client: HemisClient
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    sem_num = hemis_client.get_user_semester_number(user_id)
    assignments = await hemis_client.get_assignments(user_id=user_id)
    _ASSIGNMENTS_CACHE[user_id] = assignments

    text, kb = _build_assignments_list_view(assignments, sem_num)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:tasks")
@router.callback_query(F.data == "tasks:list")
async def cb_assignments_list(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer("Загружаю список заданий...")
    user_id = callback.from_user.id
    sem_num = hemis_client.get_user_semester_number(user_id)
    assignments = await hemis_client.get_assignments(user_id=user_id)
    _ASSIGNMENTS_CACHE[user_id] = assignments

    text, kb = _build_assignments_list_view(assignments, sem_num)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("task:view:"))
@router.callback_query(F.data.startswith("task:dl:"))
async def cb_view_single_assignment(
    callback: CallbackQuery,
    bot: Bot,
    hemis_client: HemisClient,
    file_storage: FileStorageService,
) -> None:
    user_id = callback.from_user.id
    task_idx = int(callback.data.split(":")[2])

    tasks = _ASSIGNMENTS_CACHE.get(user_id)
    if not tasks:
        tasks = await hemis_client.get_assignments(user_id=user_id)
        _ASSIGNMENTS_CACHE[user_id] = tasks

    if task_idx >= len(tasks):
        await callback.answer("Список устарел, обновляю...", show_alert=True)
        return await cb_assignments_list(callback, bot, hemis_client)

    task = tasks[task_idx]
    files = task.get("files") or []
    t_type = f" ({task['training_type']})" if task.get("training_type") else ""
    desc = task.get("description") or "Подробное условие находится в прикрепленном файле задания."

    detail_text = (
        f"📂 <b>Задание #{task_idx + 1}: {task['subject']}{t_type}</b>\n\n"
        f"📌 <b>Тема:</b> {task['title']}\n"
        f"📊 <b>Статус:</b> {task['status']} | 🏆 <b>Макс. балл:</b> {task['max_ball']}\n"
        f"⏳ <b>Дедлайн (Muddati):</b> <code>{task['deadline']}</code>\n\n"
        f"📝 <b>Описание:</b>\n{desc}"
    )

    detail_kb = _get_task_detail_kb(task_idx, has_files=bool(files))

    # Если у задания есть прикрепленный файл — сразу отправляем его с описанием и кнопками «Назад» / «Обсудить с ИИ»,
    # а старое сообщение со списком удаляем, чтобы в чате оставалось только актуальное окно!
    if files and files[0].get("url"):
        await callback.answer("Загружаю задание и прикрепленный файл...")
        try:
            await try_delete_message(callback.message)
            await file_storage.send_or_cache_file(
                bot=bot,
                chat_id=callback.message.chat.id,
                source_url=files[0]["url"],
                file_name=files[0]["name"],
                caption=detail_text[:1000],
                reply_markup=detail_kb,
            )
            return
        except Exception:
            pass

    await callback.answer()
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=detail_text,
        reply_markup=detail_kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("task:ai:"))
async def cb_discuss_task_with_ai(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    gemini_service: GeminiTutorService,
) -> None:
    await callback.answer("Запускаю разбор задания с ИИ-тьютором...")
    user_id = callback.from_user.id
    task_idx = int(callback.data.split(":")[2])

    tasks = _ASSIGNMENTS_CACHE.get(user_id) or await hemis_client.get_assignments(user_id=user_id)
    if task_idx >= len(tasks):
        return await cb_assignments_list(callback, bot, hemis_client)

    task = tasks[task_idx]
    context_str = (
        f"Предмет: {task['subject']}\n"
        f"Тип занятия: {task.get('training_type', '')}\n"
        f"Тема задания: {task['title']}\n"
        f"Описание: {task.get('description') or 'Требуется выполнить учебное задание по теме.'}\n"
        f"Дедлайн: {task['deadline']}"
    )

    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=context_str, return_task_idx=task_idx)

    loading_screen = await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=f"🤖 <i>ИИ-тьютор Gemini анализирует задание по предмету «{task['subject']}»...</i>",
        prefer_message=callback.message,
    )

    explanation = await gemini_service.ask_tutor(
        user_id=user_id,
        question=(
            "Составь пошаговый план выполнения этого учебного задания, "
            "объясни ключевые понятия и напиши, что именно нужно подготовить студенту."
        ),
        assignment_context=context_str,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к карточке задания",
                    callback_data=f"task:view:{task_idx}",
                ),
                InlineKeyboardButton(
                    text="📋 К списку заданий",
                    callback_data="tasks:list",
                ),
            ],
            [
                InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="tutor:clear"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home"),
            ],
        ]
    )

    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            f"🎓 <b>ИИ-разбор задания: {task['subject']}</b>\n"
            f"📌 <i>{task['title']}</i>\n\n"
            f"{explanation}\n\n"
            "💬 <i>Вы можете прямо сейчас написать в чат любой уточняющий вопрос по этому заданию!</i>"
        ),
        reply_markup=kb,
        prefer_message=loading_screen,
    )
