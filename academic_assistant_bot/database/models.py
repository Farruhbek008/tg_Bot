from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    is_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CachedFile(Base):
    __tablename__ = "cached_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hemis_file_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    local_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserNote(Base):
    __tablename__ = "user_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    subject_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ai_provider: Mapped[str] = mapped_column(
        String(32), default="groq", nullable=False
    )
    ai_model: Mapped[str] = mapped_column(
        String(128), default="llama-3.3-70b-versatile", nullable=False
    )
    semester_code: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    custom_groq_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    custom_gemini_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TranslationCache(Base):
    __tablename__ = "translation_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
