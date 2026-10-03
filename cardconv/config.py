"""Application settings, read from environment variables or a local .env file.

The API key itself is never stored on the Settings object; the Anthropic SDK reads
ANTHROPIC_API_KEY from the environment directly. We only check whether it is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "high"  # "" disables the effort parameter (for models that lack it)
    refusal_fallback: bool = True
    max_image_edge: int = 2048
    api_max_attempts: int = 3
    api_timeout_seconds: int = 120
    export_mode: str = "review"  # "review" or "auto"
    vcard_outlook_charset: bool = True
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    export_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "exports")
    watch_stable_seconds: int = 3
    watch_pair_wait_seconds: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cards.db"

    @property
    def image_store(self) -> Path:
        return self.data_dir / "images"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.image_store, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)


def api_key_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings. Values in the real environment win over the .env file."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    effort = os.getenv("CARDCONV_EFFORT", "high").strip().lower()
    if effort not in EFFORT_LEVELS:
        effort = ""
    mode = os.getenv("CARDCONV_EXPORT_MODE", "review").strip().lower()
    return Settings(
        model=os.getenv("CARDCONV_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5",
        effort=effort,
        refusal_fallback=_env_bool("CARDCONV_REFUSAL_FALLBACK", True),
        max_image_edge=max(800, _env_int("CARDCONV_MAX_IMAGE_EDGE", 2048)),
        api_max_attempts=max(1, min(6, _env_int("CARDCONV_API_MAX_ATTEMPTS", 3))),
        api_timeout_seconds=max(10, _env_int("CARDCONV_API_TIMEOUT", 120)),
        export_mode=mode if mode in ("review", "auto") else "review",
        vcard_outlook_charset=_env_bool("CARDCONV_VCARD_OUTLOOK_CHARSET", True),
        data_dir=_env_path("CARDCONV_DATA_DIR", PROJECT_ROOT / "data"),
        export_dir=_env_path("CARDCONV_EXPORT_DIR", PROJECT_ROOT / "exports"),
        watch_stable_seconds=max(1, _env_int("CARDCONV_WATCH_STABLE_SECONDS", 3)),
        watch_pair_wait_seconds=max(0, _env_int("CARDCONV_WATCH_PAIR_WAIT_SECONDS", 30)),
    )
