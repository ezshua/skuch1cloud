import os
import logging
from pathlib import Path

from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def _ensure_env_populated():
    """
    Проверяет наличие переменных в .env и добавляет недостающие из .env.defaults.
    """
    env_path = Path(".env")
    defaults_path = Path(".env.defaults")

    # Если файла с дефолтами нет, мы не можем ничего проверить
    if not defaults_path.exists():
        logger.warning(f"Файл значений по умолчанию {defaults_path} не найден.")
        return

    # Создаем .env, если он отсутствует
    if not env_path.exists():
        env_path.touch()
        logger.info("Создан новый файл .env")

    # Читаем текущие ключи из .env
    env_lines = env_path.read_text(encoding="utf-8").splitlines()
    existing_keys = set()
    for line in env_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            existing_keys.add(key)

    # Читаем эталонные значения
    default_lines = defaults_path.read_text(encoding="utf-8").splitlines()

    entries_to_add = []
    current_comments = []

    for line in default_lines:
        stripped = line.strip()

        if not stripped:
            # Пустая строка сбрасывает накопленные комментарии.
            # Это работает и для отступа перед переменной, и для разрывов между комментариями.
            current_comments = []
            continue

        if stripped.startswith("#"):
            current_comments.append(line)
        elif "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key not in existing_keys:
                entries_to_add.extend(current_comments)
                entries_to_add.append(line)
            current_comments = []
        else:
            # Любая другая строка (не коммент и не переменная) также прерывает контекст
            current_comments = []

    # Добавляем недостающие переменные по одной
    if entries_to_add:
        with env_path.open("a", encoding="utf-8") as f:
            # Обеспечиваем отступ от текущего содержимого
            if env_path.stat().st_size > 0:
                f.write("\n\n")

            for entry in entries_to_add:
                f.write(f"{entry}\n")
                # Логируем только добавление самой переменной, а не комментариев
                if "=" in entry and not entry.strip().startswith("#"):
                    logger.info(f"В .env добавлена переменная: {entry.split('=', 1)[0].strip()}")

# Инициализируем окружение перед загрузкой
_ensure_env_populated()
load_dotenv()


def _get_int_env(name: str, default: int) -> int:
    """Получить положительную целочисленную переменную окружения."""
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer.") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")
    return value


# Константы (загружаются из .env)
MAX_FILE_SIZE = _get_int_env("MAX_FILE_SIZE", 20 * 1024 * 1024) # Лимит Telegram на скачивание файлов для ботов (по умолчанию 20 МБ)
BOT_TOTAL_DATA_LIMIT = _get_int_env("BOT_DATA_PATH_SIZE", 500 * 1024 * 1024) # Общий лимит для всех пользователей (по умолчанию 500 МБ)
LOG_FILE_SIZE_LIMIT = _get_int_env("LOG_FILE_SIZE_LIMIT", 5 * 1024 * 1024) # Лимит на размер журнала действий (по умолчанию 5 МБ)
MAX_DISPLAY_NAME_LEN = 37 # Максимальная длина имени файла для отображения в интерфейсе
_admin_raw = os.getenv("ADMIN_ACCOUNT_ID")
try:
    ADMIN_ID: int | None = int(_admin_raw) if _admin_raw else None # ID администратора для уведомлений
except ValueError:
    raise RuntimeError("ADMIN_ACCOUNT_ID must be an integer.") from None
if ADMIN_ID is not None and ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ACCOUNT_ID must be greater than zero.")
# Если вы хотите лимит на пользователя, это будет сложнее, так как директории пользователей могут быть разных размеров.
# Для простоты пока общий лимит.

# Иконки для типов файлов (централизованное управление)
FILE_ICONS = {
    "image": "🖼️",
    "video": "🎬",
    "audio": "🎧",
    "archive": "📦",
    "document": "📄",
    "folder": "📁"  # Общая иконка или папка
}


def get_bot_token() -> str:
    """Получить токен бота из переменной окружения."""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing BOT_TOKEN env var. Put it into a .env file.")
    return token


def get_base_path() -> Path:
    """Получить базовый путь для хранения данных бота."""
    return Path(os.getenv("BOT_DATA_PATH", "data")).resolve()
