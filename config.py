from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


BASE_DIR = Path(__file__).resolve().parent
_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_base_url: str
    dashboard_secret: str
    dashboard_host: str
    dashboard_port: int
    data_dir: Path
    userbot_config_path: Path
    max_telegram_bytes: int = 49 * 1024 * 1024
    request_timeout_sec: float = 30.0
    download_timeout_sec: float = 45.0


def get_settings(require_bot_token: bool = False) -> Settings:
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if require_bot_token and not bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Add it to .env or export BOT_TOKEN in the environment."
        )
    api_base_url = (
        os.environ.get("API_BASE_URL", "https://video-down-api.vercel.app")
        .strip()
        .rstrip("/")
    )
    dashboard_secret = os.environ.get("DASHBOARD_SECRET", "").strip()
    if not dashboard_secret:
        raise RuntimeError("DASHBOARD_SECRET is not set. Add a private dashboard password to .env.")
    dashboard_host = os.environ.get("DASHBOARD_HOST", "0.0.0.0").strip()
    dashboard_port = int(os.environ.get("DASHBOARD_PORT", "8000").strip())
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    return Settings(
        bot_token=bot_token,
        api_base_url=api_base_url,
        dashboard_secret=dashboard_secret,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        data_dir=data_dir,
        userbot_config_path=data_dir / "userbot_config.json",
    )
