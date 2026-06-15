import re
from datetime import datetime
from pathlib import Path
from config import FILE_ICONS

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

def format_date(raw_date: str) -> str:
    """Форматировать ISO-дату в читаемый вид, вернуть 'неизвестно' при ошибке."""
    try:
        return datetime.fromisoformat(raw_date).strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return "неизвестно"

def wrap_filename(name: str, width: int = 37, indent: str = "      ") -> str:
    """Разбивает имя файла на части для предотвращения некрасивого переноса в Telegram."""
    if len(name) <= width:
        return name
    chunks = [name[i:i + width] for i in range(0, len(name), width)]
    return f"\n{indent}".join(chunks)

def get_file_icon(filename: str) -> str:
    """Определяет иконку на основе расширения файла."""
    name_lower = filename.lower()
    if name_lower.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff')):
        return FILE_ICONS["image"]
    if name_lower.endswith(('.mp4', '.mov', '.avi', '.mkv', '.3gp', '.flv')):
        return FILE_ICONS["video"]
    if name_lower.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac')):
        return FILE_ICONS["audio"]
    if name_lower.endswith(('.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz')):
        return FILE_ICONS["archive"]
    return FILE_ICONS["document"]

def format_saved_file_message(file_info: dict) -> str:
    """Единый расширенный ответ после успешного сохранения файла."""
    display_name = file_info.get("original_name", "Unknown")
    stored_name = file_info.get("stored_name", display_name)
    icon = get_file_icon(stored_name)
    size = format_size(file_info.get("size", 0))
    date_str = format_date(file_info.get("upload_date", ""))

    return (
        "✅ Файл сохранен.\n\n"
        f"{icon} <b>Имя:</b> <code>{wrap_filename(display_name, indent='   ')}</code>\n"
        f"📄 <b>Файл:</b> <code>{wrap_filename(stored_name, indent='   ')}</code>\n"
        f"💾 <b>Размер:</b> {size}\n"
        f"📅 <b>Дата:</b> {date_str}"
    )

def format_media_mode_message(enabled: bool) -> str:
    """Сформировать сообщение о переключении режима мультимедиа."""
    if enabled:
        return "🎬 Мультимедийный режим включен.\nФото и видео будут отправляться как медиа."
    return "📄 Режим документов включен.\nВсе файлы будут отправляться как документы."


def format_preview_caption(file_info: dict) -> str:
    """Сформировать подпись для элемента альбома превью."""
    name = file_info.get("original_name", "Unknown")
    size = format_size(file_info.get("size", 0))
    date_str = format_date(file_info.get("upload_date", ""))
    return f"{name}\n{size} | {date_str}"


def strip_display_extension(display_name: str, stored_name: str) -> str:
    """Убрать расширение из отображаемого имени, не меняя имя файла на диске."""
    ext = Path(stored_name).suffix
    if ext and display_name.lower().endswith(ext.lower()):
        return display_name[:-len(ext)]
    return display_name
