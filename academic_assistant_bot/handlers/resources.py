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

router = Router(name="resources_router")

# Кэш предметов с привязкой к семестру: {f"{user_id}_{sem_code}": [...]}
_SUBJECTS_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_SUBJECT_RESOURCES_CACHE: Dict[str, List[Dict[str, Any]]] = {}

CATEGORY_LABELS = {
    "all": "📚 Все учебные материалы",
    "lecture": "📖 Лекционные материалы",
    "practical": "🛠 Практические занятия",
    "lab": "🔬 Лабораторные работы",
    "independent": "📝 Самостоятельная работа",
    "other": "📎 Прочие материалы",
}


async def _build_performance_view(
    user_id: int, hemis_client: HemisClient
) -> tuple[str, InlineKeyboardMarkup]:
    perf = await hemis_client.get_performance_and_attendance(user_id=user_id)
    sem_num = perf["semester_num"]
    sem_code = perf["semester_code"]
    att = perf["attendance"]
    subjects = perf["subjects"]
    _SUBJECTS_CACHE[f"{user_id}_{sem_code}"] = subjects

    lines = [
        f"📊 <b>Успеваемость и посещаемость ({sem_num}-й семестр)</b>",
        f"🎓 <b>Текущий средний балл (GPA):</b> <code>{html.escape(str(perf['gpa']))} / 5.00</code>\n",
        "🕒 <b>Пропуски занятий (посещаемость):</b>",
        (
            f"• За текущий месяц: <b><code>{att['month_total']} ч.</code></b> "
            f"(без уваж. причины: <code>{att['month_unexcused']} ч.</code> | по уваж. причине: <code>{att['month_excused']} ч.</code>)"
        ),
        (
            f"• Всего за {sem_num}-й семестр: <code>{att['semester_total']} ч.</code> "
            f"(без уваж. причины: {att['semester_unexcused']} ч. | по уваж. причине: {att['semester_excused']} ч.)\n"
        ),
    ]

    if subjects:
        lines.append(
            f"📈 <b>Дисциплины {sem_num}-го семестра и набранные баллы:</b>"
        )
        for idx, s in enumerate(subjects, start=1):
            s_name_safe = html.escape(str(s.get("name", "Дисциплина")))
            lines.append(
                f"{idx}. <b>{s_name_safe}</b> — <code>{s.get('overall_score', 0)} / {s.get('max_score', 100)}</code> "
                f"(Файлов: {s.get('resources_count', 0)})"
            )
    else:
        lines.append("ℹ️ Дисциплины выбранного семестра пока не загружены.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Перейти к учебным ресурсам предметов",
                    callback_data="nav:subjects",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎓 Сменить семестр", callback_data="sem:menu"
                ),
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data="nav:perf"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню", callback_data="nav:home"
                )
            ],
        ]
    )
    return "\n".join(lines), kb


@router.message(Command("performance"))
@router.message(F.text == "📊 Баллы и посещаемость")
async def msg_show_performance(
    message: Message,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    text, kb = await _build_performance_view(user_id, hemis_client)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:perf")
async def cb_show_performance(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    await callback.answer("Обновляю сводку успеваемости и посещаемости...")
    text, kb = await _build_performance_view(
        callback.from_user.id, hemis_client
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


async def _build_subjects_list_view(
    user_id: int, hemis_client: HemisClient
) -> tuple[str, InlineKeyboardMarkup]:
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    sem_num = hemis_client.get_user_semester_number(user_id)
    subjects = await hemis_client.get_semester_subjects(user_id=user_id)
    _SUBJECTS_CACHE[f"{user_id}_{sem_code}"] = subjects

    lines = [
        f"📚 <b>Учебные ресурсы по дисциплинам ({sem_num}-й семестр)</b>\n",
        "Выберите дисциплину ниже, чтобы открыть её материалы и отфильтровать их по типу (Лекции / Практика / Лабораторные / Самостоятельная работа):\n",
    ]
    kb_rows = []

    for idx, s in enumerate(subjects):
        res_cnt = s.get("resources_count", 0)
        badge = f" ({res_cnt} файл.)" if res_cnt else ""
        s_name_safe = html.escape(str(s.get("name", "Дисциплина")))
        s_type_safe = html.escape(str(s.get("type", "Обязательный")))
        lines.append(
            f"• <b>{s_name_safe}</b> — {s_type_safe} | {s.get('load', 180)} ч. | {s.get('credit', 6.0)} кред.{badge}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📘 {s['name']}{badge}",
                    callback_data=f"subj:open:{idx}:all:0",
                )
            ]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🎓 Сменить семестр", callback_data="sem:menu"
            ),
            InlineKeyboardButton(
                text="📊 Баллы и пропуски", callback_data="nav:perf"
            ),
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("resources"))
@router.message(F.text == "📚 Ресурсы предметов")
async def msg_subjects_resources(
    message: Message,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    text, kb = await _build_subjects_list_view(user_id, hemis_client)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:subjects")
async def cb_subjects_resources(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    await callback.answer("Загружаю дисциплины семестра...")
    text, kb = await _build_subjects_list_view(
        callback.from_user.id, hemis_client
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("subj:open:"))
async def cb_open_subject_resources(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
) -> None:
    await state.clear()
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    subj_cache_key = f"{user_id}_{sem_code}"

    parts = callback.data.split(":")
    subj_idx = int(parts[2])
    selected_cat = parts[3] if len(parts) > 3 else "all"
    page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    force_refresh = len(parts) > 5 and parts[5] == "refresh"

    subjects = _SUBJECTS_CACHE.get(subj_cache_key)
    if not subjects:
        subjects = await hemis_client.get_semester_subjects(user_id=user_id)
        _SUBJECTS_CACHE[subj_cache_key] = subjects

    if subj_idx >= len(subjects):
        return await cb_subjects_resources(callback, state, bot, hemis_client)

    subj = subjects[subj_idx]
    res_cache_key = f"{user_id}_{sem_code}_{subj['id']}"

    await callback.answer("Загружаю учебные материалы дисциплины...")
    all_res = (
        None if force_refresh else _SUBJECT_RESOURCES_CACHE.get(res_cache_key)
    )
    if all_res is None:
        all_res = await hemis_client.get_subject_resources(
            subject_id=str(subj["id"]),
            subject_name=subj["name"],
            user_id=user_id,
        )
        _SUBJECT_RESOURCES_CACHE[res_cache_key] = all_res

    cat_counts: Dict[str, int] = {"all": len(all_res)}
    for r in all_res:
        c = r.get("category", "other")
        cat_counts[c] = cat_counts.get(c, 0) + 1

    filtered = (
        all_res
        if selected_cat == "all"
        else [r for r in all_res if r.get("category") == selected_cat]
    )

    page_size = 8
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_items = filtered[page * page_size : (page + 1) * page_size]

    subj_name_safe = html.escape(str(subj.get("name", "Дисциплина")))
    lines = [
        f"📘 <b>{subj_name_safe}</b>",
        f"🗂 Категория: <b>{CATEGORY_LABELS.get(selected_cat, 'Все материалы')}</b> (Найдено: {len(filtered)} из {len(all_res)})\n",
    ]

    if not filtered:
        lines.append(
            "ℹ️ В этой категории пока нет загруженных преподавателем файлов. Выберите другой тип занятий кнопками ниже:"
        )
    else:
        lines.append(
            "<i>Нажмите на нужный материал ниже, чтобы скачать файл и при необходимости разобрать его с ИИ-тьютором:</i>\n"
        )
        for i, r in enumerate(page_items, start=page * page_size + 1):
            t_type_safe = html.escape(str(r.get("training_type", "Материал")))
            title_safe = html.escape(str(r.get("title", f"Материал №{i}")))
            lines.append(f"<b>{i}.</b> [{t_type_safe}] {title_safe}")

    filter_rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if selected_cat == 'all' else ''}Все ({cat_counts.get('all', 0)})",
                callback_data=f"subj:open:{subj_idx}:all:0",
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if selected_cat == 'lecture' else ''}📖 Лекции ({cat_counts.get('lecture', 0)})",
                callback_data=f"subj:open:{subj_idx}:lecture:0",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if selected_cat == 'practical' else ''}🛠 Практика ({cat_counts.get('practical', 0)})",
                callback_data=f"subj:open:{subj_idx}:practical:0",
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if selected_cat == 'lab' else ''}🔬 Лаб. ({cat_counts.get('lab', 0)})",
                callback_data=f"subj:open:{subj_idx}:lab:0",
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if selected_cat == 'independent' else ''}📝 СРС ({cat_counts.get('independent', 0)})",
                callback_data=f"subj:open:{subj_idx}:independent:0",
            ),
        ],
    ]

    file_rows = []
    for r in page_items:
        global_idx = all_res.index(r)
        short_title = r["title"][:34]
        file_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📥 {short_title}",
                    callback_data=f"res:dl:{subj_idx}:{global_idx}:{selected_cat}:{page}",
                )
            ]
        )

    nav_row = []
    if total_pages > 1:
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ Пред.",
                    callback_data=f"subj:open:{subj_idx}:{selected_cat}:{page - 1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"Стр. {page + 1}/{total_pages}",
                callback_data="noop",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="След. ➡️",
                    callback_data=f"subj:open:{subj_idx}:{selected_cat}:{page + 1}",
                )
            )

    bottom_rows = []
    if nav_row:
        bottom_rows.append(nav_row)
    bottom_rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить материалы",
                callback_data=f"subj:open:{subj_idx}:{selected_cat}:0:refresh",
            )
        ]
    )
    bottom_rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад к предметам", callback_data="nav:subjects"
            ),
            InlineKeyboardButton(
                text="🏠 Главное меню", callback_data="nav:home"
            ),
        ]
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[*filter_rows, *file_rows, *bottom_rows]
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text="\n".join(lines),
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("res:dl:"))
async def cb_download_subject_resource(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    file_storage: FileStorageService,
) -> None:
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    sem_code = hemis_client.get_user_semester_code(user_id)
    subj_cache_key = f"{user_id}_{sem_code}"

    parts = callback.data.split(":")
    subj_idx = int(parts[2])
    res_idx = int(parts[3])
    selected_cat = parts[4] if len(parts) > 4 else "all"
    page = int(parts[5]) if len(parts) > 5 else 0

    subjects = _SUBJECTS_CACHE.get(
        subj_cache_key
    ) or await hemis_client.get_semester_subjects(user_id=user_id)
    _SUBJECTS_CACHE[subj_cache_key] = subjects
    if subj_idx >= len(subjects):
        return await cb_subjects_resources(callback, state, bot, hemis_client)

    subj = subjects[subj_idx]
    res_cache_key = f"{user_id}_{sem_code}_{subj['id']}"
    all_res = _SUBJECT_RESOURCES_CACHE.get(
        res_cache_key
    ) or await hemis_client.get_subject_resources(
        subject_id=str(subj["id"]), subject_name=subj["name"], user_id=user_id
    )
    _SUBJECT_RESOURCES_CACHE[res_cache_key] = all_res

    if res_idx >= len(all_res):
        await callback.answer("Файл не найден, обновите список.", show_alert=True)
        return

    res = all_res[res_idx]
    await callback.answer("Отправляю учебный файл...")

    return_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Разобрать этот материал с ИИ-тьютором",
                    callback_data=f"res:ai:{subj_idx}:{res_idx}:{selected_cat}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⬅️ Назад к ресурсам «{subj['name'][:20]}»",
                    callback_data=f"subj:open:{subj_idx}:{selected_cat}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Все предметы", callback_data="nav:subjects"
                ),
                InlineKeyboardButton(text="🏠 Меню", callback_data="nav:home"),
            ],
        ]
    )

    subj_safe = html.escape(str(subj.get("name", "Дисциплина")))
    type_safe = html.escape(str(res.get("training_type", "Материал")))
    title_safe = html.escape(str(res.get("title", "Документ")))

    try:
        sent_doc = await file_storage.send_or_cache_file(
            bot=bot,
            chat_id=callback.message.chat.id,
            source_url=res["url"],
            file_name=res.get("file_name") or f"{res['title']}.pdf",
            caption=(
                f"📘 <b>{subj_safe}</b>\n"
                f"🗂 Тип занятия: {type_safe}\n"
                f"📎 Материал: <b>{title_safe}</b>"
            ),
            reply_markup=return_kb,
        )
        if sent_doc:
            await try_delete_message(callback.message)
            register_active_message(
                callback.message.chat.id, sent_doc.message_id
            )
    except Exception as exc:
        await render_clean_screen(
            bot=bot,
            chat_id=callback.message.chat.id,
            text=(
                f"⚠️ Не удалось скачать файл из HEMIS ({html.escape(str(exc))}).\n"
                f"🔗 Ссылка на материал: {html.escape(str(res.get('url', '')))}"
            ),
            reply_markup=return_kb,
            prefer_message=callback.message,
        )


@router.callback_query(F.data.startswith("res:ai:"))
async def cb_discuss_resource_with_ai(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    hemis_client: HemisClient,
    file_storage: FileStorageService,
    gemini_service: GeminiTutorService,
) -> None:
    await callback.answer("Запускаю ИИ-разбор учебного материала...")
    user_id = callback.from_user.id
    await hemis_client.load_user_semester_from_db(user_id)
    await gemini_service.load_user_prefs(user_id)
    ai_summary = gemini_service.get_user_ai_summary(user_id)

    sem_code = hemis_client.get_user_semester_code(user_id)
    subj_cache_key = f"{user_id}_{sem_code}"

    parts = callback.data.split(":")
    subj_idx = int(parts[2])
    res_idx = int(parts[3])
    selected_cat = parts[4] if len(parts) > 4 else "all"
    page = int(parts[5]) if len(parts) > 5 else 0

    subjects = _SUBJECTS_CACHE.get(
        subj_cache_key
    ) or await hemis_client.get_semester_subjects(user_id=user_id)
    _SUBJECTS_CACHE[subj_cache_key] = subjects
    if subj_idx >= len(subjects):
        return await cb_subjects_resources(callback, state, bot, hemis_client)

    subj = subjects[subj_idx]
    res_cache_key = f"{user_id}_{sem_code}_{subj['id']}"
    all_res = _SUBJECT_RESOURCES_CACHE.get(
        res_cache_key
    ) or await hemis_client.get_subject_resources(
        subject_id=str(subj["id"]), subject_name=subj["name"], user_id=user_id
    )
    _SUBJECT_RESOURCES_CACHE[res_cache_key] = all_res
    if res_idx >= len(all_res):
        return await cb_subjects_resources(callback, state, bot, hemis_client)

    res = all_res[res_idx]
    subj_safe = html.escape(str(subj.get("name", "Дисциплина")))
    title_safe = html.escape(str(res.get("title", "Материал")))
    badge_safe = html.escape(ai_summary["badge"])

    loading_screen = await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            f"🤖 <b>Модель:</b> <code>{badge_safe}</code>\n\n"
            f"⏳ <i>ИИ-тьютор читает материал «{title_safe}» по предмету «{subj_safe}» и готовит конспект...</i>"
        ),
        prefer_message=callback.message,
    )

    file_bytes = None
    file_name = res.get("file_name") or f"{res['title']}.pdf"
    try:
        file_bytes, file_name = await file_storage.get_or_download_bytes(
            source_url=res["url"],
            file_name=file_name,
        )
    except Exception:
        file_bytes = None

    extracted_doc_text = ""
    if file_bytes and file_name:
        extracted_doc_text = gemini_service.extract_text_from_file_bytes(
            file_bytes, file_name
        )

    context_str = (
        f"Дисциплина: {subj['name']}\n"
        f"Тип материала: {res.get('training_type', 'Учебный материал')}\n"
        f"Название темы/файла: {res['title']}"
    )
    if extracted_doc_text:
        context_str += (
            f"\n\nСодержимое файла ({file_name}):\n{extracted_doc_text[:8000]}"
        )

    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=context_str, return_task_idx=None)

    explanation = await gemini_service.ask_tutor(
        user_id=user_id,
        question=(
            "Составь структурированный учебный конспект этого материала на русском языке: "
            "выдели ключевые определения, основные идеи, формулы/алгоритмы и вопросы для самопроверки."
        ),
        assignment_context=context_str,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💾 Сохранить конспект в заметки",
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
                    text=f"⬅️ Назад к ресурсам «{subj['name'][:18]}»",
                    callback_data=f"subj:open:{subj_idx}:{selected_cat}:{page}",
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
            f"📘 <b>ИИ-конспект материала: {subj_safe}</b>\n"
            f"📎 <i>{title_safe}</i>\n"
            f"🤖 Модель: <code>{badge_safe}</code>\n\n"
            f"{explanation}\n\n"
            "💬 <i>Вы можете задать любой вопрос по этой теме прямо сейчас в чате!</i>"
        ),
        reply_markup=kb,
        prefer_message=loading_screen,
    )
