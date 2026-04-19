import os
import logging
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env один раз при импорте модуля
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы (загружаются из .env)
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 20 * 1024 * 1024)) # Лимит Telegram на скачивание файлов для ботов (по умолчанию 20 МБ)
BOT_TOTAL_DATA_LIMIT = int(os.getenv("BOT_DATA_PATH_SIZE", 500 * 1024 * 1024)) # Общий лимит для всех пользователей (по умолчанию 500 МБ)
LOG_FILE_SIZE_LIMIT = int(os.getenv("LOG_FILE_SIZE_LIMIT", 5 * 1024 * 1024)) # Лимит на размер журнала действий (по умолчанию 5 МБ)
# Если вы хотите лимит на пользователя, это будет сложнее, так как директории пользователей могут быть разных размеров.
# Для простоты пока общий лимит.


def get_bot_token() -> str:
    """Получить токен бота из переменной окружения."""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing BOT_TOKEN env var. Put it into a .env file.")
    return token


def get_base_path() -> Path:
    """Получить базовый путь для хранения данных бота."""
    return Path(os.getenv("BOT_DATA_PATH", "data")).resolve()
