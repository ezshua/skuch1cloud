import asyncio
import shutil
from pathlib import Path

from aiogram.types import Message, MessageOriginUser, MessageOriginChat, MessageOriginChannel, MessageOriginHiddenUser

from config import BOT_TOTAL_DATA_LIMIT
from utils import normalize_filename, unique_path, append_file_data, slugify_cyrillic_to_ascii, load_json_list_safe, format_size, get_dir_size



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

    # Извлекаем метаданные для имени: либо информацию о пересылке, либо подпись
    name_metadata = ""
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

        date_label = origin.date.strftime("%Y%m%d_%H%M%S")
        name_metadata = f"forward [{source_name}]-{date_label}"
    elif message.caption:
        # Очищаем подпись от переносов строк для безопасного хранения и поиска
        name_metadata = " ".join(message.caption.split())

    # Если есть метаданные и это документ, добавляем их в имя файла
    if name_metadata and message.document:
        # Извлекаем имя и расширение, чтобы вставить подпись перед точкой
        if "." in original_name:
            name_part, ext_part = original_name.rsplit(".", 1)
            original_name = f"{name_part}({name_metadata}).{ext_part}"
        else:
            original_name = f"{original_name}({name_metadata})"

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

    if not original_name and name_metadata:
            # Для "красивого" имени в списке оставляем подпись как есть (без новых строк),
            # но ограничиваем длину, чтобы оно не было слишком "странным".
            original_name = f"{name_metadata[:150].strip()}{extension}"

    if not original_name:
            timestamp = message.date.strftime("%Y%m%d_%H%M%S")
            original_name = f"file_{timestamp}_{content.file_id[-8:]}{extension}"

    # ПРОВЕРКА НА ДУБЛИКАТ ИМЕНИ
    files_data = get_user_files(destination_dir)
    duplicate_info = next((f for f in files_data if f.get("original_name") == original_name), None)

    if duplicate_info:
        # Добавляем текущее время без двоеточий
        time_suffix = message.date.strftime("%H%M%S")
        if "." in original_name:
            stem, ext = original_name.rsplit(".", 1)
            original_name = f"{stem}({time_suffix}).{ext}"
        else:
            original_name = f"{original_name}({time_suffix})"

    final_name = normalize_filename(original_name)
    final_path = unique_path(destination_dir / final_name)
    tmp_path = final_path.with_suffix(final_path.suffix + ".download")

    try:
        # Гарантируем, что папка пользователя существует на диске перед скачиванием
        destination_dir.mkdir(parents=True, exist_ok=True)

        await message.bot.download(content.file_id, destination=tmp_path)

        if not tmp_path.exists():
            raise FileNotFoundError(f"Временный файл не найден после загрузки: {tmp_path}")

        await asyncio.to_thread(shutil.move, str(tmp_path), str(final_path))

        file_info = {
            "original_name": original_name,
            "stored_name": final_path.name,
            "upload_date": message.date.isoformat(),  # ISO формат для удобства хранения
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
