"""Environment configuration. All secrets come from env vars (Koyeb / .env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

WATCHLIST = ["R_10", "R_50", "R_75", "1HZ150V", "JD10", "JD75", "JD100"]

SYMBOL_LABELS = {
    "R_10": "Volatility 10",
    "R_50": "Volatility 50",
    "R_75": "Volatility 75",
    "1HZ150V": "Volatility 150 (1s)",
    "JD10": "Jump 10",
    "JD75": "Jump 75",
    "JD100": "Jump 100",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str = ""
    supabase_secret_key: str = ""
    supabase_publishable_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""

    deriv_app_id: str = ""

    encryption_key: str = ""

    app_env: str = "dev"
    dashboard_password: str = ""

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
