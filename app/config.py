from dataclasses import dataclass
import os
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    telegram_api_id: int
    telegram_api_hash: str
    db_path: str = "bot.db"
    sessions_dir: str = "sessions"



def load_settings() -> Settings:
    missing = [
        key
        for key in ["BOT_TOKEN", "ADMIN_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"]
        if not os.getenv(key)
    ]
    if missing:
        raise RuntimeError(f"Missing env variables: {', '.join(missing)}")

    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        admin_id=int(os.environ["ADMIN_ID"]),
        telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
        telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
        db_path=os.getenv("DB_PATH", "bot.db"),
        sessions_dir=os.getenv("SESSIONS_DIR", "sessions"),
    )
