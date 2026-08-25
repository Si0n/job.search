from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Static comparison rates. Not truth — only used to derive salary_monthly_eur
# so a floor comparison can happen across currencies. Edit when badly stale.
DEFAULT_RATES: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "PLN": 0.23,
    "UAH": 0.022,
    "CHF": 1.05,
}


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    telegram_token: str
    telegram_chat_id: str
    rates: dict[str, float]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env_path: str = ".env") -> Settings:
    file_values = _read_env_file(Path(env_path))

    def get(key: str, default: str | None = None) -> str:
        value = os.environ.get(key, file_values.get(key, default))
        if value is None:
            raise RuntimeError(f"Missing required setting: {key}")
        return value

    return Settings(
        db_host=get("DB_HOST", "127.0.0.1"),
        db_port=int(get("DB_PORT", "3306")),
        db_name=get("DB_NAME", "job_search"),
        db_user=get("DB_USER"),
        db_password=get("DB_PASSWORD"),
        telegram_token=get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=get("TELEGRAM_CHAT_ID", ""),
        rates=dict(DEFAULT_RATES),
    )
