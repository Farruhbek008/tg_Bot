import html
import json
import logging
import os
from pathlib import Path
import re
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import dotenv_values
from google import genai
from google.genai import types
import pytz

from config import settings

logger = logging.getLogger(__name__)

ACADEMIC_SYSTEM_PROMPT = (
    "Ты — специализированный академический тьютор студента университета. "
    "Твоя единственная задача — помогать по учебным предметам, объяснять сложные темы, "
    "помогать в решении задач, подготовке к лабораторным и составлении конспектов. "
    "Если студент задает вопросы на темы, не относящиеся к учебе, вежливо откажись "
    "и напомни, что ты работаешь исключительно как помощник по учебе. "
    "Пиши структурировано, понятно и без сложной HTML-разметки."
)


class GeminiTutorService:
    """
    Сервис взаимодействия с Google Gemini через SDK `google-genai`.
    Напрямую перечитывает `.env`, чтобы исключить кэширование старых переменных окружения терминала,
    и выводит точную причину ошибки API без маскировки.
    """

    def __init__(self) -> None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        env_file_vars = dotenv_values(env_path) if env_path.exists() else {}

        raw_key = (
            env_file_vars.get("GEMINI_API_KEY")
            or settings.gemini_api_key
            or ""
        )
        self.api_key = raw_key.strip().strip('"').strip("'")

        configured_model = (
            env_file_vars.get("GEMINI_MODEL")
            or settings.gemini_model
            or "gemini-2.5-flash"
        ).strip()
        if "gemini-2.0" in configured_model or "gemini-1.5" in configured_model:
            configured_model = "gemini-2.5-flash"
        self.model_name = configured_model

        self.tz = pytz.timezone(settings.timezone)
        self._history: Dict[int, List[types.Content]] = {}

        self._is_aq_key = self.api_key.startswith("AQ.")
        if not self._is_aq_key:
            # Гарантируем, что стандартный ключ AIza... идет в Google AI Studio, а не в Vertex AI
            os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
            self._client = genai.Client(api_key=self.api_key)
        else:
            self._client = genai.Client(vertexai=True, api_key=self.api_key)

        masked_key = (
            f"{self.api_key[:6]}...{self.api_key[-4:]}"
            if len(self.api_key) > 10
            else "SHORT_OR_EMPTY"
        )
        logger.info(
            "GeminiTutorService initialized: key=%s (len=%d, vertexai=%s, model=%s)",
            masked_key,
            len(self.api_key),
            self._is_aq_key,
            self.model_name,
        )

    def clear_history(self, user_id: int) -> None:
        self._history.pop(user_id, None)

    @staticmethod
    def sanitize_for_telegram_html(text: str) -> str:
        """
        Безопасно экранирует спецсимволы `<`, `>`, `&` для ParseMode.HTML в Telegram,
        превращая базовый Markdown (`**жирный**`) в `<b>жирный</b>`.
        """
        escaped = html.escape(text)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return escaped

    async def _generate_with_fallback(
        self,
        contents: list,
        config: types.GenerateContentConfig,
    ) -> str:
        models_to_try = [
            self.model_name,
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3-flash-preview",
            "gemini-flash-latest",
        ]
        unique_models = list(dict.fromkeys(models_to_try))

        first_non_404_error: Optional[Exception] = None
        last_error: Optional[Exception] = None

        for model in unique_models:
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                if response and response.text:
                    return response.text
            except Exception as exc:
                last_error = exc
                err_str = str(exc)
                logger.warning("Gemini attempt (model=%s) warning: %s", model, exc)

                if any(
                    code in err_str
                    for code in (
                        "401",
                        "403",
                        "API_KEY_INVALID",
                        "PERMISSION_DENIED",
                        "ACCESS_TOKEN_TYPE_UNSUPPORTED",
                        "RESOURCE_EXHAUSTED",
                        "429",
                        "FAILED_PRECONDITION",
                    )
                ):
                    raise exc

                if "404" not in err_str and first_non_404_error is None:
                    first_non_404_error = exc

        if first_non_404_error:
            raise first_non_404_error
        if last_error:
            raise last_error
        return "Не удалось получить ответ от модели."

    async def ask_tutor(
        self,
        user_id: int,
        question: str,
        assignment_context: Optional[str] = None,
    ) -> str:
        user_history = self._history.setdefault(user_id, [])

        prompt_text = question
        if assignment_context:
            prompt_text = (
                f"Контекст учебного задания из системы HEMIS:\n"
                f"---\n{assignment_context}\n---\n\n"
                f"Вопрос студента по этому заданию:\n{question}"
            )

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt_text)],
        )
        contents = [*user_history, user_content]

        config = types.GenerateContentConfig(
            system_instruction=ACADEMIC_SYSTEM_PROMPT,
            temperature=0.3,
            max_output_tokens=2048,
        )

        try:
            raw_answer = await self._generate_with_fallback(contents=contents, config=config)

            user_history.append(user_content)
            user_history.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=raw_answer)],
                )
            )
            if len(user_history) > 10:
                self._history[user_id] = user_history[-10:]

            return self.sanitize_for_telegram_html(raw_answer)
        except Exception as exc:
            logger.error("Gemini API fatal error: %s", exc)
            err_msg = str(exc)
            masked_key = (
                f"{self.api_key[:6]}...{self.api_key[-4:]}"
                if len(self.api_key) > 10
                else self.api_key
            )
            return (
                "⚠️ <b>Ошибка при обращении к Gemini API:</b>\n"
                f"🔑 Используемый ключ: <code>{html.escape(masked_key)}</code> (vertexai={self._is_aq_key})\n\n"
                f"📋 <b>Детали ответа Google:</b>\n<code>{html.escape(err_msg[:600])}</code>"
            )

    async def parse_reminder_fallback(self, raw_text: str) -> Optional[Dict[str, str]]:
        now_str = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S (%A)")
        schema_prompt = (
            f"Текущее время (Asia/Tashkent): {now_str}.\n"
            f"Извлеки из запроса пользователя точное время напоминания в будущем и текст задачи.\n"
            f"Запрос: «{raw_text}»\n"
            "Верни строго валидный JSON без markdown-обертки в формате:\n"
            '{"iso_datetime": "YYYY-MM-DDTHH:MM:SS", "task": "чистый текст задачи без слов напомни и времени"}'
        )

        try:
            text_resp = await self._generate_with_fallback(
                contents=[schema_prompt],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(text_resp.strip())
            if "iso_datetime" in data and "task" in data:
                return data
        except Exception as exc:
            logger.debug("Gemini reminder fallback parsing failed: %s", exc)
        return None