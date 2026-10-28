from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram Bot
    bot_token: str = Field(..., alias="BOT_TOKEN")

    # Gemini API
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # HEMIS Credentials (student.fstu.uz)
    hemis_base_url: str = Field(default="https://student.fstu.uz", alias="HEMIS_BASE_URL")
    hemis_login: str = Field(..., alias="HEMIS_LOGIN")
    hemis_password: str = Field(..., alias="HEMIS_PASSWORD")

    # App Settings
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
