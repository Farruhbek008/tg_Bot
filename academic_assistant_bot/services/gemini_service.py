import asyncio
import html
import io
import json
import logging
import os
from pathlib import Path
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import dotenv_values
from google import genai
from google.genai import types
import pytz
from sqlalchemy import select

from config import settings
from database.db import async_session_maker
from database.models import UserSettings

logger = logging.getLogger(__name__)

ACADEMIC_SYSTEM_PROMPT = (
    "Ты — специализированный академический ИИ-тьютор студента университета. "
    "Твоя задача — помогать по учебным предметам, объяснять сложные темы простым и понятным языком, "
    "помогать в решении задач, подготовке к лабораторным, практическим, курсовым и самостоятельным работам, "
    "а также составлять чёткие структурированные конспекты. "
    "Если студент задаёт вопросы на темы, не относящиеся к учёбе (кроме проверки связи), вежливо напомни, "
    "что ты работаешь как персональный помощник по учёбе. "
    "Всегда отвечай исключительно на грамотном русском языке."
)

# Каталог доступных провайдеров и моделей для выбора пользователем
AI_PROVIDERS_CATALOG: Dict[str, Dict[str, Any]] = {
    "groq": {
        "title": "⚡ Groq Cloud (Сверхбыстрый)",
        "short_title": "⚡ Groq",
        "description": (
            "Мгновенная генерация ответов на базе аппаратных ускорителей LPU. "
            "Отлично подходит для быстрого разбора заданий, программирования и конспектов."
        ),
        "default_model": "llama-3.3-70b-versatile",
        "models": [
            {
                "id": "llama-3.3-70b-versatile",
                "name": "Llama 3.3 70B Versatile",
                "desc": "Флагманская универсальная модель (рекомендуется)",
            },
            {
                "id": "openai/gpt-oss-120b",
                "name": "GPT-OSS 120B",
                "desc": "Крупная модель для глубокого анализа и сложных задач",
            },
            {
                "id": "openai/gpt-oss-20b",
                "name": "GPT-OSS 20B",
                "desc": "Быстрая и точная модель для повседневных вопросов",
            },
            {
                "id": "deepseek-r1-distill-llama-70b",
                "name": "DeepSeek R1 Distill 70B",
                "desc": "Усиленная логика, математика и алгоритмы",
            },
            {
                "id": "qwen/qwen3-32b",
                "name": "Qwen 3 32B",
                "desc": "Отличное качество кода, SQL и технических дисциплин",
            },
            {
                "id": "llama-3.1-8b-instant",
                "name": "Llama 3.1 8B Instant",
                "desc": "Сверхбыстрые краткие ответы за доли секунды",
            },
        ],
    },
    "gemini": {
        "title": "✨ Google Gemini",
        "short_title": "✨ Gemini",
        "description": (
            "Интеллектуальные модели от Google с поддержкой большого контекста, "
            "глубоким пониманием академических документов, PDF и методичек."
        ),
        "default_model": "gemini-2.5-flash",
        "models": [
            {
                "id": "gemini-2.5-flash",
                "name": "Gemini 2.5 Flash",
                "desc": "Оптимальный баланс высокой скорости и качества (рекомендуется)",
            },
            {
                "id": "gemini-2.5-pro",
                "name": "Gemini 2.5 Pro",
                "desc": "Максимальная точность для сложных научных и курсовых работ",
            },
            {
                "id": "gemini-2.5-flash-lite",
                "name": "Gemini 2.5 Flash-Lite",
                "desc": "Лёгкая модель для быстрых справок и конспектов",
            },
            {
                "id": "gemini-2.0-flash",
                "name": "Gemini 2.0 Flash",
                "desc": "Проверенная стабильная модель Google",
            },
        ],
    },
    "g4f": {
        "title": "🆓 G4F / GPT4Free (Бесплатный)",
        "short_title": "🆓 G4F (GPT4Free)",
        "description": (
            "Бесплатный мульти-провайдер GPT4Free без необходимости указывать API-ключи. "
            "Даёт доступ к популярным мировым моделям (GPT-4o, DeepSeek R1, Claude, Qwen)."
        ),
        "default_model": "gpt-4o",
        "models": [
            {
                "id": "gpt-4o",
                "name": "GPT-4o (Free)",
                "desc": "Универсальный флагман OpenAI для любых учебных задач",
            },
            {
                "id": "gpt-4o-mini",
                "name": "GPT-4o Mini (Free)",
                "desc": "Быстрая модель для кратких объяснений и тестов",
            },
            {
                "id": "deepseek-r1",
                "name": "DeepSeek R1 (Free)",
                "desc": "Модель с пошаговым рассуждением для математики и физики",
            },
            {
                "id": "deepseek-v3",
                "name": "DeepSeek V3 (Free)",
                "desc": "Мощная модель для написания кода и разбора лекций",
            },
            {
                "id": "claude-3.7-sonnet",
                "name": "Claude 3.7 Sonnet (Free)",
                "desc": "Превосходный академический стиль, рефераты и эссе",
            },
            {
                "id": "qwen-2.5-coder-32b",
                "name": "Qwen 2.5 Coder 32B (Free)",
                "desc": "Специализированная модель для программирования и баз данных",
            },
            {
                "id": "llama-3.3-70b",
                "name": "Llama 3.3 70B (Free)",
                "desc": "Мощная открытая модель от Meta",
            },
            {
                "id": "gemini-2.5-flash",
                "name": "Gemini 2.5 Flash (Free)",
                "desc": "Бесплатный шлюз к модели Gemini без API-ключа",
            },
        ],
    },
}

# Соответствие моделей G4F для открытых бесплатных шлюзов (Pollinations OpenAI API)
G4F_GATEWAY_MODEL_MAP: Dict[str, List[str]] = {
    "gpt-4o": ["openai-large", "openai"],
    "gpt-4o-mini": ["openai", "openai-fast"],
    "deepseek-r1": ["deepseek-reasoning", "deepseek", "openai-large"],
    "deepseek-v3": ["deepseek", "openai-large", "openai"],
    "claude-3.7-sonnet": ["claude-hybridspace", "openai-large", "openai"],
    "qwen-2.5-coder-32b": ["qwen-coder", "openai-large", "openai"],
    "llama-3.3-70b": ["llama", "openai-large", "openai"],
    "gemini-2.5-flash": ["gemini", "openai-large", "openai"],
}


class GeminiTutorService:
    """
    Мульти-провайдерный сервис академического ИИ-тьютора:
    Поддерживает полноценный выбор между:
    1. ⚡ Groq (`groq`) — с выбором моделей (Llama 3.3 70B, GPT-OSS 120B/20B, DeepSeek R1, Qwen 3, Llama 3.1 8B).
    2. ✨ Google Gemini (`gemini`) — с выбором моделей (Gemini 2.5 Flash, Gemini 2.5 Pro, Flash-Lite, 2.0 Flash).
    3. 🆓 G4F / GPT4Free (`g4f`) — бесплатный доступ без ключей к GPT-4o, DeepSeek R1/V3, Claude 3.7, Qwen Coder, Llama 3.3.
    Сохраняет выбор каждого пользователя в базе данных SQLite (`user_settings`).
    """

    def __init__(self) -> None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        env_file_vars = dotenv_values(env_path) if env_path.exists() else {}

        raw_gemini_key = (
            env_file_vars.get("GEMINI_API_KEY")
            or settings.gemini_api_key
            or ""
        ).strip().strip('"').strip("'")

        raw_groq_key = (
            env_file_vars.get("GROQ_API_KEY")
            or settings.groq_api_key
            or ""
        ).strip().strip('"').strip("'")

        # Автоматическое распознавание: если в GEMINI_API_KEY лежит ключ Groq (gsk_...),
        # используем его для Groq, чтобы всё работало без ошибок конфигурации
        if not raw_groq_key and raw_gemini_key.startswith("gsk_"):
            raw_groq_key = raw_gemini_key

        self.groq_api_key: str = raw_groq_key
        self.gemini_api_key: str = (
            raw_gemini_key
            if not raw_gemini_key.startswith(("gsk_", "sk-or-"))
            else ""
        )
        self.openrouter_api_key: str = (
            raw_gemini_key if raw_gemini_key.startswith("sk-or-") else ""
        )

        # Определяем провайдера по умолчанию
        default_prov = (
            env_file_vars.get("DEFAULT_AI_PROVIDER")
            or settings.default_ai_provider
            or ""
        ).strip().lower()
        if default_prov not in AI_PROVIDERS_CATALOG:
            if self.groq_api_key:
                default_prov = "groq"
            elif self.gemini_api_key:
                default_prov = "gemini"
            else:
                default_prov = "g4f"
        self.default_provider: str = default_prov

        self.default_models: Dict[str, str] = {
            "groq": (
                env_file_vars.get("GROQ_MODEL")
                or settings.groq_model
                or AI_PROVIDERS_CATALOG["groq"]["default_model"]
            ).strip(),
            "gemini": (
                env_file_vars.get("GEMINI_MODEL")
                or settings.gemini_model
                or AI_PROVIDERS_CATALOG["gemini"]["default_model"]
            ).strip(),
            "g4f": (
                env_file_vars.get("G4F_MODEL")
                or settings.g4f_model
                or AI_PROVIDERS_CATALOG["g4f"]["default_model"]
            ).strip(),
        }

        raw_vertex_flag = env_file_vars.get("GEMINI_USE_VERTEXAI")
        if raw_vertex_flag is not None and str(raw_vertex_flag).strip() != "":
            self._force_vertex: Optional[bool] = str(
                raw_vertex_flag
            ).strip().lower() in ("1", "true", "yes")
        else:
            self._force_vertex = settings.gemini_use_vertexai

        if not self._force_vertex:
            os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)

        self.tz = pytz.timezone(settings.timezone)
        self._history: Dict[int, List[Dict[str, str]]] = {}
        self._last_answers: Dict[int, Tuple[str, str]] = {}

        # Кэш настроек пользователей: {user_id: {"provider": ..., "model": ..., "groq_key": ..., "gemini_key": ...}}
        self._user_prefs: Dict[int, Dict[str, Any]] = {}
        self._db_loaded_users: set[int] = set()

        self._ai_studio_client: Optional[genai.Client] = None
        self._vertex_client: Optional[genai.Client] = None
        self._preferred_gemini_backend: Optional[str] = None

        if self.gemini_api_key:
            try:
                self._ai_studio_client = genai.Client(
                    api_key=self.gemini_api_key, vertexai=False
                )
            except Exception as exc:
                logger.debug("AI Studio SDK client init note: %s", exc)
            try:
                self._vertex_client = genai.Client(
                    api_key=self.gemini_api_key, vertexai=True
                )
            except Exception as exc:
                logger.debug("Vertex SDK client init note: %s", exc)

        logger.info(
            "AI Tutor Service initialized: default_provider=%s | groq_key=%s | gemini_key=%s | g4f=enabled",
            self.default_provider,
            "yes" if self.groq_api_key else "no",
            "yes" if self.gemini_api_key else "no",
        )

    # -------------------------------------------------------------------------
    # УПРАВЛЕНИЕ ВЫБОРОМ ПРОВАЙДЕРА И МОДЕЛИ ПОЛЬЗОВАТЕЛЯ
    # -------------------------------------------------------------------------
    async def load_user_prefs(self, user_id: int) -> Dict[str, Any]:
        if user_id in self._user_prefs and user_id in self._db_loaded_users:
            return self._user_prefs[user_id]

        prefs = {
            "provider": self.default_provider,
            "model": self.default_models.get(
                self.default_provider, "llama-3.3-70b-versatile"
            ),
            "custom_groq_key": None,
            "custom_gemini_key": None,
        }
        if user_id > 0:
            self._db_loaded_users.add(user_id)
            try:
                async with async_session_maker() as session:
                    stmt = select(UserSettings).where(
                        UserSettings.user_id == user_id
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if row:
                        prov = (row.ai_provider or "").lower()
                        if prov in AI_PROVIDERS_CATALOG:
                            prefs["provider"] = prov
                        if row.ai_model:
                            prefs["model"] = row.ai_model
                        prefs["custom_groq_key"] = row.custom_groq_key
                        prefs["custom_gemini_key"] = row.custom_gemini_key
            except Exception as exc:
                logger.debug("Load user AI prefs note: %s", exc)

        self._user_prefs[user_id] = prefs
        return prefs

    def get_user_prefs_sync(self, user_id: int) -> Dict[str, Any]:
        if user_id in self._user_prefs:
            return self._user_prefs[user_id]
        return {
            "provider": self.default_provider,
            "model": self.default_models.get(
                self.default_provider, "llama-3.3-70b-versatile"
            ),
            "custom_groq_key": None,
            "custom_gemini_key": None,
        }

    async def set_user_provider_and_model(
        self, user_id: int, provider: str, model: Optional[str] = None
    ) -> Dict[str, Any]:
        provider = provider.lower().strip()
        if provider not in AI_PROVIDERS_CATALOG:
            provider = self.default_provider

        catalog_entry = AI_PROVIDERS_CATALOG[provider]
        valid_model_ids = [m["id"] for m in catalog_entry["models"]]
        if not model or model not in valid_model_ids:
            model = catalog_entry["default_model"]

        prefs = await self.load_user_prefs(user_id)
        prefs["provider"] = provider
        prefs["model"] = model
        self._user_prefs[user_id] = prefs

        if user_id > 0:
            try:
                async with async_session_maker() as session:
                    stmt = select(UserSettings).where(
                        UserSettings.user_id == user_id
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if row:
                        row.ai_provider = provider
                        row.ai_model = model
                    else:
                        session.add(
                            UserSettings(
                                user_id=user_id,
                                ai_provider=provider,
                                ai_model=model,
                            )
                        )
                    await session.commit()
            except Exception as exc:
                logger.debug("Save user AI prefs note: %s", exc)

        return self.get_user_ai_summary(user_id)

    async def set_user_api_key(
        self, user_id: int, provider: str, api_key: str
    ) -> None:
        prefs = await self.load_user_prefs(user_id)
        clean_key = api_key.strip()
        if provider == "groq":
            prefs["custom_groq_key"] = clean_key
        elif provider == "gemini":
            prefs["custom_gemini_key"] = clean_key

        if user_id > 0:
            try:
                async with async_session_maker() as session:
                    stmt = select(UserSettings).where(
                        UserSettings.user_id == user_id
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if not row:
                        row = UserSettings(
                            user_id=user_id,
                            ai_provider=prefs["provider"],
                            ai_model=prefs["model"],
                        )
                        session.add(row)
                    if provider == "groq":
                        row.custom_groq_key = clean_key
                    elif provider == "gemini":
                        row.custom_gemini_key = clean_key
                    await session.commit()
            except Exception as exc:
                logger.debug("Save custom API key note: %s", exc)

    def get_user_ai_summary(self, user_id: int) -> Dict[str, str]:
        prefs = self.get_user_prefs_sync(user_id)
        prov = prefs.get("provider", self.default_provider)
        if prov not in AI_PROVIDERS_CATALOG:
            prov = self.default_provider
        cat = AI_PROVIDERS_CATALOG[prov]
        model_id = prefs.get("model") or cat["default_model"]

        model_name = model_id
        model_desc = ""
        for m in cat["models"]:
            if m["id"] == model_id:
                model_name = m["name"]
                model_desc = m["desc"]
                break

        return {
            "provider": prov,
            "provider_title": cat["title"],
            "provider_short": cat["short_title"],
            "model_id": model_id,
            "model_name": model_name,
            "model_desc": model_desc,
            "badge": f"{cat['short_title']} • {model_name}",
        }

    def clear_history(self, user_id: int) -> None:
        self._history.pop(user_id, None)
        self._last_answers.pop(user_id, None)

    def get_last_qa(self, user_id: int) -> Optional[Tuple[str, str]]:
        return self._last_answers.get(user_id)

    @staticmethod
    def extract_text_from_file_bytes(file_bytes: bytes, file_name: str) -> str:
        lower_name = file_name.lower()
        try:
            if lower_name.endswith(".pdf"):
                import pypdf

                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                pages_text = []
                for idx, page in enumerate(reader.pages[:25], start=1):
                    t = (page.extract_text() or "").strip()
                    if t:
                        pages_text.append(f"[Стр. {idx}]\n{t}")
                return "\n\n".join(pages_text)[:15000]

            if lower_name.endswith(".docx"):
                import docx

                doc = docx.Document(io.BytesIO(file_bytes))
                paras = [
                    p.text.strip() for p in doc.paragraphs if p.text.strip()
                ]
                for table in doc.tables:
                    for row in table.rows:
                        row_cells = [
                            c.text.strip() for c in row.cells if c.text.strip()
                        ]
                        if row_cells:
                            paras.append(" | ".join(row_cells))
                return "\n\n".join(paras)[:15000]

            if any(
                lower_name.endswith(ext)
                for ext in (
                    ".txt",
                    ".md",
                    ".py",
                    ".sql",
                    ".cpp",
                    ".c",
                    ".java",
                    ".js",
                    ".json",
                    ".csv",
                )
            ):
                return file_bytes.decode("utf-8", errors="ignore")[:15000]
        except Exception as exc:
            logger.warning("Could not extract text from %s: %s", file_name, exc)
        return ""

    @staticmethod
    def sanitize_for_telegram_html(text: str, max_len: int = 3500) -> str:
        if not text:
            return ""

        # Убираем блоки внутренних размышлений <think>...</think> (DeepSeek R1 / Qwen / GPT-OSS)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        code_blocks: List[str] = []

        def _replace_code_block(match: re.Match) -> str:
            code_content = html.escape(match.group(2).strip())
            code_blocks.append(f"<pre><code>{code_content}</code></pre>")
            return f"@@CODEBLOCK_{len(code_blocks) - 1}@@"

        processed = re.sub(
            r"```([a-zA-Z0-9_+-]*)\n?(.*?)```",
            _replace_code_block,
            text,
            flags=re.DOTALL,
        )

        inline_codes: List[str] = []

        def _replace_inline_code(match: re.Match) -> str:
            code_content = html.escape(match.group(1))
            inline_codes.append(f"<code>{code_content}</code>")
            return f"@@INLINECODE_{len(inline_codes) - 1}@@"

        processed = re.sub(r"`([^`\n]+)`", _replace_inline_code, processed)

        escaped = html.escape(processed)
        escaped = re.sub(
            r"^(?:#){1,6}\s+(.+)$",
            r"📌 <b>\1</b>",
            escaped,
            flags=re.MULTILINE,
        )
        escaped = re.sub(
            r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL
        )
        escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped, flags=re.DOTALL)
        escaped = re.sub(
            r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<i>\1</i>", escaped
        )
        escaped = re.sub(r"^\s*[\*\-]\s+", "• ", escaped, flags=re.MULTILINE)

        for idx, code_html in enumerate(inline_codes):
            escaped = escaped.replace(f"@@INLINECODE_{idx}@@", code_html)
        for idx, block_html in enumerate(code_blocks):
            escaped = escaped.replace(f"@@CODEBLOCK_{idx}@@", block_html)

        if len(escaped) > max_len:
            truncated = escaped[:max_len]
            for tag in ("pre", "code", "b", "i"):
                open_cnt = len(re.findall(f"<{tag}>", truncated))
                close_cnt = len(re.findall(f"</{tag}>", truncated))
                if open_cnt > close_cnt:
                    truncated += f"</{tag}>" * (open_cnt - close_cnt)
            escaped = (
                truncated
                + "\n\n<i>... (ответ сокращён под лимит сообщения Telegram)</i>"
            )

        return escaped

    # -------------------------------------------------------------------------
    # ВЫЗОВЫ ПРОВАЙДЕРА 1: GROQ CLOUD
    # -------------------------------------------------------------------------
    async def _discover_groq_models(self, api_key: str) -> List[str]:
        url = "https://api.groq.com/openai/v1/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        skip_keywords = (
            "whisper",
            "orpheus",
            "guard",
            "safeguard",
            "tts",
            "audio",
            "embed",
        )
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data") or []
                        discovered = []
                        for item in items:
                            m_id = str(item.get("id") or "")
                            if m_id and not any(
                                k in m_id.lower() for k in skip_keywords
                            ):
                                discovered.append(m_id)
                        return discovered
        except Exception as exc:
            logger.debug("Groq model discovery note: %s", exc)
        return []

    async def _call_groq_provider(
        self,
        selected_model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        custom_key: Optional[str] = None,
    ) -> str:
        active_key = (custom_key or self.groq_api_key or "").strip()
        if not active_key:
            raise RuntimeError(
                "Ключ Groq API (gsk_...) не задан. Укажите его в настройках модели или переключитесь на бесплатный G4F."
            )

        candidate_models = [
            selected_model,
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "deepseek-r1-distill-llama-70b",
            "qwen/qwen3-32b",
            "llama-3.1-8b-instant",
        ]
        # Убираем дубликаты с сохранением порядка (первой всегда идет выбранная пользователем модель)
        ordered_models = list(dict.fromkeys([m for m in candidate_models if m]))
        errors: List[str] = []

        for model in ordered_models:
            try:
                ans = await self._call_openai_compatible(
                    url="https://api.groq.com/openai/v1/chat/completions",
                    api_key=active_key,
                    model=model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
                logger.info("Groq response succeeded with model=%s", model)
                return ans
            except Exception as exc:
                err_str = str(exc)
                logger.warning(
                    "Groq attempt (model=%s) note: %s", model, err_str[:200]
                )
                errors.append(f"[{model}] {err_str}")
                if "401" in err_str or "invalid_api_key" in err_str.lower():
                    break

        discovered = await self._discover_groq_models(active_key)
        for model in discovered:
            if model in ordered_models:
                continue
            try:
                ans = await self._call_openai_compatible(
                    url="https://api.groq.com/openai/v1/chat/completions",
                    api_key=active_key,
                    model=model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
                logger.info(
                    "Groq response succeeded with discovered model=%s", model
                )
                return ans
            except Exception as exc:
                errors.append(f"[{model}] {exc}")

        raise RuntimeError(" | ".join(errors))

    # -------------------------------------------------------------------------
    # ВЫЗОВЫ ПРОВАЙДЕРА 2: GOOGLE GEMINI
    # -------------------------------------------------------------------------
    async def _call_aistudio_rest(
        self,
        api_key: str,
        model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
        response_mime_type: Optional[str] = None,
    ) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload: Dict[str, Any] = {
            "contents": [
                {
                    "role": t["role"],
                    "parts": [{"text": t["text"]}],
                }
                for t in turns
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if response_mime_type:
            payload["generationConfig"]["responseMimeType"] = response_mime_type
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        timeout = aiohttp.ClientTimeout(total=35)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"AIStudio REST {resp.status}: {body_text[:500]}"
                    )
                data = json.loads(body_text)
                candidates = data.get("candidates") or []
                if candidates:
                    parts = (
                        (candidates[0].get("content") or {}).get("parts") or []
                    )
                    texts = [p.get("text", "") for p in parts if p.get("text")]
                    if texts:
                        return "".join(texts)
                raise RuntimeError("Пустой ответ от Google AI Studio REST API")

    async def _call_vertex_express_rest(
        self,
        api_key: str,
        model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
        response_mime_type: Optional[str] = None,
    ) -> str:
        url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        payload: Dict[str, Any] = {
            "contents": [
                {
                    "role": ("user" if t["role"] == "user" else "model"),
                    "parts": [{"text": t["text"]}],
                }
                for t in turns
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if response_mime_type:
            payload["generationConfig"]["responseMimeType"] = response_mime_type
        if system_instruction:
            payload["systemInstruction"] = {
                "role": "system",
                "parts": [{"text": system_instruction}],
            }

        timeout = aiohttp.ClientTimeout(total=35)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"Vertex Express REST {resp.status}: {body_text[:500]}"
                    )
                data = json.loads(body_text)
                candidates = data.get("candidates") or []
                if candidates:
                    parts = (
                        (candidates[0].get("content") or {}).get("parts") or []
                    )
                    texts = [p.get("text", "") for p in parts if p.get("text")]
                    if texts:
                        return "".join(texts)
                raise RuntimeError("Пустой ответ от Vertex Express REST API")

    async def _call_gemini_provider(
        self,
        selected_model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
        response_mime_type: Optional[str] = None,
        custom_key: Optional[str] = None,
    ) -> str:
        active_key = (custom_key or self.gemini_api_key or "").strip()

        # Если официальный ключ Gemini (AIza...) задан, обращаемся напрямую к Google Gemini API
        if active_key and not active_key.startswith(("gsk_", "sk-or-")):
            models_to_try = list(
                dict.fromkeys(
                    [
                        selected_model,
                        "gemini-2.5-flash",
                        "gemini-2.5-pro",
                        "gemini-2.5-flash-lite",
                        "gemini-2.0-flash",
                    ]
                )
            )
            errors_log: List[str] = []
            for model in models_to_try:
                try:
                    return await self._call_aistudio_rest(
                        api_key=active_key,
                        model=model,
                        turns=turns,
                        system_instruction=system_instruction,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        response_mime_type=response_mime_type,
                    )
                except Exception as exc:
                    err_s = str(exc)
                    errors_log.append(f"[AIStudio/{model}] {err_s}")
                    if "404" not in err_s and "NOT_FOUND" not in err_s:
                        try:
                            return await self._call_vertex_express_rest(
                                api_key=active_key,
                                model=model,
                                turns=turns,
                                system_instruction=system_instruction,
                                temperature=temperature,
                                max_output_tokens=max_output_tokens,
                                response_mime_type=response_mime_type,
                            )
                        except Exception as v_exc:
                            errors_log.append(f"[Vertex/{model}] {v_exc}")
                        break

        # Если ключ Gemini не задан в .env (например, указан только ключ Groq),
        # прозрачно вызываем Gemini через бесплатный шлюз G4F, чтобы выбор Gemini работал всегда!
        return await self._call_g4f_provider(
            selected_model="gemini-2.5-flash",
            turns=turns,
            system_instruction=system_instruction,
            temperature=temperature,
        )

    # -------------------------------------------------------------------------
    # ВЫЗОВЫ ПРОВАЙДЕРА 3: G4F / GPT4FREE (БЕСПЛАТНЫЙ БЕЗ КЛЮЧЕЙ)
    # -------------------------------------------------------------------------
    async def _call_g4f_library(
        self,
        selected_model: str,
        messages: List[Dict[str, str]],
    ) -> Optional[str]:
        """
        Пытается вызвать установленную библиотеку `g4f` (GPT4Free), если она установлена в окружении.
        """
        try:
            import g4f  # type: ignore

            g4f_model_map = {
                "gpt-4o": "gpt-4o",
                "gpt-4o-mini": "gpt-4o-mini",
                "deepseek-r1": "deepseek-r1",
                "deepseek-v3": "deepseek-v3",
                "claude-3.7-sonnet": "claude-3.7-sonnet",
                "qwen-2.5-coder-32b": "qwen-2.5-coder-32b",
                "llama-3.3-70b": "llama-3.3-70b",
                "gemini-2.5-flash": "gemini-2.0-flash",
            }
            target_model = g4f_model_map.get(selected_model, "gpt-4o")

            resp = await asyncio.wait_for(
                g4f.ChatCompletion.create_async(
                    model=target_model,
                    messages=messages,
                ),
                timeout=22,
            )
            if isinstance(resp, str) and len(resp.strip()) > 5:
                return resp.strip()
        except Exception as exc:
            logger.debug("g4f library direct call note: %s", exc)
        return None

    async def _call_g4f_provider(
        self,
        selected_model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        """
        Многоуровневый провайдер G4F (GPT4Free):
        1. Пробует библиотеку `g4f` (`g4f.ChatCompletion.create_async`).
        2. Пробует прямые бесплатные OpenAI-совместимые шлюзы с маппингом выбранной модели
           (GPT-4o, DeepSeek R1/V3, Claude 3.7, Qwen Coder, Llama 3.3, Gemini).
        """
        messages: List[Dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        for t in turns:
            role = "assistant" if t["role"] == "model" else "user"
            messages.append({"role": role, "content": t["text"]})

        # 1. Пробуем пакет g4f, если установлен
        lib_ans = await self._call_g4f_library(selected_model, messages)
        if lib_ans:
            logger.info(
                "G4F library response succeeded (model=%s)", selected_model
            )
            return lib_ans

        # 2. Прямые бесплатные асинхронные шлюзы G4F (работают без API-ключей)
        gateway_models = G4F_GATEWAY_MODEL_MAP.get(
            selected_model, ["openai-large", "openai"]
        )
        errors: List[str] = []

        for gw_model in gateway_models:
            try:
                ans = await self._call_openai_compatible(
                    url="https://text.pollinations.ai/openai",
                    api_key=None,
                    model=gw_model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
                if ans and len(ans.strip()) > 2:
                    logger.info(
                        "G4F HTTP gateway succeeded (requested=%s, gateway_model=%s)",
                        selected_model,
                        gw_model,
                    )
                    return ans
            except Exception as exc:
                errors.append(f"[G4F/{gw_model}] {exc}")

        raise RuntimeError(" | ".join(errors) or "Бесплатный шлюз G4F временно перегружен.")

    async def _call_openai_compatible(
        self,
        url: str,
        api_key: Optional[str],
        model: str,
        turns: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        messages: List[Dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        for t in turns:
            role = "assistant" if t["role"] == "model" else "user"
            messages.append({"role": role, "content": t["text"]})

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        safe_temp = max(0.01, temperature) if temperature <= 0 else temperature

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": safe_temp,
        }

        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"HTTP {resp.status}: {body_text[:400]}"
                    )
                try:
                    data = json.loads(body_text)
                    choices = data.get("choices") or []
                    if choices:
                        msg = choices[0].get("message") or {}
                        content = msg.get("content") or ""
                        if content:
                            return content
                except json.JSONDecodeError:
                    if body_text.strip():
                        return body_text.strip()
                raise RuntimeError("Пустой ответ от ИИ-шлюза")

    # -------------------------------------------------------------------------
    # ЕДИНЫЙ МАРШРУТИЗАТОР С УЧЁТОМ ВЫБОРА ПОЛЬЗОВАТЕЛЯ И РЕЗЕРВНЫМ ФОЛБЭКОМ
    # -------------------------------------------------------------------------
    async def _generate_with_fallback(
        self,
        turns: List[Dict[str, str]],
        user_id: int = 0,
        system_instruction: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
        response_mime_type: Optional[str] = None,
    ) -> str:
        prefs = await self.load_user_prefs(user_id)
        chosen_provider = prefs.get("provider", self.default_provider)
        chosen_model = prefs.get("model") or AI_PROVIDERS_CATALOG.get(
            chosen_provider, AI_PROVIDERS_CATALOG["groq"]
        )["default_model"]
        custom_groq_key = prefs.get("custom_groq_key")
        custom_gemini_key = prefs.get("custom_gemini_key")

        errors_log: List[str] = []

        # 1. Сначала вызываем именно тот провайдер и модель, которые выбрал пользователь
        try:
            if chosen_provider == "groq":
                return await self._call_groq_provider(
                    selected_model=chosen_model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    custom_key=custom_groq_key,
                )
            elif chosen_provider == "gemini":
                return await self._call_gemini_provider(
                    selected_model=chosen_model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    response_mime_type=response_mime_type,
                    custom_key=custom_gemini_key,
                )
            else:
                return await self._call_g4f_provider(
                    selected_model=chosen_model,
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
        except Exception as primary_exc:
            logger.warning(
                "Primary provider %s (%s) failed: %s",
                chosen_provider,
                chosen_model,
                primary_exc,
            )
            errors_log.append(f"[{chosen_provider}/{chosen_model}] {primary_exc}")

        # 2. Резервная цепочка на случай сбоя выбранного провайдера
        if chosen_provider != "groq" and (custom_groq_key or self.groq_api_key):
            try:
                return await self._call_groq_provider(
                    selected_model=self.default_models["groq"],
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    custom_key=custom_groq_key,
                )
            except Exception as exc:
                errors_log.append(f"[Groq Fallback] {exc}")

        if chosen_provider != "g4f":
            try:
                return await self._call_g4f_provider(
                    selected_model="gpt-4o",
                    turns=turns,
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
            except Exception as exc:
                errors_log.append(f"[G4F Fallback] {exc}")

        raise RuntimeError(" | ".join(errors_log))

    def _format_friendly_api_error(self, user_id: int, err_msg: str) -> str:
        summary = self.get_user_ai_summary(user_id)
        return (
            f"⚠️ <b>Не удалось получить ответ от провайдера {html.escape(summary['provider_short'])} ({html.escape(summary['model_name'])}):</b>\n\n"
            f"📋 <b>Детали:</b>\n<code>{html.escape(err_msg[:450])}</code>\n\n"
            "💡 <i>Попробуйте переключить модель или выбрать другого провайдера (Groq / Gemini / G4F) кнопкой «⚙️ Сменить ИИ-модель» ниже.</i>"
        )

    async def ask_tutor(
        self,
        user_id: int,
        question: str,
        assignment_context: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
        file_name: Optional[str] = None,
    ) -> str:
        await self.load_user_prefs(user_id)
        user_history = self._history.setdefault(user_id, [])

        extracted_file_text = ""
        if file_bytes and file_name:
            extracted_file_text = self.extract_text_from_file_bytes(
                file_bytes, file_name
            )

        context_parts = []
        if assignment_context:
            context_parts.append(
                f"Контекст учебного материала/задания из системы HEMIS:\n---\n{assignment_context}\n---"
            )
        if extracted_file_text:
            context_parts.append(
                f"Содержимое прикреплённого файла ({file_name}):\n---\n{extracted_file_text}\n---"
            )

        if context_parts:
            prompt_text = (
                "\n\n".join(context_parts)
                + f"\n\nВопрос/запрос студента:\n{question}"
            )
        else:
            prompt_text = question

        turns = [*user_history, {"role": "user", "text": prompt_text}]

        try:
            raw_answer = await self._generate_with_fallback(
                turns=turns,
                user_id=user_id,
                system_instruction=ACADEMIC_SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=2048,
            )

            user_history.append({"role": "user", "text": prompt_text})
            user_history.append({"role": "model", "text": raw_answer})
            if len(user_history) > 10:
                self._history[user_id] = user_history[-10:]

            self._last_answers[user_id] = (question, raw_answer)
            return self.sanitize_for_telegram_html(raw_answer)
        except Exception as exc:
            logger.error("AI Tutor error: %s", exc)
            return self._format_friendly_api_error(user_id, str(exc))

    async def parse_reminder_fallback(
        self, raw_text: str, user_id: int = 0
    ) -> Optional[Dict[str, str]]:
        now_str = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S (%A)")
        schema_prompt = (
            f"Текущее время (Asia/Tashkent): {now_str}.\n"
            f"Извлеки из запроса пользователя точное время напоминания в будущем и текст задачи.\n"
            f"Запрос: «{raw_text}»\n"
            "Верни строго валидный JSON без markdown-обёртки в формате:\n"
            '{"iso_datetime": "YYYY-MM-DDTHH:MM:SS", "task": "чистый текст задачи без слов напомни и времени"}'
        )

        try:
            text_resp = await self._generate_with_fallback(
                turns=[{"role": "user", "text": schema_prompt}],
                user_id=user_id,
                temperature=0.01,
                max_output_tokens=256,
                response_mime_type="application/json",
            )
            cleaned_json = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", text_resp.strip(), flags=re.I
            )
            data = json.loads(cleaned_json)
            if "iso_datetime" in data and "task" in data:
                return data
        except Exception as exc:
            logger.debug("AI reminder fallback parsing failed: %s", exc)
        return None


AITutorService = GeminiTutorService