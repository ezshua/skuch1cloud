import logging
from datetime import datetime
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, logger
from file_handler import save_incoming_file, get_user_files
from utils import format_size, append_file_data, atomic_write_text, get_dir_size, log_user_action
import json
from users import ensure_user_dir
from texts import get_welcome_message


def build_dispatcher() -> Dispatcher:
    """Построить диспетчер с обработчиками."""
    dp = Dispatcher()
    # Словари для хранения состояния бота
    user_status_msgs: dict[int, int] = {}
    pending_deletions: dict[int, dict] = {}
    FILES_PER_PAGE = 10


    async def scan_and_fix_files(user_dir: Path) -> None:
        """Сканирует директорию на наличие файлов, которых нет в files_data.json."""
        files_data_path = user_dir / "files_data.json"
        existing_data = get_user_files(user_dir)
        stored_names = {f.get("stored_name") for f in existing_data}

        # Служебные файлы и временные расширения, которые не должны попасть в индекс
        excluded_files = {"files_data.json", "action_log.json"}
        temp_extensions = {".tmp", ".download"}

        for file_path in user_dir.iterdir():
            if (file_path.is_file() and
                file_path.name not in excluded_files and
                file_path.suffix not in temp_extensions and
                file_path.name not in stored_names):
                # Добавляем файл в список
                file_info = {
                    "original_name": file_path.name,
                    "stored_name": file_path.name,
                    "upload_date": datetime.fromtimestamp(file_path.stat().st_ctime).isoformat(),
                    "size": file_path.stat().st_size
                }
                append_file_data(files_data_path, file_info)


    def _format_date(raw_date: str) -> str:
        """Форматировать ISO-дату в читаемый вид, вернуть 'неизвестно' при ошибке."""
        try:
            return datetime.fromisoformat(raw_date).strftime("%d.%m.%Y %H:%M:%S")
        except (ValueError, TypeError):
            return "неизвестно"


    def _wrap_filename(name: str, width: int = 37, indent: str = "      ") -> str:
        """Разбивает имя файла на части для предотвращения некрасивого переноса в Telegram."""
        if len(name) <= width:
            return name
        # Разбиваем строку на куски по width символов
        chunks = [name[i:i + width] for i in range(0, len(name), width)]
        return f"\n{indent}".join(chunks)


    def _clean_filename(text: str) -> str:
        """Убирает технические переносы и отступы, которые бот добавил для красоты при выводе."""
        # Убираем наши переносы с любым количеством пробелов
        import re
        text = re.sub(r"\n\s+", "", text)
        # Если скопировалось вместе с иконкой "📁", берем только то, что после неё
        return text.split("📁")[-1].strip()


    async def _get_status_content(user_dir: Path, page: int = 1):
        """Формирует текст и клавиатуру для команды /status с учетом пагинации."""
        files = get_user_files(user_dir)
        if not files:
            return "Вы еще не отправили ни одного файла.", None

        total_pages = (len(files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * FILES_PER_PAGE
        end_idx = start_idx + FILES_PER_PAGE
        page_files = files[start_idx:end_idx]

        total_user_size = sum(f.get("size", 0) for f in files)
        base_data_dir = user_dir.parent
        total_all_size = get_dir_size(base_data_dir)
        remaining = BOT_TOTAL_DATA_LIMIT - total_all_size
        remaining_str = format_size(max(0, remaining)) if remaining >= 0 else "<i><u>Лимит превышен</u></i>"

        header = (f"<b>Размер каталога: {format_size(total_user_size)}. Свободно: {remaining_str}</b>\n"
                  f"<b>Список файлов (стр. {page}/{total_pages}):</b>\n\n")

        lines = []
        for i, file_info in enumerate(page_files, start_idx + 1):
            name = file_info.get("original_name", "Unknown")
            size = format_size(file_info.get("size", 0))
            date_str = _format_date(file_info.get("upload_date", ""))
            prefix = f"{i}. 📁 "
            # Динамический отступ, чтобы имя во второй строке было ровно под именем в первой
            wrapped_name = _wrap_filename(name, indent=" " * len(prefix))
            lines.append(f"<code>{prefix}{wrapped_name}</code>\n   📅 {date_str} | 💾 {size}\n")

        response = header + "".join(lines)

        buttons = []
        if page > 1:
            buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"status_page:{page-1}"))
        if page < total_pages:
            buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"status_page:{page+1}"))

        kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
        return response, kb


    @dp.message(CommandStart())
    async def command_start_handler(message: Message) -> None:
        """Обработчик команды /start."""
        if not message.from_user:
            return
        user_dir = await ensure_user_dir(message.from_user, create=True)
        # Сканируем при запуске
        await scan_and_fix_files(user_dir)

        log_user_action(user_dir, "user_command", {"command": "/start"})

        # Получаем обновленный список файлов
        files = get_user_files(user_dir)
        total_size = sum(f.get("size", 0) for f in files)

        welcome_text = get_welcome_message(user_dir.name, format_size(total_size), len(files))
        await message.answer(welcome_text)
        log_user_action(user_dir, "bot_response", {"type": "welcome"})

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

        log_user_action(user_dir, "user_command", {"command": "/status"})
        response, kb = await _get_status_content(user_dir, page=1)
        sent_message = await message.answer(response, reply_markup=kb)
        # Сохраняем ID сообщения
        user_status_msgs[message.from_user.id] = sent_message.message_id

        files = get_user_files(user_dir)
        total_size = sum(f.get("size", 0) for f in files)
        log_user_action(user_dir, "bot_response", {
            "type": "status_summary",
            "files_count": len(files),
            "total_size": format_size(total_size)
        })


    @dp.callback_query(F.data.startswith("status_page:"))
    async def status_pagination_handler(callback: CallbackQuery) -> None:
        """Обработчик переключения страниц в списке файлов."""
        user_dir = await ensure_user_dir(callback.from_user, create=False)
        if not user_dir:
            await callback.answer("Ошибка сессии. Введите /start", show_alert=True)
            return

        try:
            page = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            page = 1

        # log_user_action(user_dir, "user_interaction", {"action": "pagination", "page": page})
        text, kb = await _get_status_content(user_dir, page)

        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            # Ошибка может возникнуть, если сообщение не изменилось
            pass
        await callback.answer()

        # files = get_user_files(user_dir)
        # total_size = sum(f.get("size", 0) for f in files)
        # log_user_action(user_dir, "bot_response", {
        #     "type": "status_summary_pagination",
        #     "page": page,
        #     "files_count": len(files),
        #     "total_size": format_size(total_size)
        # })


    @dp.message(Command("delete"))
    async def command_delete_handler(message: Message) -> None:
        """Обработчик команды /delete [имя файла]."""
        if not message.from_user:
            return

        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("⚠️ Пожалуйста, укажите имя файла: <code>/delete имя_файла</code>")
            return

        filename = _clean_filename(args[1])
        user_dir = await ensure_user_dir(message.from_user, create=False)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return

        log_user_action(user_dir, "user_command", {"command": f"/delete {filename}"})

        files_data = get_user_files(user_dir)
        target = next((f for f in files_data if f.get("original_name") == filename), None)

        if not target:
            await message.answer(f"❌ Файл <code>{filename}</code> не найден в вашем списке.")
            return

        # Сохраняем состояние ожидания подтверждения
        pending_deletions[message.from_user.id] = target

        size = format_size(target.get("size", 0))
        date_str = _format_date(target.get("upload_date", ""))

        await message.answer(
            f"❓ <b>Подтвердите удаление файла:</b>\n\n"
            f"<code>📁 {_wrap_filename(target['original_name'], indent='   ')}</code>\n"
            f"� {date_str} | 💾 {size}\n\n"
            f"Для удаления отправьте: <b>да</b>, <b>yes</b> или <b>так</b>.\n"
            f"Любое другое сообщение или файл отменит удаление."
        )
        log_user_action(user_dir, "bot_response", {"type": "delete_confirmation_request", "file": filename})

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
            log_user_action(user_dir, "user_upload", {"media_type": type(media_obj).__name__, "size": media_obj.file_size})

            try:
                file_info, duplicate_info = await save_incoming_file(message, None, user_dir)

                if duplicate_info:
                    # Форматируем данные о дубликате
                    dup_size = format_size(duplicate_info.get("size", 0))
                    dup_date = _format_date(duplicate_info.get("upload_date", ""))

                    # Форматируем данные о новом файле
                    new_size = format_size(file_info.get("size", 0))
                    new_date = _format_date(file_info.get("upload_date", ""))

                    await message.answer(
                        f"⚠️ Файл с таким именем уже был:\n"
                        f"<code>📁 {_wrap_filename(duplicate_info['original_name'], indent='   ')}</code>\n"
                        f"� {dup_date} | 💾 {dup_size}\n\n"
                        f"✅ Новый файл сохранен под именем:\n"
                        f"<code>📁 {_wrap_filename(file_info['original_name'], indent='   ')}</code>\n"
                        f" {new_date} | 💾 {new_size}"
                    )
                    log_user_action(user_dir, "bot_response", {"type": "file_saved_duplicate", "name": file_info['original_name']})
                else:
                    await message.answer("Файл сохранен.")
                    log_user_action(user_dir, "bot_response", {"type": "file_saved", "name": file_info['original_name']})

                # Отправляем сообщение об отмене удаления, если оно было
                if cancelled_delete_target:
                    await message.answer(f"🚫 Удаление файла <code>{cancelled_delete_target['original_name']}</code> отменено.")
                    log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_media"})

            except Exception as e:
                logger.exception("Ошибка при сохранении файла:")
                await message.answer(f"⚠️ Ошибка при сохранении: {type(e).__name__}: {e}")
            return

        # Если это не медиа-файл, но был pending_deletions, то отменяем и сообщаем
        elif cancelled_delete_target:
            user_dir = await ensure_user_dir(message.from_user, create=False)
            if user_dir:
                 log_user_action(user_dir, "user_action_cancelled_delete", {"reason": "unexpected_media"})
                 await message.answer(f"🚫 Удаление файла <code>{cancelled_delete_target['original_name']}</code> отменено.")
                 log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_media_generic"})


    @dp.message(F.text)
    async def text_echo_handler(message: Message) -> None:
        """Обработчик текстовых сообщений: выдача файла, подтверждение удаления или эхо."""
        if not message.from_user or not message.text:
            return

        user_id = message.from_user.id
        user_text = message.text.strip().lower()

        user_dir = await ensure_user_dir(message.from_user, create=False)
        if user_dir:
            log_user_action(user_dir, "user_text", {"text": message.text})

        # 1. Проверяем, не является ли сообщение подтверждением удаления
        if user_id in pending_deletions:
            target = pending_deletions.pop(user_id)
            if user_text in ["да", "yes", "так"]:
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
                    log_user_action(user_dir, "bot_response", {"type": "delete_success", "file": target['original_name']})

                    # Удаляем старое сообщение статуса и вызываем новый статус
                    prev_status_id = user_status_msgs.pop(user_id, None)
                    if prev_status_id:
                        try:
                            await message.bot.delete_message(chat_id=message.chat.id, message_id=prev_status_id)
                        except Exception:
                            pass

                    # Пересканируем (на всякий случай) и выводим статус
                    await scan_and_fix_files(user_dir)
                    await command_status_handler(message) # Вызываем хендлер статуса
                    return # Завершаем обработку после удаления
            else:
                # Удаление отменено - сообщаем и завершаем обработку
                if user_dir:
                    await message.answer(f"🚫 Удаление файла <code>{target['original_name']}</code> отменено.")
                    log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_text"})
                return # Завершаем обработку, чтобы не было эхо-ответа

        # 2. Пытаемся найти файл по имени в директории пользователя
        if user_dir:
            cleaned_name = _clean_filename(message.text)
            files_data = get_user_files(user_dir)
            # Ищем точное совпадение имени (оригинального)
            target = next((f for f in files_data if f.get("original_name") == cleaned_name), None)

            if target:
                file_path = user_dir / target["stored_name"]
                if file_path.exists():
                    try:
                        # Отправляем найденный файл как документ
                        await message.answer_document(
                            document=FSInputFile(path=file_path, filename=target["original_name"]),
                            caption=f"📁 Файл: {target['original_name']}"
                        )
                        log_user_action(user_dir, "bot_response", {"type": "send_file", "file": target['original_name']})
                        return
                    except Exception as e:
                        logger.error(f"Ошибка при отправке документа: {e}")
                else:
                    # Файл есть в списке, но удалён с диска
                    await message.answer(
                        f"⚠️ Файл <code>{target['original_name']}</code> не найден на диске.\n"
                        f"Возможно, он был удалён вручную. Используйте /delete для удаления записи."
                    )
                    log_user_action(user_dir, "bot_response", {"type": "file_not_found_on_disk", "file": target['original_name']})
                    return

        # 3. Если файл не найден или произошла ошибка — обычное эхо
        try:
            await message.answer(f"📢 <b>Эхо:</b> {message.text}")
            if user_dir:
                log_user_action(user_dir, "bot_response", {"type": "echo", "text": message.text})
        except Exception:
            await message.answer("Я принимаю только файлы или имена ваших файлов.")

    return dp
