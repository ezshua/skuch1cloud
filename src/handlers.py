import logging
from datetime import datetime
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, logger
from file_handler import save_incoming_file, get_user_files
from utils import format_size, append_file_data, atomic_write_text, get_dir_size
import json
from users import ensure_user_dir


def build_dispatcher() -> Dispatcher:
    """Построить диспетчер с обработчиками."""
    dp = Dispatcher()
    # Словари для хранения состояния бота
    user_status_msgs: dict[int, int] = {}
    pending_deletions: dict[int, dict] = {}


    async def scan_and_fix_files(user_dir: Path) -> None:
        """Сканирует директорию на наличие файлов, которых нет в files_data.json."""
        files_data_path = user_dir / "files_data.json"
        existing_data = get_user_files(user_dir)
        stored_names = {f.get("stored_name") for f in existing_data}

        # Получаем все файлы в директории, кроме json
        for file_path in user_dir.iterdir():
            if file_path.is_file() and file_path.name != "files_data.json" and file_path.name not in stored_names:
                # Добавляем файл в список
                file_info = {
                    "original_name": file_path.name,
                    "stored_name": file_path.name,
                    "upload_date": datetime.fromtimestamp(file_path.stat().st_ctime).isoformat(),
                    "size": file_path.stat().st_size
                }
                append_file_data(files_data_path, file_info)

    @dp.message(CommandStart())
    async def command_start_handler(message: Message) -> None:
        """Обработчик команды /start."""
        if not message.from_user:
            return
        user_dir = await ensure_user_dir(message.from_user, create=True)
        # Сканируем при запуске
        await scan_and_fix_files(user_dir)

        # Получаем обновленный список файлов
        files = get_user_files(user_dir)
        total_size = sum(f.get("size", 0) for f in files)

        await message.answer(
            f"Привет! Твоя папка: <b>{user_dir.name}</b> ({format_size(total_size)} в {len(files)} файлах)"
        )

    @dp.message(Command("status"))
    async def command_status_handler(message: Message) -> None:
        """Обработчик команды /status."""
        if not message.from_user:
            return

        # Удаляем предыдущее сообщение статуса, если оно было
        prev_status_id = user_status_msgs.pop(message.from_user.id, None)
        if prev_status_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=prev_status_id)
            except Exception:
                pass

        # Всегда определяем user_dir
        user_dir = await ensure_user_dir(message.from_user, create=False)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return

        files = get_user_files(user_dir)
        if not files:
            await message.answer("Вы еще не отправили ни одного файла.")
            return

        # Расчет размеров
        total_user_size = sum(f.get("size", 0) for f in files)
        base_data_dir = user_dir.parent
        total_all_size = get_dir_size(base_data_dir)
        remaining = BOT_TOTAL_DATA_LIMIT - total_all_size
        remaining_str = format_size(max(0, remaining)) if remaining >= 0 else "Лимит превышен"

        response = f"<b>Размер каталога: {format_size(total_user_size)}. Свободно: {remaining_str}</b>\n\n<b>Список файлов:</b>\n\n"
        for i, file_info in enumerate(files, 1):
            name = file_info.get("original_name", "Unknown")
            size = format_size(file_info.get("size", 0))

            # Парсим дату и форматируем ее
            raw_date = file_info.get("upload_date", "")
            try:
                date_obj = datetime.fromisoformat(raw_date)
                date_str = date_obj.strftime("%d.%m.%Y %H:%M:%S")
            except (ValueError, TypeError):
                date_str = "неизвестно"

            response += f"{i}. 📁 <code>{name}</code>\n"
            response += f"   📅 {date_str} | 💾 {size}\n\n"

        # Если сообщение слишком длинное, Telegram может его отклонить.
        if len(response) > 4096:
            response = response[:4090] + "..."

        sent_message = await message.answer(response)
        # Сохраняем ID сообщения
        user_status_msgs[message.from_user.id] = sent_message.message_id


    @dp.message(Command("delete"))
    async def command_delete_handler(message: Message) -> None:
        """Обработчик команды /delete [имя файла]."""
        if not message.from_user:
            return
        
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("⚠️ Пожалуйста, укажите имя файла: <code>/delete имя_файла</code>")
            return
        
        filename = args[1].strip()
        user_dir = await ensure_user_dir(message.from_user, create=False)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return

        files_data = get_user_files(user_dir)
        target = next((f for f in files_data if f.get("original_name") == filename), None)
        
        if not target:
            await message.answer(f"❌ Файл <code>{filename}</code> не найден в вашем списке.")
            return

        # Сохраняем состояние ожидания подтверждения
        pending_deletions[message.from_user.id] = target
        
        size = format_size(target.get("size", 0))
        raw_date = target.get("upload_date", "")
        try:
            date_obj = datetime.fromisoformat(raw_date)
            date_str = date_obj.strftime("%d.%m.%Y %H:%M:%S")
        except:
            date_str = "неизвестно"

        await message.answer(
            f"❓ <b>Подтвердите удаление файла:</b>\n\n"
            f"📁 <code>{target['original_name']}</code>\n"
            f"📅 {date_str} | 💾 {size}\n\n"
            f"Для удаления отправьте: <b>да</b>, <b>yes</b> или <b>так</b>.\n"
            f"Любое другое сообщение или файл отменит удаление."
        )

    @dp.message(F.document | F.audio | F.video | F.voice | F.sticker | F.video_note | F.photo)
    async def incoming_files_handler(message: Message) -> None:
        """Обработчик входящих файлов и медиа."""
        if not message.from_user:
            return

        user_id = message.from_user.id
        cancelled_delete_target = None

        # Отменяем ожидание удаления, если пользователь прислал файл,
        # но сообщение об отмене отправим позже.
        if user_id in pending_deletions:
            cancelled_delete_target = pending_deletions.pop(user_id)
        # Удаляем сообщение /status при получении нового файла
        status_msg_id = user_status_msgs.pop(message.from_user.id, None)
        if status_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=status_msg_id)
            except Exception:
                pass # Сообщение могло быть уже удалено пользователем

        # Определяем объект медиа для проверки размера
        media_obj = (\
            message.document or message.audio or message.video or
            message.voice or message.video_note or message.sticker or
            (max(message.photo, key=lambda p: p.file_size) if message.photo else None)\
        )

        if media_obj:
            # ПРОВЕРКА РАЗМЕРА ФАЙЛА (20 МБ)
            if media_obj.file_size and media_obj.file_size > MAX_FILE_SIZE:
                size_mb = round(media_obj.file_size / (1024 * 1024), 2)
                await message.answer(
                    f"⚠️ Файл слишком большой ({size_mb} МБ).\n"
                    f"Telegram ограничивает ботов скачиванием файлов до 20 МБ."
                )
                return

            user_dir = await ensure_user_dir(message.from_user, create=False)
            if not user_dir:
                await message.answer("Пожалуйста, сначала отправьте /start.")
                return

            try:
                file_info, duplicate_info = await save_incoming_file(message, None, user_dir)

                if duplicate_info:
                    # Форматируем данные о дубликате
                    dup_size = format_size(duplicate_info.get("size", 0))
                    dup_date = datetime.fromisoformat(duplicate_info.get("upload_date", "")).strftime("%d.%m.%Y %H:%M:%S")

                    # Форматируем данные о новом файле
                    new_size = format_size(file_info.get("size", 0))
                    new_date = datetime.fromisoformat(file_info.get("upload_date", "")).strftime("%d.%m.%Y %H:%M:%S")

                    await message.answer(
                        f"⚠️ Файл с таким именем уже был:\n"
                        f"📁 <code>{duplicate_info['original_name']}</code>\n"
                        f"📅 {dup_date} | 💾 {dup_size}\n\n"
                        f"✅ Новый файл сохранен под именем:\n"
                        f"📁 <code>{file_info['original_name']}</code>\n"
                        f"📅 {new_date} | 💾 {new_size}"
                    )
                else:
                    await message.answer("Файл сохранен.")

                # Отправляем сообщение об отмене удаления, если оно было
                if cancelled_delete_target:
                    await message.answer(f"🚫 Удаление файла <code>{cancelled_delete_target['original_name']}</code> отменено.")

            except Exception as e:
                logger.exception("Ошибка при сохранении файла:")
                await message.answer(f"⚠️ Ошибка при сохранении: {type(e).__name__}: {e}")
            return

        # Если это не медиа-файл, но был pending_deletions, то отменяем и сообщаем
        elif cancelled_delete_target:
             await message.answer(f"🚫 Удаление файла <code>{cancelled_delete_target['original_name']}</code> отменено.")


    @dp.message(F.text)
    async def text_echo_handler(message: Message) -> None:
        """Обработчик текстовых сообщений: выдача файла, подтверждение удаления или эхо."""
        if not message.from_user or not message.text:
            return

        user_id = message.from_user.id
        user_text = message.text.strip().lower()

        # 1. Проверяем, не является ли сообщение подтверждением удаления
        if user_id in pending_deletions:
            target = pending_deletions.pop(user_id)
            if user_text in ["да", "yes", "так"]:
                user_dir = await ensure_user_dir(message.from_user, create=False)
                if user_dir:
                    file_path = user_dir / target["stored_name"]
                    files_data_path = user_dir / "files_data.json"

                    # Удаляем физически
                    if file_path.exists():
                        file_path.unlink()

                    # Обновляем JSON (удаляем запись)
                    files_data = get_user_files(user_dir)
                    updated_data = [f for f in files_data if f.get("stored_name") != target["stored_name"]]
                    atomic_write_text(files_data_path, json.dumps(updated_data, ensure_ascii=False, indent=2))

                    await message.answer(f"✅ Файл <code>{target['original_name']}</code> удален.")

                    # Удаляем старое сообщение статуса и вызываем новый статус
                    prev_status_id = user_status_msgs.pop(user_id, None)
                    if prev_status_id:
                        try:
                            await message.bot.delete_message(chat_id=message.chat.id, message_id=prev_status_id)
                        except:
                            pass

                    # Пересканируем (на всякий случай) и выводим статус
                    await scan_and_fix_files(user_dir)
                    await command_status_handler(message) # Вызываем хендлер статуса
                    return # Завершаем обработку после удаления
            else:
                # Удаление отменено - сообщаем и завершаем обработку
                await message.answer(f"🚫 Удаление файла <code>{target['original_name']}</code> отменено.")
                return # Завершаем обработку, чтобы не было эхо-ответа

        # 2. Пытаемся найти файл по имени в директории пользователя
        user_dir = await ensure_user_dir(message.from_user, create=False)
        if user_dir:
            files_data = get_user_files(user_dir)
            # Ищем точное совпадение имени (оригинального)
            target = next((f for f in files_data if f.get("original_name") == message.text), None)

            if target:
                file_path = user_dir / target["stored_name"]
                if file_path.exists():
                    try:
                        # Отправляем найденный файл как документ
                        await message.answer_document(
                            document=FSInputFile(path=file_path, filename=target["original_name"]),
                            caption=f"📁 Файл: {target['original_name']}"
                        )
                        return
                    except Exception as e:
                        logger.error(f"Ошибка при отправке документа: {e}")

        # 3. Если файл не найден или произошла ошибка — обычное эхо
        try:
            await message.answer(f"📢 <b>Эхо:</b> {message.text}")
        except Exception:
            await message.answer("Я принимаю только файлы или имена ваших файлов.")

    return dp