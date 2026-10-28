from typing import Any, Dict, List
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.ui_manager import render_clean_screen, try_delete_message
from services.file_storage import FileStorageService
from services.hemis_client import HemisClient

router = Router(name="resources_router")

_SUBJECTS_CACHE: Dict[int, List[Dict[str, Any]]] = {}
_SUBJECT_RESOURCES_CACHE: Dict[str, List[Dict[str, Any]]] = {}

CATEGORY_LABELS = {
    "all": "📚 Все материалы",
    "lecture": "📖 Лекционные (Ma'ruza)",
    "practical": "🛠 Практические (Amaliy)",
    "lab": "🔬 Лабораторные (Lab)",
    "independent": "📝 Самостоятельные (Mustaqil)",
    "other": "📎 Прочие",
}


# -----------------------------------------------------------------------------
# 1. ЭКРАН УСПЕВАЕМОСТИ, GPA И ПОСЕЩАЕМОСТИ (Как на дашборде my.hemis.uz)
# -----------------------------------------------------------------------------
async def _build_performance_view(
    user_id: int, hemis_client: HemisClient
) -> tuple[str, InlineKeyboardMarkup]:
    perf = await hemis_client.get_performance_and_attendance(user_id=user_id)
    sem_num = perf["semester_num"]
    att = perf["attendance"]
    subjects = perf["subjects"]
    _SUBJECTS_CACHE[user_id] = subjects

    lines = [
        f"📊 <b>Мониторинг курса ({sem_num}-семестр)</b>",
        f"🎓 <b>Текущий GPA:</b> <code>{perf['gpa']} / 5.00</code>\n",
        "🕒 <b>Посещаемость (Davomat):</b>",
        (
            f"• За текущий месяц: <b><code>{att['month_total']} ч.</code></b> "
            f"(неуваж. / sababsiz: <code>{att['month_unexcused']} ч.</code> | уваж.: <code>{att['month_excused']} ч.</code>)"
        ),
        (
            f"• Всего за {sem_num}-семестр: <code>{att['semester_total']} ч.</code> "
            f"(неуваж.: {att['semester_unexcused']} ч. | уваж.: {att['semester_excused']} ч.)\n"
        ),
    ]

    if subjects:
        lines.append(f"📈 <b>Дисциплины {sem_num}-семестра (O'zlashtirish):</b>")
        for idx, s in enumerate(subjects, start=1):
            lines.append(
                f"{idx}. <b>{s['name']}</b> — <code>{s.get('overall_score', 0)} / {s.get('max_score', 100)}</code> "
                f"(Ресурсов: {s.get('resources_count', 0)})"
            )
    else:
        lines.append("ℹ️ Предметы выбранного семестра пока не загружены.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Перейти к ресурсам предметов",
                    callback_data="nav:subjects",
                )
            ],
            [
                InlineKeyboardButton(text="🎓 Сменить семестр", callback_data="sem:menu"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:perf"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
    return "\n".join(lines), kb


@router.message(Command("performance"))
@router.message(F.text == "📊 Баллы и посещаемость")
async def msg_show_performance(
    message: Message, bot: Bot, hemis_client: HemisClient
) -> None:
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
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer("Обновляю сводку успеваемости и посещаемости...")
    text, kb = await _build_performance_view(callback.from_user.id, hemis_client)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


# -----------------------------------------------------------------------------
# 2. ЭКРАН ВЫБОРА ПРЕДМЕТА ДЛЯ ПРОСМОТРА РЕСУРСОВ (Fanlar resurslari)
# -----------------------------------------------------------------------------
async def _build_subjects_list_view(
    user_id: int, hemis_client: HemisClient
) -> tuple[str, InlineKeyboardMarkup]:
    sem_num = hemis_client.get_user_semester_number(user_id)
    subjects = await hemis_client.get_semester_subjects(user_id=user_id)
    _SUBJECTS_CACHE[user_id] = subjects

    lines = [
        f"📚 <b>Учебные ресурсы по дисциплинам ({sem_num}-семестр)</b>\n",
        "Выберите предмет ниже, чтобы открыть его материалы и отфильтровать их по типу (Лекционные / Практические / Лабораторные / Самостоятельные):\n",
    ]
    kb_rows = []

    for idx, s in enumerate(subjects):
        res_cnt = s.get("resources_count", 0)
        badge = f" ({res_cnt} файл.)" if res_cnt else ""
        lines.append(
            f"• <b>{s['name']}</b> — {s.get('type', 'Majburiy')} | {s.get('load', 180)} ч. | {s.get('credit', 6.0)} кред.{badge}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📘 {s['name']}{badge}",
                    callback_data=f"subj:open:{idx}:all",
                )
            ]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(text="🎓 Сменить семестр", callback_data="sem:menu"),
            InlineKeyboardButton(text="📊 Баллы и пропуски", callback_data="nav:perf"),
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("resources"))
@router.message(F.text == "📚 Ресурсы предметов")
async def msg_subjects_resources(
    message: Message, bot: Bot, hemis_client: HemisClient
) -> None:
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
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    await callback.answer("Загружаю дисциплины семестра...")
    text, kb = await _build_subjects_list_view(callback.from_user.id, hemis_client)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


# -----------------------------------------------------------------------------
# 3. ЭКРАН РЕСУРСОВ КОНКРЕТНОГО ПРЕДМЕТА С ФИЛЬТРОМ ПО ТИПУ ЗАНЯТИЙ
# -----------------------------------------------------------------------------
@router.callback_query(F.data.startswith("subj:open:"))
async def cb_open_subject_resources(
    callback: CallbackQuery, bot: Bot, hemis_client: HemisClient
) -> None:
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    subj_idx = int(parts[2])
    selected_cat = parts[3] if len(parts) > 3 else "all"
    page = int(parts[4]) if len(parts) > 4 else 0

    subjects = _SUBJECTS_CACHE.get(user_id)
    if not subjects:
        subjects = await hemis_client.get_semester_subjects(user_id=user_id)
        _SUBJECTS_CACHE[user_id] = subjects

    if subj_idx >= len(subjects):
        return await cb_subjects_resources(callback, bot, hemis_client)

    subj = subjects[subj_idx]
    cache_key = f"{user_id}_{subj['id']}"

    await callback.answer("Загружаю материалы предмета...")
    all_res = _SUBJECT_RESOURCES_CACHE.get(cache_key)
    if all_res is None:
        all_res = await hemis_client.get_subject_resources(
            subject_id=str(subj["id"]),
            subject_name=subj["name"],
            user_id=user_id,
        )
        _SUBJECT_RESOURCES_CACHE[cache_key] = all_res

    # Считаем количество ресурсов по категориям для кнопок фильтрации
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

    lines = [
        f"📘 <b>{subj['name']}</b>",
        f"🗂 Фильтр: <b>{CATEGORY_LABELS.get(selected_cat, 'Все')}</b> (Найдено: {len(filtered)} из {len(all_res)})\n",
    ]

    if not filtered:
        lines.append("ℹ️ В этой категории пока нет загруженных преподавателем файлов. Выберите другой тип занятий кнопками ниже:")
    else:
        lines.append("<i>Нажмите на нужный материал ниже, чтобы мгновенно скачать файл:</i>\n")
        for i, r in enumerate(page_items, start=page * page_size + 1):
            lines.append(f"<b>{i}.</b> [{r['training_type']}] {r['title']}")

    # Строим кнопки фильтрации типов (Лекционные / Практические / Лабораторные / Самостоятельные / Все)
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

    # Кнопки скачивания каждого файла на текущей странице
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

    # Пагинация, если файлов больше 8 (например, 36 ресурсов по предмету)
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
            InlineKeyboardButton(text="⬅️ Назад к предметам", callback_data="nav:subjects"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home"),
        ]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[*filter_rows, *file_rows, *bottom_rows])
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
    bot: Bot,
    hemis_client: HemisClient,
    file_storage: FileStorageService,
) -> None:
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    subj_idx = int(parts[2])
    res_idx = int(parts[3])
    selected_cat = parts[4] if len(parts) > 4 else "all"
    page = int(parts[5]) if len(parts) > 5 else 0

    subjects = _SUBJECTS_CACHE.get(user_id) or await hemis_client.get_semester_subjects(user_id=user_id)
    if subj_idx >= len(subjects):
        return await cb_subjects_resources(callback, bot, hemis_client)

    subj = subjects[subj_idx]
    cache_key = f"{user_id}_{subj['id']}"
    all_res = _SUBJECT_RESOURCES_CACHE.get(cache_key) or await hemis_client.get_subject_resources(
        subject_id=str(subj["id"]), subject_name=subj["name"], user_id=user_id
    )

    if res_idx >= len(all_res):
        await callback.answer("Файл не найден, обновите список.", show_alert=True)
        return

    res = all_res[res_idx]
    await callback.answer("Отправляю файл...")

    return_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⬅️ Назад к ресурсам «{subj['name'][:20]}»",
                    callback_data=f"subj:open:{subj_idx}:{selected_cat}:{page}",
                )
            ],
            [
                InlineKeyboardButton(text="📚 Все предметы", callback_data="nav:subjects"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="nav:home"),
            ],
        ]
    )

    try:
        await try_delete_message(callback.message)
        await file_storage.send_or_cache_file(
            bot=bot,
            chat_id=callback.message.chat.id,
            source_url=res["url"],
            file_name=res.get("file_name") or f"{res['title']}.pdf",
            caption=(
                f"📘 <b>{subj['name']}</b>\n"
                f"🗂 Тип: {res['training_type']}\n"
                f"📎 Материал: <b>{res['title']}</b>"
            ),
            reply_markup=return_kb,
        )
    except Exception as exc:
        await render_clean_screen(
            bot=bot,
            chat_id=callback.message.chat.id,
            text=f"⚠️ Не удалось скачать файл из HEMIS ({exc}). Возможно, преподаватель прикрепил внешнюю ссылку: {res['url']}",
            reply_markup=return_kb,
        )
