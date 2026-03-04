import asyncio

from app.bot import run_bot
from app.config import load_settings


if __name__ == "__main__":
    asyncio.run(run_bot(load_settings()))
