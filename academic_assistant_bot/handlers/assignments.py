import html
from typing import Any, Dict, List
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers.ai_tutor import TutorStates
from handlers.ui_manager import (
    register_active_message,
    render_clean_screen,
    try_delete_message,
)
from services.file_storage import FileStorageService
from services.gemini_service import GeminiTutorService
from services.hemis_client import HemisClient

router = Router(name="assignments_router")

# Кэш заданий с привязкой к пользователю и коду семестра: {f"{user_id}_{sem_code}": [...]}
_ASSIGNMENTS_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def _build_assignments_list_view(
    assignments: List[Dict[str, Any]], sem_num: int, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    if not assignments:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Обновить", callback_data="tasks:list:0:refresh"
                    ),
                    InlineKeyboardButton(
                        text="🎓 Сменить семестр", callback_data="sem:menu"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню", callback_data="nav:home"
                    )
                ],
            ]
        )
        return (
            f"📝 <b>Задания и дедлайны ({sem_num}-й семестр)</b>\n\n"
            "✅ В выбранном семестре активных заданий пока не найдено (также проверьте раздел «📚 Ресурсы предметов»).",
            kb,
        )

    page_size = 8
    total_pages = max(1, (len(assignments) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_items = assignments[page * page_size : (page + 1) * page_size]

    lines = [
        f"📝 <b>Учебные задания по дисциплинам ({sem_num}-й семестр, всего: {len(assignments)}):</b>",
        "<i>Нажмите на нужное задание ниже, чтобы открыть подробности, скачать прикреплённый файл и разобрать его с ИИ-тьютором:</i>\n",
    ]
    kb_rows = []

    for offset, item in enumerate(page_items):
        idx = page * page_size + offset
        subj_safe = html.escape(str(item.get("subject", "Дисциплина")))
        title_safe = html.escape(str(item.get("title", "Учебное задание")))
        deadline_safe = html.escape(str(item.get("deadline", "Не указан")))
        status_safe = html.escape(str(item.get("status", "📌 Активно")))
        t_type_raw = str(item.get("training_type") or "")
        t_type = f" ({html.escape(t_type_raw)})" if t_type_raw else ""

        lines.append(
            f"<b>{idx + 1}. {subj_safe}</b>{t_type}\n"
            f"   📌 {title_safe}\n"
            f"   ⏳ Срок сдачи: <code>{deadline_safe}</code> | {status_safe}"
        )
        btn_label = (
            f"📂 №{idx + 1}. {item.get('subject', '')[:20]} — {item.get('deadline', '')}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=btn_label, callback_data=f"task:view:{idx}"
                )
            ]
        )

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ Пред.", callback_data=f"tasks:list:{page - 1}"
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"Стр. {page + 1}/{total_pages}", callback_data="noop"
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="След. ➡️", callback_data=f"tasks:list:{page + 1}"
                )
            )
        kb_rows.append(nav_row)

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить список", callback_data="tasks:list:0:refresh"
            ),
            InlineKeyboardButton(text="🎓 Семестр", callback_data="sem:menu"),
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]
    )
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _get_task_detail_kb(
    task_idx: int, files: List[Dict[str, Any]]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🤖 Разобрать задание и файл с ИИ-тьютором",
                callback_data=f"task:ai:{task_idx}",
            )
        ]
    ]
    for f_idx, f_obj in enumerate(files[:4]):
        f_name = (f_obj.get("name") or f"Файл №{f_idx + 1}")[:28]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📥 Скачать: {f_name}",
                    callback_data=f"task:dl:{task_idx}:{f_idx}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку заданий", callback_data="tasks:list:0"
            ),
            InlineKeyboardButton(text="🏠 Меню", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("assignments"))
@router.message(F.text == "📝 Задания и дедлайны")
async def msg_assignments_list(
    message: Message,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    sem_num = hemis_client.get_user_semester_number(user_id)
    cache_key = f"{user_id}_{sem_code}"

    assignments = await hemis_client.get_assignments(user_id=user_id)
    _ASSIGNMENTS_CACHE[cache_key] = assignments

    text, kb = _build_assignments_list_view(assignments, sem_num, page=0)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:tasks")
@router.callback_query(F.data.startswith("tasks:list"))
async def cb_assignments_list(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    await callback.answer("Загружаю список заданий...")
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    sem_num = hemis_client.get_user_semester_number(user_id)
    cache_key = f"{user_id}_{sem_code}"

    parts = callback.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    force_refresh = len(parts) > 3 and parts[3] == "refresh"

    assignments = None if force_refresh else _ASSIGNMENTS_CACHE.get(cache_key)
    if assignments is None:
        assignments = await hemis_client.get_assignments(user_id=user_id)
        _ASSIGNMENTS_CACHE[cache_key] = assignments

    text, kb = _build_assignments_list_view(assignments, sem_num, page=page)
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
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    file_storage: FileStorageService,
) -> None:
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    cache_key = f"{user_id}_{sem_code}"

    parts = callback.data.split(":")
    action = parts[1]
    task_idx = int(parts[2])
    file_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    tasks = _ASSIGNMENTS_CACHE.get(cache_key)
    if not tasks:
        tasks = await hemis_client.get_assignments(user_id=user_id)
        _ASSIGNMENTS_CACHE[cache_key] = tasks

    if task_idx >= len(tasks):
        await callback.answer("Список устарел, обновляю...", show_alert=True)
        return await cb_assignments_list(callback, state, bot, hemis_client)

    task = tasks[task_idx]
    files = task.get("files") or []
    t_type_raw = str(task.get("training_type") or "")
    t_type = f" ({html.escape(t_type_raw)})" if t_type_raw else ""
    raw_desc = (
        task.get("description")
        or "Подробное условие находится в прикреплённом файле задания."
    )

    subj_safe = html.escape(str(task.get("subject", "Дисциплина")))
    title_safe = html.escape(str(task.get("title", "Учебное задание")))
    status_safe = html.escape(str(task.get("status", "📌 Активно")))
    max_ball_safe = html.escape(str(task.get("max_ball", "—")))
    deadline_safe = html.escape(str(task.get("deadline", "Не указан")))
    desc_safe = html.escape(str(raw_desc))

    detail_text = (
        f"📂 <b>Задание №{task_idx + 1}: {subj_safe}{t_type}</b>\n\n"
        f"📌 <b>Тема:</b> {title_safe}\n"
        f"📊 <b>Статус:</b> {status_safe} | 🏆 <b>Макс. балл:</b> {max_ball_safe}\n"
        f"⏳ <b>Срок сдачи (дедлайн):</b> <code>{deadline_safe}</code>\n\n"
        f"📝 <b>Описание задания:</b>\n{desc_safe}"
    )

    detail_kb = _get_task_detail_kb(task_idx, files=files)

    if files and file_idx < len(files) and files[file_idx].get("url"):
        target_file = files[file_idx]
        await callback.answer("Загружаю задание и прикреплённый файл...")
        try:
            sent_doc = await file_storage.send_or_cache_file(
                bot=bot,
                chat_id=callback.message.chat.id,
                source_url=target_file["url"],
                file_name=target_file["name"],
                caption=detail_text[:1000],
                reply_markup=detail_kb,
            )
            if sent_doc:
                await try_delete_message(callback.message)
                register_active_message(
                    callback.message.chat.id, sent_doc.message_id
                )
                return
        except Exception as exc:
            if action == "dl":
                detail_text += f"\n\n⚠️ <i>Не удалось автоматически скачать файл ({html.escape(str(exc))}).</i>"

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
    file_storage: FileStorageService,
    gemini_service: GeminiTutorService,
) -> None:
    await callback.answer("Запускаю разбор задания с ИИ-тьютором...")
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    await gemini_service.load_user_prefs(user_id)
    ai_summary = gemini_service.get_user_ai_summary(user_id)

    sem_code = hemis_client.get_user_semester_code(user_id)
    cache_key = f"{user_id}_{sem_code}"
    task_idx = int(callback.data.split(":")[2])

    tasks = _ASSIGNMENTS_CACHE.get(
        cache_key
    ) or await hemis_client.get_assignments(user_id=user_id)
    _ASSIGNMENTS_CACHE[cache_key] = tasks
    if task_idx >= len(tasks):
        return await cb_assignments_list(callback, state, bot, hemis_client)

    task = tasks[task_idx]
    files = task.get("files") or []
    subj_safe = html.escape(str(task.get("subject", "Дисциплина")))
    title_safe = html.escape(str(task.get("title", "Задание")))
    badge_safe = html.escape(ai_summary["badge"])

    loading_screen = await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            f"🤖 <b>Модель:</b> <code>{badge_safe}</code>\n\n"
            f"⏳ <i>ИИ-тьютор анализирует задание и прикреплённые материалы по дисциплине «{subj_safe}»...</i>"
        ),
        prefer_message=callback.message,
    )

    file_bytes = None
    file_name = None
    if files and files[0].get("url"):
        try:
            file_bytes, file_name = await file_storage.get_or_download_bytes(
                source_url=files[0]["url"],
                file_name=files[0].get("name") or "Задание.pdf",
            )
        except Exception:
            file_bytes = None

    extracted_doc_text = ""
    if file_bytes and file_name:
        extracted_doc_text = gemini_service.extract_text_from_file_bytes(
            file_bytes, file_name
        )

    context_str = (
        f"Дисциплина: {task['subject']}\n"
        f"Тип занятия: {task.get('training_type', '')}\n"
        f"Тема задания: {task['title']}\n"
        f"Описание: {task.get('description') or 'Требуется выполнить учебное задание по теме.'}\n"
        f"Срок сдачи: {task['deadline']}"
    )
    if extracted_doc_text:
        context_str += f"\n\nТекст из прикреплённого файла ({file_name}):\n{extracted_doc_text[:8000]}"

    await state.set_state(TutorStates.chatting)
    await state.update_data(
        assignment_context=context_str, return_task_idx=task_idx
    )

    explanation = await gemini_service.ask_tutor(
        user_id=user_id,
        question=(
            "Составь подробный пошаговый план выполнения этого учебного задания на русском языке, "
            "объясни ключевые понятия и напиши, что именно нужно подготовить студенту."
        ),
        assignment_context=context_str,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💾 Сохранить разбор в заметки",
                    callback_data="notes:save_ai",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⚙️ Модель ({ai_summary['badge']})",
                    callback_data="ai:models",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К карточке задания",
                    callback_data=f"task:view:{task_idx}",
                ),
                InlineKeyboardButton(
                    text="📋 К списку заданий",
                    callback_data="tasks:list:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Очистить контекст", callback_data="tutor:clear"
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
        text=(
            f"🎓 <b>ИИ-разбор задания: {subj_safe}</b>\n"
            f"📌 <i>{title_safe}</i>\n"
            f"🤖 Модель: <code>{badge_safe}</code>\n\n"
            f"{explanation}\n\n"
            "💬 <i>Вы можете прямо сейчас написать в чат любой уточняющий вопрос по этому заданию!</i>"
        ),
        reply_markup=kb,
        prefer_message=loading_screen,
    )
