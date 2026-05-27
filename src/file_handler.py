import asyncio
import shutil
from datetime import datetime
from pathlib import Path

from aiogram.types import Message, MessageOriginUser, MessageOriginChat, MessageOriginChannel, MessageOriginHiddenUser

from config import BOT_TOTAL_DATA_LIMIT
from utils import normalize_filename, unique_path, append_file_data, slugify_cyrillic_to_ascii, load_json_list_safe, format_size, get_dir_size



def _extract_metadata(message: Message) -> dict:
    """Извлекает метаданные из сообщения для формирования имен."""
    metadata = {
        "forward_from": None,
        "forward_date": None,
        "caption": None,
    }
    if message.forward_origin:
        origin = message.forward_origin
        source_name = "Unknown"
        if isinstance(origin, MessageOriginUser):
            source_name = origin.sender_user.full_name
        elif isinstance(origin, MessageOriginChat):
            source_name = origin.sender_chat.title
        elif isinstance(origin, MessageOriginChannel):
            source_name = origin.chat.title
        elif isinstance(origin, MessageOriginHiddenUser):
            source_name = origin.sender_user_name

        metadata["forward_from"] = source_name
        metadata["forward_date"] = origin.date

    if message.caption:
        metadata["caption"] = " ".join(message.caption.split())

    return metadata


def _generate_storage_base_name(original_name: str, metadata: dict, is_document: bool) -> str:
    """Реализует СТАРУЮ логику формирования имени для сохранения (stored_name)."""
    name_metadata = ""
    if metadata["forward_from"]:
        date_label = metadata["forward_date"].strftime("%Y%m%d_%H%M%S")
        name_metadata = f"forward [{metadata['forward_from']}]-{date_label}"
    elif metadata["caption"]:
        name_metadata = metadata["caption"]

    if name_metadata and is_document and original_name:
        if "." in original_name:
            name_part, ext_part = original_name.rsplit(".", 1)
            return f"{name_part}({name_metadata}).{ext_part}"
        return f"{original_name}({name_metadata})"

    return original_name


def _generate_display_name(original_name: str, extension: str, metadata: dict, message: Message) -> str:
    """Генерирует имя для отображения пользователю (место для ваших новых правил)."""
    # Пока оставляем логику, идентичную старой, до получения новых правил
    display_name = _generate_storage_base_name(original_name, metadata, bool(message.document))

    if not display_name:
        if metadata["caption"]:
            display_name = f"{metadata['caption'][:150].strip()}{extension}"
        else:
            timestamp = message.date.strftime("%Y%m%d_%H%M%S")
            display_name = f"file_{timestamp}_{message.message_id}{extension}"

    return display_name


async def save_incoming_file(message: Message, file_name: str | None, destination_dir: Path) -> tuple[dict, dict | None]:
    """
    Сохранить входящий файл/медиа из сообщения.

    Returns:
        tuple[dict, dict | None]: (новая_информация_о_файле, информация_о_дубликате_если_есть)
    """
    content = None
    original_name = file_name
    extension = ""
    file_size = 0

    if message.document:
        content, original_name = message.document, (file_name or message.document.file_name)
    elif message.audio:
        content, original_name, extension = message.audio, (file_name or message.audio.file_name), ".mp3"
    elif message.video:
        content, original_name, extension = message.video, (file_name or message.video.file_name), ".mp4"
    elif message.voice:
        content, extension = message.voice, ".ogg"
    elif message.sticker:
        content, extension = message.sticker, ".webp"
    elif message.video_note:
        content, extension = message.video_note, ".mp4"
    elif message.photo:
        content, extension = max(message.photo, key=lambda p: (p.file_size or 0)), ".jpg"

    if not content:
        return {}, None

    # 1. Извлекаем метаданные и формируем имена
    metadata = _extract_metadata(message)

    # База для физического имени (сохраняем старую логику)
    storage_base = _generate_storage_base_name(original_name, metadata, bool(message.document))

    # Имя для отображения пользователю (сюда пойдут новые правила)
    display_name = _generate_display_name(original_name, extension, metadata, message)

    file_size = content.file_size or 0

    # ПРОВЕРКА ОБЩЕГО ОБЪЕМА ДАННЫХ
    base_data_dir = destination_dir.parent
    current_usage = get_dir_size(base_data_dir)
    if current_usage + file_size > BOT_TOTAL_DATA_LIMIT:
        remaining_quota = max(0, BOT_TOTAL_DATA_LIMIT - current_usage)
        raise PermissionError(f"Превышен лимит хранилища бота ({format_size(BOT_TOTAL_DATA_LIMIT)}). "
                             f"Осталось места по квоте: {format_size(remaining_quota)}.")

    # Проверка физического места на диске
    disk_usage = shutil.disk_usage(base_data_dir if base_data_dir.exists() else ".")
    if disk_usage.free < file_size:
        raise OSError(f"На физическом диске сервера недостаточно места. Свободно: {format_size(disk_usage.free)}.")

    # ПРОВЕРКА НА ДУБЛИКАТ ИМЕНИ
    files_data = get_user_files(destination_dir)
    duplicate_info = next((f for f in files_data if f.get("original_name") == display_name), None)

    if duplicate_info:
        # Добавляем текущее время без двоеточий
        time_suffix = message.date.strftime("%H%M%S")
        if "." in display_name:
            stem, ext = display_name.rsplit(".", 1)
            display_name = f"{stem}({time_suffix}).{ext}"
        else:
            display_name = f"{display_name}({time_suffix})"

    final_name = normalize_filename(storage_base or display_name)
    # Используем file_id для временного файла, чтобы избежать конфликтов при одновременной загрузке
    tmp_path = destination_dir / f"{content.file_id}.download"

    try:
        # Гарантируем, что папка пользователя существует на диске перед скачиванием
        destination_dir.mkdir(parents=True, exist_ok=True)

        # Ограничиваем время скачивания из Telegram (например, 2 минуты)
        try:
            await asyncio.wait_for(message.bot.download(content.file_id, destination=tmp_path), timeout=120)
        except asyncio.TimeoutError:
            # Выбрасываем OSError, так как он уже красиво обрабатывается в handlers.py
            raise OSError("Загрузка файла из Telegram заняла слишком много времени (тайм-аут).")

        if not tmp_path.exists():
            raise FileNotFoundError(f"Временный файл не найден после загрузки: {tmp_path}")

        # Вычисляем финальный путь только после загрузки, чтобы избежать гонки имен (например, в альбомах)
        final_path = unique_path(destination_dir / final_name)
        await asyncio.to_thread(shutil.move, str(tmp_path), str(final_path))

        file_info = {
            "original_name": display_name,
            "stored_name": final_path.name,
            "upload_date": datetime.now().isoformat(),  # Используем локальное наивное время для консистентности
            "size": file_size
        }
        append_file_data(destination_dir / "files_data.json", file_info)
        return file_info, duplicate_info
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise e



def get_user_files(user_dir: Path) -> list[dict]:
    """Получить список файлов, загруженных пользователем."""
    files_data_path = user_dir / "files_data.json"
    return load_json_list_safe(files_data_path)
