"""Environment configuration. All secrets come from env vars (Koyeb / .env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Deriv does not list a "Volatility 150 (1s)" instrument — the 1-second
# volatility indices it offers are 1HZ10V / 25V / 50V / 75V / 100V. The
# methodology PDF §8.1 names Vol 150 (1s), so that symbol is unavailable
# rather than mis-spelled; it is left out until a replacement is chosen.
WATCHLIST = ["R_10", "R_50", "R_75", "JD10", "JD75", "JD100"]

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
    def deriv_app_id_clean(self) -> str:
        """DERIV_APP_ID with shell/dashboard noise removed.

        Values pasted into a hosting dashboard often arrive wrapped in
        quotes or with stray whitespace ("12345", ' 12345 '), and Deriv
        answers the handshake with the same HTTP 401 as an unknown id — so
        normalise before use.
        """
        return self.deriv_app_id.strip().strip("\"'").strip()

    @property
    def deriv_app_id_valid(self) -> bool:
        """Usable, not necessarily numeric.

        Legacy app ids are numeric, but Deriv's current dashboard issues
        alphanumeric identifiers, so the format is NOT constrained here —
        only obviously broken values (empty, or containing whitespace from
        a bad paste) are rejected. Deriv is the authority on whether an id
        is accepted; that verdict arrives as the handshake result.
        """
        value = self.deriv_app_id_clean
        return bool(value) and not any(c.isspace() for c in value)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
