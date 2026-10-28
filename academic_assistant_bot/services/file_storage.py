import hashlib
import logging
from pathlib import Path
import re
from typing import Optional

import aiofiles
from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import settings
from database.db import async_session_maker
from database.models import CachedFile
from services.hemis_client import HemisClient

logger = logging.getLogger(__name__)


class FileStorageService:
    """
    Сервис локального кэширования учебных файлов из HEMIS и мгновенной отправки
    через сохраненный Telegram `file_id` с поддержкой прикрепления inline-клавиатуры.
    """

    def __init__(self, hemis_client: HemisClient) -> None:
        self.hemis_client = hemis_client
        self.media_dir: Path = settings.media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_file_key(source_url: str) -> str:
        return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁўқғҳЎҚҒҲ._-]", "_", name.strip())
        if not any(cleaned.lower().endswith(ext) for ext in [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rar"]):
            cleaned += ".pdf"
        return cleaned or "document.pdf"

    async def send_or_cache_file(
        self,
        bot: Bot,
        chat_id: int,
        source_url: str,
        file_name: str,
        caption: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Message:
        file_key = self._make_file_key(source_url)
        safe_name = self._sanitize_filename(file_name)

        async with async_session_maker() as session:
            stmt = select(CachedFile).where(CachedFile.hemis_file_key == file_key)
            result = await session.execute(stmt)
            cached: Optional[CachedFile] = result.scalar_one_or_none()

            # 1. Мгновенная отправка по telegram_file_id
            if cached and cached.telegram_file_id:
                try:
                    logger.info("Sending cached file via Telegram file_id: %s", safe_name)
                    return await bot.send_document(
                        chat_id=chat_id,
                        document=cached.telegram_file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )
                except Exception as exc:
                    logger.warning("Stored file_id expired or invalid, re-uploading: %s", exc)

            # 2. Скачивание из HEMIS в ./data/media/
            local_path = self.media_dir / f"{file_key}_{safe_name}"
            if not local_path.exists():
                logger.info("Downloading file from HEMIS: %s", source_url)
                file_bytes = await self.hemis_client.download_file_bytes(source_url)
                async with aiofiles.open(local_path, mode="wb") as f:
                    await f.write(file_bytes)

            # 3. Отправка в Telegram и сохранение file_id
            input_file = FSInputFile(path=local_path, filename=safe_name)
            sent_msg = await bot.send_document(
                chat_id=chat_id,
                document=input_file,
                caption=caption,
                reply_markup=reply_markup,
            )

            tg_file_id = sent_msg.document.file_id if sent_msg.document else None

            if cached:
                cached.local_path = str(local_path)
                cached.telegram_file_id = tg_file_id
            else:
                cached = CachedFile(
                    hemis_file_key=file_key,
                    source_url=source_url,
                    file_name=safe_name,
                    local_path=str(local_path),
                    telegram_file_id=tg_file_id,
                )
                session.add(cached)

            await session.commit()
            return sent_msg
