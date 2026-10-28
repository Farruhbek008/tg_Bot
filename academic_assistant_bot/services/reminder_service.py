import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import dateparser
from dateparser.search import search_dates
import pytz
from sqlalchemy import delete, select, update

from config import settings
from database.db import async_session_maker
from database.models import Reminder
from services.gemini_service import GeminiTutorService

logger = logging.getLogger(__name__)


class ReminderService:
    """
    Сервис умных напоминаний на базе AsyncIOScheduler (APScheduler) и SQLAlchemy.
    Сохраняет все напоминания в SQLite (WAL) и восстанавливает их при перезапуске бота.
    """

    def __init__(self, bot: Bot, gemini_service: Optional[GeminiTutorService] = None) -> None:
        self.bot = bot
        self.gemini_service = gemini_service
        self.tz = pytz.timezone(settings.timezone)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    async def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        await self.restore_pending_reminders()

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def restore_pending_reminders(self) -> None:
        now = datetime.now(self.tz)
        async with async_session_maker() as session:
            stmt = select(Reminder).where(Reminder.is_sent.is_(False))
            result = await session.execute(stmt)
            pending: List[Reminder] = list(result.scalars().all())

        restored_count = 0
        for rem in pending:
            remind_time = rem.remind_at
            if remind_time.tzinfo is None:
                remind_time = self.tz.localize(remind_time)
            else:
                remind_time = remind_time.astimezone(self.tz)

            if remind_time <= now:
                await self._send_reminder_job(rem.id)
            else:
                self._schedule_job(rem.id, remind_time)
                restored_count += 1

        logger.info("Restored %d pending reminders into AsyncIOScheduler.", restored_count)

    def _schedule_job(self, reminder_id: int, run_date: datetime) -> None:
        job_id = f"reminder_{reminder_id}"
        self.scheduler.add_job(
            self._send_reminder_job,
            trigger="date",
            run_date=run_date,
            args=[reminder_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )

    async def _send_reminder_job(self, reminder_id: int) -> None:
        async with async_session_maker() as session:
            stmt = select(Reminder).where(Reminder.id == reminder_id)
            result = await session.execute(stmt)
            rem = result.scalar_one_or_none()

            if not rem or rem.is_sent:
                return

            try:
                await self.bot.send_message(
                    chat_id=rem.chat_id,
                    text=(
                        "🔔 <b>Напоминание!</b>\n\n"
                        f"📌 Задача: <b>{rem.task_text}</b>"
                    ),
                )
            except Exception as exc:
                logger.error("Failed to send reminder %s: %s", reminder_id, exc)

            await session.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(is_sent=True)
            )
            await session.commit()

    async def parse_natural_language(self, raw_text: str) -> Optional[Tuple[datetime, str]]:
        cleaned = re.sub(
            r"^(?:пожалуйста\s*,?\s*)?(?:напомни(?:те)?(?:\s+мне)?|создай\s+напоминание)\s+",
            "",
            raw_text.strip(),
            flags=re.IGNORECASE,
        )

        normalized = re.sub(
            r"в\s+(\d{1,2})\s+вечера",
            lambda m: f"в {int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1))}:00",
            cleaned,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"в\s+(\d{1,2})\s+утра",
            lambda m: f"в {int(m.group(1)):02d}:00",
            normalized,
            flags=re.IGNORECASE,
        )

        dp_settings = {
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": settings.timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "RELATIVE_BASE": datetime.now(self.tz),
        }

        matches = search_dates(normalized, languages=["ru", "uz", "en"], settings=dp_settings)
        if matches:
            matched_substring, parsed_dt = matches[0]
            task_text = normalized.replace(matched_substring, "", 1).strip(" ,.-—:")
            task_text = re.sub(r"^(?:о\s+том\s+что\s+|что\s+|про\s+)", "", task_text, flags=re.I).strip()
            if not task_text:
                task_text = cleaned
            return parsed_dt.astimezone(self.tz), task_text

        if self.gemini_service:
            gemini_parsed = await self.gemini_service.parse_reminder_fallback(raw_text)
            if gemini_parsed:
                try:
                    dt_naive = datetime.fromisoformat(gemini_parsed["iso_datetime"])
                    dt_aware = (
                        self.tz.localize(dt_naive)
                        if dt_naive.tzinfo is None
                        else dt_naive.astimezone(self.tz)
                    )
                    return dt_aware, gemini_parsed["task"].strip()
                except Exception as exc:
                    logger.debug("Invalid datetime from Gemini fallback: %s", exc)

        return None

    async def create_reminder(
        self,
        user_id: int,
        chat_id: int,
        task_text: str,
        remind_at: datetime,
    ) -> Reminder:
        if remind_at.tzinfo is None:
            remind_at = self.tz.localize(remind_at)
        else:
            remind_at = remind_at.astimezone(self.tz)

        async with async_session_maker() as session:
            reminder = Reminder(
                user_id=user_id,
                chat_id=chat_id,
                task_text=task_text,
                remind_at=remind_at,
                is_sent=False,
            )
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)

        self._schedule_job(reminder.id, remind_at)
        return reminder

    async def delete_reminder(self, reminder_id: int, user_id: int) -> bool:
        job_id = f"reminder_{reminder_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        async with async_session_maker() as session:
            await session.execute(
                delete(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
            )
            await session.commit()
        return True

    async def list_active_reminders(self, user_id: int) -> List[Reminder]:
        async with async_session_maker() as session:
            stmt = (
                select(Reminder)
                .where(Reminder.user_id == user_id, Reminder.is_sent.is_(False))
                .order_by(Reminder.remind_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
