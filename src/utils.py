import json
import re
from pathlib import Path
from datetime import datetime
from config import LOG_FILE_SIZE_LIMIT


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
    Если размер файла превышает лимит, удаляет около 2% старых записей.
    """
    log_path = user_dir / "action_log.json"
    data = load_json_list_safe(log_path)

    # Проверка размера и ротация (2% старых записей)
    if log_path.exists() and log_path.stat().st_size > LOG_FILE_SIZE_LIMIT:
        if data:
            remove_count = max(1, len(data) // 50)  # 2% от общего числа записей
            data = data[remove_count:]

    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": action_type,
        "details": details
    }
    data.append(entry)
    atomic_write_text(log_path, json.dumps(data, ensure_ascii=False, indent=2))
