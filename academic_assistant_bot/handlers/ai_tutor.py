import html
import io
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

from handlers.ui_manager import MAIN_MENU_BUTTON_TEXTS, render_clean_screen
from services.gemini_service import AI_PROVIDERS_CATALOG, GeminiTutorService

router = Router(name="ai_tutor_router")


class TutorStates(StatesGroup):
    chatting = State()
    waiting_for_api_key = State()


def get_tutor_controls_kb(
    ai_short_badge: str = "⚡ Groq",
    return_task_idx: int | None = None,
    has_answer: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if has_answer:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💾 Сохранить ответ в заметки",
                    callback_data="notes:save_ai",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"⚙️ Сменить модель ({ai_short_badge})",
                callback_data="ai:models",
            )
        ]
    )
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
            InlineKeyboardButton(
                text="🧹 Очистить диалог", callback_data="tutor:clear"
            ),
            InlineKeyboardButton(
                text="🏠 Главное меню", callback_data="nav:home"
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_tutor_welcome_text(user_id: int, gemini_service: GeminiTutorService) -> str:
    summary = gemini_service.get_user_ai_summary(user_id)
    return (
        "🎓 <b>Режим академического ИИ-тьютора активирован!</b>\n\n"
        f"🤖 <b>Активный провайдер:</b> {html.escape(summary['provider_title'])}\n"
        f"🧠 <b>Выбранная модель:</b> <code>{html.escape(summary['model_name'])}</code>\n"
        f"ℹ️ <i>{html.escape(summary['model_desc'])}</i>\n\n"
        "• Напишите любой вопрос по учёбе (базы данных, компьютерные сети, дискретные структуры, "
        "линейная алгебра, электроника, программирование, высшая математика) или попросите разобрать тему.\n"
        "• 📎 <b>Отправьте сюда учебный файл (.pdf, .docx, .txt, .py, .sql)</b> — ИИ прочитает документ и поможет с выполнением!\n"
        "• Нажмите <b>«⚙️ Сменить модель»</b>, чтобы переключиться между <b>Groq</b>, <b>Gemini</b> и <b>G4F (GPT4Free)</b>."
    )


def _build_model_selector_view(
    user_id: int,
    gemini_service: GeminiTutorService,
    view_provider: str | None = None,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    summary = gemini_service.get_user_ai_summary(user_id)
    active_prov = summary["provider"]
    active_model = summary["model_id"]
    shown_prov = (
        view_provider
        if view_provider in AI_PROVIDERS_CATALOG
        else active_prov
    )
    cat = AI_PROVIDERS_CATALOG[shown_prov]

    lines = ["⚙️ <b>Выбор ИИ-провайдера и модели</b>\n"]
    if notice:
        lines.append(f"{notice}\n")

    lines.extend(
        [
            f"✅ <b>Сейчас активно:</b> {html.escape(summary['badge'])}",
            f"📂 <b>Просматриваемый провайдер:</b> {html.escape(cat['title'])}",
            f"<i>{html.escape(cat['description'])}</i>\n",
            "👇 <b>Выберите провайдера (вкладки сверху) и нажмите на нужную модель:</b>",
        ]
    )

    # Вкладки переключения провайдеров: Groq | Gemini | G4F
    prov_tabs = []
    for p_key, p_info in AI_PROVIDERS_CATALOG.items():
        is_tab = p_key == shown_prov
        prefix = "🔘 " if is_tab else ""
        prov_tabs.append(
            InlineKeyboardButton(
                text=f"{prefix}{p_info['short_title']}",
                callback_data=f"ai:prov:{p_key}",
            )
        )

    rows = [prov_tabs]

    # Список моделей выбранного провайдера
    for idx, m in enumerate(cat["models"]):
        is_selected = (shown_prov == active_prov) and (m["id"] == active_model)
        check = "✅ " if is_selected else "🔹 "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{check}{m['name']} — {m['desc'][:28]}",
                    callback_data=f"ai:set:{shown_prov}:{idx}",
                )
            ]
        )

    if shown_prov in ("groq", "gemini"):
        key_btn_label = (
            "🔑 Задать свой API-ключ Groq"
            if shown_prov == "groq"
            else "🔑 Задать свой API-ключ Gemini"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=key_btn_label,
                    callback_data=f"ai:key:{shown_prov}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🤖 Перейти к диалогу с ИИ", callback_data="nav:tutor"
            ),
            InlineKeyboardButton(
                text="🏠 Главное меню", callback_data="nav:home"
            ),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("model"))
@router.message(Command("models"))
@router.message(Command("ai"))
@router.message(F.text == "⚙️ Выбор ИИ-модели")
async def msg_open_model_selector(
    message: Message,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await gemini_service.load_user_prefs(user_id)
    text, kb = _build_model_selector_view(user_id, gemini_service)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "ai:models")
async def cb_open_model_selector(
    callback: CallbackQuery,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    await gemini_service.load_user_prefs(user_id)
    text, kb = _build_model_selector_view(user_id, gemini_service)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("ai:prov:"))
async def cb_switch_provider_tab(
    callback: CallbackQuery,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = callback.from_user.id
    await gemini_service.load_user_prefs(user_id)
    prov = callback.data.split(":")[2]
    if prov not in AI_PROVIDERS_CATALOG:
        prov = "groq"

    # Сразу активируем провайдера с его моделью по умолчанию, если пользователь переключил вкладку
    current = gemini_service.get_user_ai_summary(user_id)
    if current["provider"] != prov:
        default_m = AI_PROVIDERS_CATALOG[prov]["default_model"]
        new_summary = await gemini_service.set_user_provider_and_model(
            user_id, prov, default_m
        )
        notice = f"✅ <b>Активирован провайдер:</b> {html.escape(new_summary['badge'])}"
        await callback.answer(f"Выбран {new_summary['provider_short']}!")
    else:
        notice = ""
        await callback.answer()

    text, kb = _build_model_selector_view(
        user_id, gemini_service, view_provider=prov, notice=notice
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("ai:set:"))
async def cb_select_specific_model(
    callback: CallbackQuery,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    prov = parts[2]
    model_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    if prov not in AI_PROVIDERS_CATALOG:
        prov = "groq"
    models_list = AI_PROVIDERS_CATALOG[prov]["models"]
    if model_idx >= len(models_list):
        model_idx = 0
    chosen_model_id = models_list[model_idx]["id"]

    new_summary = await gemini_service.set_user_provider_and_model(
        user_id=user_id, provider=prov, model=chosen_model_id
    )
    await callback.answer(f"✅ Выбрана модель: {new_summary['model_name']}")

    notice = (
        f"✅ <b>Успешно установлена модель:</b> <code>{html.escape(new_summary['model_name'])}</code> "
        f"({html.escape(new_summary['provider_short'])})"
    )
    text, kb = _build_model_selector_view(
        user_id, gemini_service, view_provider=prov, notice=notice
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.callback_query(F.data.startswith("ai:key:"))
async def cb_prompt_custom_api_key(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    prov = callback.data.split(":")[2]
    await callback.answer()
    await state.set_state(TutorStates.waiting_for_api_key)
    await state.update_data(key_provider=prov)

    prov_label = "Groq (ключ gsk_...)" if prov == "groq" else "Google Gemini (ключ AIza...)"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена (назад к моделям)", callback_data=f"ai:prov:{prov}"
                )
            ]
        ]
    )
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            f"🔑 <b>Настройка персонального API-ключа для {prov_label}</b>\n\n"
            "Отправьте ваш API-ключ ответным сообщением в чат (сообщение с ключом будет сразу удалено из чата для безопасности и сохранено в базе).\n\n"
            "<i>Примечание: для работы бесплатного режима G4F (GPT4Free) или при наличии ключа в .env вводить ключ не требуется.</i>"
        ),
        reply_markup=kb,
        prefer_message=callback.message,
    )


@router.message(
    TutorStates.waiting_for_api_key,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTON_TEXTS),
)
async def msg_save_custom_api_key(
    message: Message,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    data = await state.get_data()
    prov = data.get("key_provider", "groq")
    raw_key = (message.text or "").strip()

    await gemini_service.set_user_api_key(user_id, prov, raw_key)
    await state.set_state(TutorStates.chatting)

    text, kb = _build_model_selector_view(
        user_id,
        gemini_service,
        view_provider=prov,
        notice=f"✅ <b>Персональный API-ключ для {prov.upper()} успешно сохранён!</b>",
    )
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb,
        user_trigger_msg=message,
    )


@router.message(Command("tutor"))
@router.message(F.text.in_({"🤖 ИИ-тьютор", "🤖 ИИ-тьютор (Gemini)"}))
async def msg_start_tutor(
    message: Message,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await gemini_service.load_user_prefs(user_id)
    summary = gemini_service.get_user_ai_summary(user_id)

    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=_build_tutor_welcome_text(user_id, gemini_service),
        reply_markup=get_tutor_controls_kb(summary["badge"]),
        user_trigger_msg=message,
    )


@router.callback_query(F.data == "nav:tutor")
async def cb_start_tutor(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    await gemini_service.load_user_prefs(user_id)
    summary = gemini_service.get_user_ai_summary(user_id)

    await state.set_state(TutorStates.chatting)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=_build_tutor_welcome_text(user_id, gemini_service),
        reply_markup=get_tutor_controls_kb(summary["badge"]),
        prefer_message=callback.message,
    )


@router.callback_query(F.data == "tutor:clear")
async def cb_clear_tutor_context(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = callback.from_user.id
    await gemini_service.load_user_prefs(user_id)
    summary = gemini_service.get_user_ai_summary(user_id)

    gemini_service.clear_history(user_id)
    await state.update_data(assignment_context=None, return_task_idx=None)
    await callback.answer("Контекст диалога очищен!")
    await render_clean_screen(
        bot=bot,
        chat_id=callback.message.chat.id,
        text=(
            "🧹 <b>История диалога сброшена!</b>\n"
            f"🤖 <b>Активная модель:</b> <code>{html.escape(summary['badge'])}</code>\n\n"
            "Задайте новый вопрос или отправьте учебный файл (.pdf / .docx):"
        ),
        reply_markup=get_tutor_controls_kb(summary["badge"]),
        prefer_message=callback.message,
    )


@router.message(TutorStates.chatting, F.document)
async def handle_tutor_document(
    message: Message,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await gemini_service.load_user_prefs(user_id)
    summary = gemini_service.get_user_ai_summary(user_id)

    doc = message.document
    file_name = doc.file_name or "документ.pdf"
    user_question = (
        message.caption
        or f"Проанализируй прикреплённый учебный файл «{file_name}», объясни основные положения и составь подробный план решения заданий из него."
    )
    data = await state.get_data()
    assignment_context = data.get("assignment_context")
    return_task_idx = data.get("return_task_idx")

    safe_fname = html.escape(file_name)
    safe_q = html.escape(user_question)
    safe_badge = html.escape(summary["badge"])

    thinking_screen = await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=(
            f"🤖 <b>Модель:</b> <code>{safe_badge}</code>\n"
            f"📎 <b>Файл:</b> <code>{safe_fname}</code>\n"
            f"💬 <b>Запрос:</b> <i>{safe_q}</i>\n\n"
            "⏳ <i>ИИ-тьютор читает документ и готовит подробный разбор...</i>"
        ),
        reply_markup=get_tutor_controls_kb(summary["badge"], return_task_idx),
        user_trigger_msg=message,
    )

    file_bytes = None
    try:
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        file_bytes = buf.getvalue()
    except Exception:
        file_bytes = None

    answer = await gemini_service.ask_tutor(
        user_id=user_id,
        question=user_question,
        assignment_context=assignment_context,
        file_bytes=file_bytes,
        file_name=file_name,
    )

    await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=(
            f"🤖 <b>Модель:</b> <code>{safe_badge}</code>\n"
            f"📎 <b>Файл:</b> <code>{safe_fname}</code>\n"
            f"💬 <b>Запрос:</b> <i>{safe_q}</i>\n\n"
            f"🎓 <b>Ответ ИИ-тьютора:</b>\n{answer}"
        ),
        reply_markup=get_tutor_controls_kb(
            summary["badge"], return_task_idx, has_answer=True
        ),
        prefer_message=thinking_screen,
    )


@router.message(
    TutorStates.chatting,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTON_TEXTS),
    ~F.text.lower().startswith("напомни"),
)
async def handle_tutor_question(
    message: Message,
    state: FSMContext,
    bot: Bot,
    gemini_service: GeminiTutorService,
) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await gemini_service.load_user_prefs(user_id)
    summary = gemini_service.get_user_ai_summary(user_id)

    user_question = message.text or ""
    data = await state.get_data()
    assignment_context = data.get("assignment_context")
    return_task_idx = data.get("return_task_idx")

    safe_q = html.escape(user_question)
    safe_badge = html.escape(summary["badge"])

    thinking_screen = await render_clean_screen(
        bot=bot,
        chat_id=message.chat.id,
        text=(
            f"🤖 <b>Модель:</b> <code>{safe_badge}</code>\n"
            f"💬 <b>Ваш вопрос:</b> <i>{safe_q}</i>\n\n"
            "⏳ <i>ИИ-тьютор готовит ответ...</i>"
        ),
        reply_markup=get_tutor_controls_kb(summary["badge"], return_task_idx),
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
            f"🤖 <b>Модель:</b> <code>{safe_badge}</code>\n"
            f"💬 <b>Вопрос:</b> <i>{safe_q}</i>\n\n"
            f"🎓 <b>Ответ ИИ-тьютора:</b>\n{answer}"
        ),
        reply_markup=get_tutor_controls_kb(
            summary["badge"], return_task_idx, has_answer=True
        ),
        prefer_message=thinking_screen,
    )
