import asyncio
import threading
from contextlib import asynccontextmanager
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from config import LOG_FILE_SIZE_LIMIT


_file_data_locks: dict[Path, asyncio.Lock] = {}
_file_data_locks_guard = asyncio.Lock()
_sync_file_locks: dict[Path, threading.Lock] = {}
_sync_file_locks_guard = threading.Lock()


async def _get_file_data_lock(files_data_path: Path) -> asyncio.Lock:
    """Вернуть общий lock для конкретного files_data.json в рамках процесса."""
    key = files_data_path.resolve()
    async with _file_data_locks_guard:
        lock = _file_data_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _file_data_locks[key] = lock
        return lock


@asynccontextmanager
async def locked_file_data(files_data_path: Path):
    """Сериализовать read-modify-write операции с индексом файлов пользователя."""
    lock = await _get_file_data_lock(files_data_path)
    async with lock:
        yield


def _get_sync_file_lock(path: Path) -> threading.Lock:
    """Вернуть синхронный lock для read-modify-write операций с JSON-файлом."""
    key = path.resolve()
    with _sync_file_locks_guard:
        lock = _sync_file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _sync_file_locks[key] = lock
        return lock


def slugify_cyrillic_to_ascii(s: str) -> str:
    """Конвертировать кириллицу в латиницу."""
    mapping = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    res: list[str] = []
    for ch in s:
        low = ch.lower()
        if low in mapping:
            mapped = mapping[low]
            res.append(mapped.upper() if ch.isupper() else mapped)
        else:
            res.append(ch)
    return "".join(res)


def normalize_filename(file_name: str) -> str:
    """Нормализовать имя файла для безопасного сохранения."""
    file_name = file_name.strip().replace("\\", "/").split("/")[-1] or "file.bin"
    file_name = slugify_cyrillic_to_ascii(file_name)

    stem, ext = file_name, ""
    if "." in file_name:
        stem, ext = file_name.rsplit(".", 1)
        ext = ext.lower()

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._-")[:120] or "file"
    return f"{stem.lower()}.{ext}" if ext else stem.lower()

def shorten_name(name: str, max_len: int, existing_names: list[str] = None) -> str:
    """
    Сокращает имя файла посередине, если оно превышает max_len или уже существует.
    Пример: "ОченьДлинноеИмяФайла_2024.jpg" -> "ОченьДлин....2024.jpg"
    Если имя занято: "ОченьДлин..1..2024.jpg"
    """
    def _compose(stem_part: str, ext_part: str, limit: int, counter: int = None) -> str:
        # Выбираем разделитель: либо 4 точки, либо цифра в точках для уникальности
        sep = f"..{counter}.." if counter is not None else "...."
        room = limit - len(ext_part) - len(sep)

        if room <= 4:
            # Если места критически мало, просто обрезаем начало и ставим многоточие
            dots = "..."
            return (stem_part + ext_part)[:limit - len(dots)] + dots

        # Распределяем оставшееся место между началом и концом имени
        left_len = room // 2 + (room % 2)
        right_len = room // 2
        return stem_part[:left_len] + sep + stem_part[-right_len:] + ext_part

    # Если имя короткое и проверка на уникальность не требуется или пройдена
    if len(name) <= max_len and (not existing_names or name not in existing_names):
        return name

    # Выделяем расширение
    if "." in name and not name.startswith("."):
        stem, ext = name.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = name, ""

    # 1. Пытаемся просто сократить, если имя слишком длинное
    candidate = name
    if len(name) > max_len:
        candidate = _compose(stem, ext, max_len)

    # 2. Если имя (сокращенное или исходное) конфликтует, ищем свободный номер
    if existing_names and candidate in existing_names:
        counter = 1
        while True:
            candidate = _compose(stem, ext, max_len, counter)
            if candidate not in existing_names or counter > 999:
                break
            counter += 1

    return candidate

def unique_path(path: Path) -> Path:
    """Найти уникальный путь, добавив суффикс если файл существует."""
    if not path.exists():
        return path
    i = 1
    while (path.parent / f"{path.stem}_{i}{path.suffix}").exists():
        i += 1
    return path.parent / f"{path.stem}_{i}{path.suffix}"


def atomic_write_text(path: Path, text: str) -> None:
    """Атомарная запись текста в файл (через временный файл)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def get_dir_size(path: Path) -> int:
    """Рекурсивно вычисляет размер директории в байтах."""
    total_size = 0
    if path.is_dir():
        for entry in path.iterdir():
            if entry.is_file():
                total_size += entry.stat().st_size
            elif entry.is_dir():
                total_size += get_dir_size(entry) # Рекурсивный вызов
    return total_size


def load_json_safe(path: Path) -> dict:
    """Безопасно загрузить JSON файл (как словарь)."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def load_json_list_safe(path: Path) -> list:
    """Безопасно загрузить JSON файл (как список)."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []



def format_size(size_bytes: int) -> str:
    """Форматирует размер файла в удобочитаемый вид (Б, КБ, МБ, ГБ)."""
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.2f} КБ"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.2f} МБ"
    else:
        return f"{size_bytes / (1024 ** 3):.2f} ГБ"
def append_file_data(files_data_path: Path, file_info: dict) -> None:
    """
    Добавить запись о файле в JSON-файл данных о файлах.

    Args:
        files_data_path (Path): Путь к файлу files_data.json.
        file_info (dict): Словарь с информацией о файле (original_name, stored_name, upload_date, size).
    """
    data = load_json_list_safe(files_data_path)
    data.append(file_info)
    atomic_write_text(files_data_path, json.dumps(data, ensure_ascii=False, indent=2))


def log_user_action(user_dir: Path, action_type: str, details: dict) -> None:
    """
    Записать действие в журнал пользователя с контролем размера файла.
    Если размер файла превышает лимит, удаляет около 20% старых записей.
    """
    log_path = user_dir / "action_log.json"
    with _get_sync_file_lock(log_path):
        data = load_json_list_safe(log_path)

        # Проверка размера и ротация: оставляем последние 80% записей.
        if log_path.exists() and log_path.stat().st_size > LOG_FILE_SIZE_LIMIT:
            if data:
                keep_count = max(1, int(len(data) * 0.8))
                data = data[-keep_count:]

        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": action_type,
            "details": details
        }
        data.append(entry)
        atomic_write_text(log_path, json.dumps(data, ensure_ascii=False, indent=2))


async def async_log_user_action(user_dir: Path, action_type: str, details: dict) -> None:
    """Записать действие пользователя, не блокируя async event loop."""
    await asyncio.to_thread(log_user_action, user_dir, action_type, details)


def cleanup_temp_files(path: Path) -> None:
    """
    Рекурсивно удаляет временные файлы (.tmp, .download) в указанной директории.
    """
    temp_extensions = {".tmp", ".download"}
    if not path.exists():
        return
    for entry in path.rglob("*"):
        if entry.is_file() and entry.suffix in temp_extensions:
            try:
                entry.unlink()
            except Exception:
                pass


def collect_daily_report(base_path: Path) -> str:
    """
    Собирает статистику активности всех пользователей за последние 24 часа.
    """
    users_map_path = base_path / "users_map.json"
    if not users_map_path.exists():
        return ""

    mapping = load_json_safe(users_map_path)
    if not mapping:
        return ""

    report_lines = ["📊 <b>Ежедневный отчет по активности</b>"]
    total_active = 0

    now = datetime.now()
    yesterday = now - timedelta(days=1)

    for user_label, data in mapping.items():
        dir_name = data["dir"] if isinstance(data, dict) else data
        user_dir = base_path / dir_name

        # Проверяем наличие активности в логе за последние сутки
        log_path = user_dir / "action_log.json"
        actions = load_json_list_safe(log_path)

        # Очищаем tzinfo для безопасного сравнения с наивным yesterday
        recent_actions = [a for a in actions if datetime.fromisoformat(a["timestamp"]).replace(tzinfo=None) > yesterday]

        if not recent_actions:
            continue

        total_active += 1

        # Считаем только новые файлы за сутки из индекса
        files_data = load_json_list_safe(user_dir / "files_data.json")
        # Аналогично очищаем tzinfo, так как старые записи могли быть сохранены с часовым поясом
        daily_files = [
            f for f in files_data
            if datetime.fromisoformat(f["upload_date"]).replace(tzinfo=None) > yesterday
        ]

        count = len(daily_files)
        size = sum(f.get("size", 0) for f in daily_files)
        total_size = get_dir_size(user_dir)

        # Считаем только те сообщения, на которые бот ответил в режиме "эхо"
        echo_count = 0
        echo_chars = 0
        for action in recent_actions:
            details = action.get("details", {})
            if action.get("type") == "bot_response" and details.get("type") == "echo":
                echo_count += 1
                echo_chars += len(str(details.get("text", "")))

        user_row = f"👤 {user_label}: 🆕 {count} шт. | 💾 {format_size(size)} / {format_size(total_size)}"
        if echo_count > 0:
            user_row += f" | 💬 Эхо: {echo_count} ({echo_chars} симв.)"

        report_lines.append(user_row)

    if total_active == 0:
        return "📊 Активности за прошедшие сутки не зафиксировано."

    return "\n".join(report_lines)


def collect_users_summary(base_path: Path) -> str:
    """
    Собирает общую статистику по всем пользователям: кол-во файлов и физический объем папок.
    """
    users_map_path = base_path / "users_map.json"
    if not users_map_path.exists():
        return ""

    mapping = load_json_safe(users_map_path)
    if not mapping:
        return ""

    report_lines = ["👥 <b>Сводка по пользователям:</b>"]

    # Сортируем пользователей по имени/логину для удобства чтения
    for label in sorted(mapping.keys(), key=lambda s: s.lower()):
        data = mapping[label]
        dir_name = data["dir"] if isinstance(data, dict) else data
        user_dir = base_path / dir_name

        if not user_dir.exists():
            continue

        files_data = load_json_list_safe(user_dir / "files_data.json")
        count = len(files_data)
        size = get_dir_size(user_dir)

        report_lines.append(f"👤 {label}: 📁 {count} шт. | 💾 {format_size(size)}")

    return "\n".join(report_lines) if len(report_lines) > 1 else ""
