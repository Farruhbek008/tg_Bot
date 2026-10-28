from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram Bot
    bot_token: str = Field(..., alias="BOT_TOKEN")

    # ИИ-провайдер по умолчанию: groq | gemini | g4f
    default_ai_provider: str = Field(
        default="groq", alias="DEFAULT_AI_PROVIDER"
    )

    # Groq API (ключ начинается с gsk_...)
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = Field(
        default="llama-3.3-70b-versatile", alias="GROQ_MODEL"
    )

    # Google Gemini API (ключ начинается с AIza... или AQ.Ab...)
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_use_vertexai: Optional[bool] = Field(
        default=None, alias="GEMINI_USE_VERTEXAI"
    )

    # G4F / GPT4Free (бесплатный провайдер без API-ключей)
    g4f_model: str = Field(default="gpt-4o", alias="G4F_MODEL")

    # Учётные данные HEMIS (student.fstu.uz / my.hemis.uz)
    hemis_base_url: str = Field(
        default="https://student.fstu.uz", alias="HEMIS_BASE_URL"
    )
    hemis_login: str = Field(..., alias="HEMIS_LOGIN")
    hemis_password: str = Field(..., alias="HEMIS_PASSWORD")
    hemis_timeout: int = Field(default=30, alias="HEMIS_TIMEOUT")

    # Настройки приложения
    timezone: str = Field(default="Asia/Tashkent", alias="TIMEZONE")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/bot.db", alias="DATABASE_URL"
    )
    media_dir: Path = Field(default=Path("./data/media"), alias="MEDIA_DIR")

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def ensure_dirs(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        if "sqlite" in self.database_url and "///" in self.database_url:
            db_path_str = self.database_url.split("///", 1)[1]
            db_path = Path(db_path_str)
            db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
