import asyncio
import shutil
from pathlib import Path

from aiogram.types import Message

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

    # Если есть подпись (caption) и это документ, добавляем её в имя файла
    if message.caption and message.document:
        # Извлекаем имя и расширение, чтобы вставить подпись перед точкой
        if "." in original_name:
            name_part, ext_part = original_name.rsplit(".", 1)
            original_name = f"{name_part}({message.caption}).{ext_part}"
        else:
            original_name = f"{original_name}({message.caption})"

    file_size = content.file_size or 0

    # ПРОВЕРКА ОБЩЕГО ОБЪЕМА ДАННЫХ
    base_data_dir = destination_dir.parent
    sum_size = get_dir_size(base_data_dir) + file_size
    if sum_size > BOT_TOTAL_DATA_LIMIT:
        # raise PermissionError(f"Превышен общий лимит дискового пространства - {sum_size_str}")
        raise PermissionError(f"Превышен лимит - {format_size(sum_size)} > {format_size(BOT_TOTAL_DATA_LIMIT)}")

    if not original_name:
        if message.caption:
            safe_caption = slugify_cyrillic_to_ascii(message.caption)
            original_name = f"{safe_caption}{extension}"
        else:
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
        await message.bot.download(content.file_id, destination=tmp_path)
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


