from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import get_bot_token, get_base_path
from handlers import build_dispatcher
from utils import cleanup_temp_files


async def run_polling() -> None:
    """Запустить бота в режиме polling."""
    bot = Bot(
        token=get_bot_token(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    # Очистка временных файлов всех пользователей при запуске бота
    cleanup_temp_files(get_base_path())

    dp = build_dispatcher()
    await dp.start_polling(bot)
