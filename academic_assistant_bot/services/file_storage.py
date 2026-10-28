import hashlib
import logging
from pathlib import Path
import re
from typing import Optional, Tuple

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
    через сохранённый Telegram `file_id`, а также чтения байтов файла для ИИ-тьютора.
    Автоматически определяет реальное расширение файла по сигнатуре байтов (PDF, DOCX/PPTX/ZIP, RAR).
    """

    def __init__(self, hemis_client: HemisClient) -> None:
        self.hemis_client = hemis_client
        self.media_dir: Path = settings.media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_file_key(source_url: str) -> str:
        return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _sanitize_filename(name: str, data: Optional[bytes] = None) -> str:
        cleaned = re.sub(
            r"[^a-zA-Z0-9а-яА-ЯёЁўқғҳЎҚҒҲ._ -]", "_", name.strip()
        )
        cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
        if not cleaned:
            cleaned = "Учебный_материал"

        valid_exts = (
            ".pdf",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xls",
            ".xlsx",
            ".zip",
            ".rar",
            ".7z",
            ".txt",
            ".jpg",
            ".jpeg",
            ".png",
        )
        has_ext = any(cleaned.lower().endswith(ext) for ext in valid_exts)

        # Автоопределение расширения по заголовку бинарного файла (magic bytes)
        if data and not has_ext:
            if data.startswith(b"%PDF"):
                cleaned += ".pdf"
                has_ext = True
            elif data.startswith(b"Rar!"):
                cleaned += ".rar"
                has_ext = True
            elif data.startswith(b"\x89PNG"):
                cleaned += ".png"
                has_ext = True
            elif data.startswith(b"\xff\xd8\xff"):
                cleaned += ".jpg"
                has_ext = True
            elif data.startswith(b"PK\x03\x04"):
                # OpenXML (docx / pptx / xlsx) или zip
                sample = data[:4096]
                if b"word/" in sample:
                    cleaned += ".docx"
                elif b"ppt/" in sample:
                    cleaned += ".pptx"
                elif b"xl/" in sample:
                    cleaned += ".xlsx"
                else:
                    cleaned += ".docx"
                has_ext = True
            elif data.startswith(b"\xd0\xcf\x11\xe0"):
                cleaned += ".doc"
                has_ext = True

        if not has_ext:
            cleaned += ".pdf"
        return cleaned

    @staticmethod
    def _is_valid_binary(data: bytes) -> bool:
        if not data or len(data) < 16:
            return False
        head = data[:256].lstrip().lower()
        return not (
            head.startswith(b"<!doctype html") or head.startswith(b"<html")
        )

    async def get_or_download_bytes(
        self, source_url: str, file_name: str
    ) -> Tuple[bytes, str]:
        """
        Возвращает байты учебного файла (из локального кэша или скачивая из HEMIS)
        для последующего анализа ИИ-тьютором.
        """
        file_key = self._make_file_key(source_url)
        for existing_file in self.media_dir.glob(f"{file_key}_*"):
            async with aiofiles.open(existing_file, mode="rb") as f:
                existing_bytes = await f.read()
            if self._is_valid_binary(existing_bytes):
                return existing_bytes, existing_file.name.split("_", 1)[-1]
            existing_file.unlink(missing_ok=True)

        file_bytes = await self.hemis_client.download_file_bytes(source_url)
        safe_name = self._sanitize_filename(file_name, data=file_bytes)
        local_path = self.media_dir / f"{file_key}_{safe_name}"
        async with aiofiles.open(local_path, mode="wb") as f:
            await f.write(file_bytes)
        return file_bytes, safe_name

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

        async with async_session_maker() as session:
            stmt = select(CachedFile).where(
                CachedFile.hemis_file_key == file_key
            )
            result = await session.execute(stmt)
            cached: Optional[CachedFile] = result.scalar_one_or_none()

            # 1. Мгновенная отправка по telegram_file_id
            if cached and cached.telegram_file_id:
                try:
                    logger.info(
                        "Sending cached file via Telegram file_id: %s",
                        cached.file_name,
                    )
                    return await bot.send_document(
                        chat_id=chat_id,
                        document=cached.telegram_file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )
                except Exception as exc:
                    logger.warning(
                        "Stored file_id expired or invalid, re-uploading: %s",
                        exc,
                    )

            # 2. Скачивание из HEMIS в ./data/media/
            file_bytes, safe_name = await self.get_or_download_bytes(
                source_url, file_name
            )
            local_path = self.media_dir / f"{file_key}_{safe_name}"

            # 3. Отправка в Telegram и сохранение file_id
            input_file = FSInputFile(path=local_path, filename=safe_name)
            sent_msg = await bot.send_document(
                chat_id=chat_id,
                document=input_file,
                caption=caption,
                reply_markup=reply_markup,
            )

            tg_file_id = (
                sent_msg.document.file_id if sent_msg.document else None
            )

            if cached:
                cached.file_name = safe_name
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
