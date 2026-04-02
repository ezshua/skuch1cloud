from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import get_bot_token
from handlers import build_dispatcher


async def run_polling() -> None:
    """Запустить бота в режиме polling."""
    bot = Bot(
        token=get_bot_token(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = build_dispatcher()
    await dp.start_polling(bot)
