"""Application configuration loaded from environment variables (.env).

All secrets and deployment-specific values live in the environment. Nothing
here is hardcoded so the bot can be redeployed by swapping the .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    ids: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"Invalid Telegram user id in {name}: {chunk!r}") from exc
    return ids


# Category key -> (emoji, display label). Order defines keyboard layout.
# Keys stay in English (used internally, e.g. callback_data); labels are
# what the user sees and what gets written into the Category column.
CATEGORIES: dict[str, tuple[str, str]] = {
    "food": ("🍜", "อาหาร"),
    "accommodation": ("🏨", "ที่พัก"),
    "transportation": ("🚗", "การเดินทาง"),
    "entertainment": ("🎮", "ความบันเทิง"),
    "education": ("📚", "การศึกษา"),
    "donation": ("🙏", "บริจาค"),
    "shopping": ("🛍", "ช้อปปิ้ง"),
    "bills": ("💡", "ค่าบิล"),
    "healthcare": ("🏥", "สุขภาพ"),
    "investment": ("📈", "การลงทุน"),
    "family": ("👨‍👩‍👧", "ครอบครัว"),
    "business": ("💼", "ธุรกิจ"),
    "other": ("📦", "อื่นๆ"),
}

EXPENSES_SHEET_NAME = "Expenses"
SUMMARY_SHEET_NAME = "Summary"
DRIVE_ROOT_FOLDER_NAME = "Expenses"

SHEET_HEADERS = [
    "Date",
    "Time",
    "Amount",
    "Bank",
    "Sender",
    "Receiver",
    "Reference Number",
    "Category",
    "Remark",
    "Drive URL",
    "Telegram File ID",
    "OCR Confidence",
    "User ID",  # extension of the spec, required for multi-user support (feature 9)
]


@dataclass(frozen=True)
class Config:
    bot_token: str
    google_application_credentials: str
    google_drive_folder_id: str
    spreadsheet_id: str
    google_oauth_client_secret_path: str
    google_oauth_token_path: str
    allowed_user_ids: list[int] = field(default_factory=list)
    timezone: str = "Asia/Bangkok"
    log_file: str = "expense_bot.log"
    log_level: str = "INFO"
    ocr_language_hints: list[str] = field(default_factory=lambda: ["th", "en"])
    tesseract_cmd: str | None = None
    ocr_confidence_threshold: float = 0.55
    backup_folder_id: str | None = None
    daily_backup_hour_utc: int = 18  # 01:00 Asia/Bangkok
    folder_cache_path: str = ".drive_folder_cache.json"

    @property
    def credentials_path(self) -> Path:
        return Path(self.google_application_credentials)


def load_config() -> Config:
    """Load and validate configuration from environment variables.

    Raises:
        ConfigError: if a required variable is missing or a credentials
            file path does not exist.
    """
    creds_path = _require("GOOGLE_APPLICATION_CREDENTIALS")
    if not Path(creds_path).exists():
        raise ConfigError(f"GOOGLE_APPLICATION_CREDENTIALS file not found: {creds_path}")

    oauth_client_secret_path = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_PATH", "client_secret.json")
    if not Path(oauth_client_secret_path).exists():
        raise ConfigError(
            f"GOOGLE_OAUTH_CLIENT_SECRET_PATH file not found: {oauth_client_secret_path}. "
            "Download an OAuth Client ID (Desktop app) JSON from Google Cloud Console - see README.md."
        )

    return Config(
        bot_token=_require("BOT_TOKEN"),
        google_application_credentials=creds_path,
        google_drive_folder_id=_require("GOOGLE_DRIVE_FOLDER_ID"),
        spreadsheet_id=_require("SPREADSHEET_ID"),
        google_oauth_client_secret_path=oauth_client_secret_path,
        # Not validated here - it doesn't exist yet until authorize_drive.py
        # is run for the first time; drive.py/auth.py raise a clear,
        # actionable error at the point of use if it's still missing.
        google_oauth_token_path=os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "token.json"),
        allowed_user_ids=_optional_int_list("ALLOWED_USER_IDS"),
        timezone=os.getenv("TIMEZONE", "Asia/Bangkok"),
        log_file=os.getenv("LOG_FILE", "expense_bot.log"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        tesseract_cmd=os.getenv("TESSERACT_CMD") or None,
        ocr_confidence_threshold=float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.55")),
        backup_folder_id=os.getenv("GOOGLE_DRIVE_BACKUP_FOLDER_ID") or None,
        daily_backup_hour_utc=int(os.getenv("DAILY_BACKUP_HOUR_UTC", "18")),
        folder_cache_path=os.getenv("DRIVE_FOLDER_CACHE_PATH", ".drive_folder_cache.json"),
    )
