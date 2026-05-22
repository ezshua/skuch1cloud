import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import get_bot_token, get_base_path, ADMIN_ID, logger
from handlers import build_dispatcher, notify_admin
from utils import cleanup_temp_files, collect_daily_report, load_json_safe


async def daily_report_task(bot: Bot):
    """Фоновая задача для отправки ежедневного отчета администратору в полночь."""
    while True:
        now = datetime.now()
        # Рассчитываем время до следующей полночи (00:00:05 для надежности смены даты)
        target_time = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        wait_seconds = (target_time - now).total_seconds()

        logger.info(f"Следующий административный отчет запланирован через {int(wait_seconds)} сек.")
        await asyncio.sleep(wait_seconds)

        try:
            if ADMIN_ID:
                # Проверяем флаг в состоянии бота, прежде чем отправлять отчет
                state = load_json_safe(get_base_path() / "bot_state.json")
                if state.get("daily_report_enabled", True):
                    report = collect_daily_report(get_base_path())
                    if report:
                        await bot.send_message(ADMIN_ID, report)
        except Exception as e:
            logger.error(f"Ошибка при отправке ежедневного отчета: {e}")


async def run_polling() -> None:
    """Запустить бота в режиме polling."""
    bot = Bot(
        token=get_bot_token(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    # Очистка временных файлов всех пользователей при запуске бота
    cleanup_temp_files(get_base_path())

    dp = build_dispatcher()

    # Запускаем фоновую задачу отчета
    asyncio.create_task(daily_report_task(bot))

    # Уведомляем администратора о запуске
    await notify_admin(bot, "🚀 Бот успешно запущен и готов к работе на сервере.")

    await dp.start_polling(bot)
