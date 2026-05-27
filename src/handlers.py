import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from aiogram import Dispatcher, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, MAX_DISPLAY_NAME_LEN, ADMIN_ID, FILE_ICONS, logger, get_base_path
from file_handler import save_incoming_file, get_user_files
from utils import (
    format_size, append_file_data, atomic_write_text, get_dir_size, collect_users_summary,
    log_user_action, load_json_safe, collect_daily_report, shorten_name
)
import json
from users import ensure_user_dir
from texts import get_welcome_message
from url_handler import download_file_from_url


async def notify_admin(bot: Bot, text: str) -> None:
    """Отправляет текстовое уведомление администратору."""
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception as e:
            logger.error(f"Ошибка при уведомлении администратора: {e}")


async def forward_to_admin(message: Message) -> None:
    """Пересылает сообщение целиком администратору."""
    if ADMIN_ID:
        try:
            await message.forward(ADMIN_ID)
        except Exception as e:
            logger.error(f"Ошибка при пересылке сообщения администратору: {e}")


def build_dispatcher() -> Dispatcher:
    """Построить диспетчер с обработчиками."""
    dp = Dispatcher()
    # Словари для хранения состояния бота
    user_status_msgs: dict[int, int] = {}
    pending_deletions: dict[int, dict] = {}
    FILES_PER_PAGE = 10


    async def scan_and_fix_files(user_dir: Path) -> None:
        """
        Синхронизирует индекс файлов с реальным содержимым папки:
        1. Удаляет записи о файлах, которые физически отсутствуют.
        2. Проверяет и исправляет отображаемые имена существующих записей (длина и уникальность).
        3. Добавляет новые файлы, найденные в директории.
        """
        files_data_path = user_dir / "files_data.json"
        existing_data = get_user_files(user_dir)

        updated_data = []
        seen_visual_names = []
        data_changed = False

        # 1 & 2. Очистка и исправление имен (display names) для уже известных файлов
        for file_info in existing_data:
            stored_name = file_info.get("stored_name")
            if not stored_name or not (user_dir / stored_name).exists():
                data_changed = True
                continue

            old_display_name = file_info.get("original_name", "")

            # Для существующих записей проверяем только длину и уникальность.
            # Не навязываем префиксы, если их нет, просто приводим к лимиту.
            new_name = shorten_name(old_display_name, MAX_DISPLAY_NAME_LEN, seen_visual_names)
            if new_name != old_display_name:
                file_info["original_name"] = new_name
                data_changed = True

            updated_data.append(file_info)
            seen_visual_names.append(file_info["original_name"])

        stored_names = {f.get("stored_name") for f in updated_data}

        excluded_files = {"files_data.json", "action_log.json"}
        temp_extensions = {".tmp", ".download"}

        for file_path in user_dir.iterdir():
            if not file_path.is_file() or file_path.name in excluded_files:
                continue

            if file_path.suffix in temp_extensions:
                try:
                    file_path.unlink()
                except Exception:
                    pass
                continue

            if file_path.name not in stored_names:
                # Убираем ВСЕ технические префиксы из имени файла на диске
                clean_base = file_path.stem
                while True:
                    m = re.match(r'^(fwd|upl|dwn|fnd)_', clean_base)
                    if not m:
                        break
                    clean_base = clean_base[len(m.group(0)):]

                # Генерируем уникальное сокращенное имя (не более MAX_DISPLAY_NAME_LEN)
                new_display_name = shorten_name(f"fnd_{clean_base}{file_path.suffix}", MAX_DISPLAY_NAME_LEN, seen_visual_names)
                seen_visual_names.append(new_display_name)

                # Добавляем файл в список
                updated_data.append({
                    "original_name": new_display_name,
                    "stored_name": file_path.name,
                    "upload_date": datetime.fromtimestamp(file_path.stat().st_ctime).isoformat(),
                    "size": file_path.stat().st_size
                })
                data_changed = True

        if data_changed or len(updated_data) != len(existing_data):
            atomic_write_text(files_data_path, json.dumps(updated_data, ensure_ascii=False, indent=2))


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


    def _get_file_icon(filename: str) -> str:
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


    def _clean_filename(text: str) -> str:
        """Нормализует имя файла для сравнения: NFKC, удаление невидимых символов и пробелов."""
        # 1. Удаляем номер в начале списка (например, "37. ")
        text = re.sub(r'^\s*\d+\.\s*', '', text)

        # 2. Удаляем известные иконки, учитывая возможные невидимые вариаторы (\ufe0f)
        icons = list(FILE_ICONS.values())
        for icon in icons:
            # Пробуем найти иконку как есть и в "чистом" виде
            variants = [icon, icon.replace('\ufe0f', '')]
            found = False
            for v in variants:
                if v in text:
                    text = text.split(v)[-1]
                    found = True
                    break
            if found:
                break

        # 3. Удаляем ВСЕ технические префиксы источников (могут быть fnd_upl_...)
        text = text.strip()
        while True:
            changed = False
            for prefix in ["fwd_", "dwn_", "upl_", "fnd_"]:
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    changed = True
            if not changed:
                break

        # Нормализация Unicode (совмещает символы и их модификаторы в одну форму)
        text = unicodedata.normalize('NFKC', text)

        # 4. Удаляем любые оставшиеся спецсимволы/пробелы в самом начале до первой буквы или цифры
        text = re.sub(r'^[^\w\d]+', '', text)

        # 5. Удаляем расширение для сравнения (1-5 символов после точки в конце).
        # Это позволяет "точному совпадению" находить файл, даже если расширение не введено пользователем.
        text = re.sub(r'\.[a-zA-Z0-9]{1,5}$', '', text)

        # 6. Удаляем невидимые символы: вариаторы (\ufe00-\ufe0f), ZWSP (\u200b), мягкие переносы (\u00ad) и др.
        text = re.sub(r'[\u00ad\u200b-\u200d\ufeff\ufe00-\ufe0f]', '', text)

        # 7. Удаляем все пробелы, табы и переносы строк, приводим к нижнему регистру.
        return "".join(text.split()).lower()


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
            # Определяем иконку по физическому имени, так как в визуальном расширения может не быть
            icon = _get_file_icon(file_info.get("stored_name", name))

            # Отображаем имя целиком (оно уже сокращено функцией shorten_name и содержит уникальные индексы)
            prefix = f"{i}. {icon} "
            # Динамический отступ, чтобы имя во второй строке было ровно под именем в первой
            wrapped_name = _wrap_filename(name, indent=" " * len(prefix))
            lines.append(f"{prefix}<code>{wrapped_name}</code>\n   📅 {date_str} | 💾 {size}\n")

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

        await notify_admin(message.bot, f"👤 Пользователь {message.from_user.full_name} (@{message.from_user.username}) подключился.")
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

    @dp.message(Command("report"), F.from_user.id == ADMIN_ID)
    async def command_report_handler(message: Message) -> None:
        """Обработчик команды /report для администратора."""
        args = message.text.split()
        base_path = get_base_path()
        state_path = base_path / "bot_state.json"

        if len(args) > 1:
            subcommand = args[1].lower()
            if subcommand == "daily":
                state = load_json_safe(state_path)
                # По умолчанию True (включено), так как это было стандартное поведение
                is_enabled = state.get("daily_report_enabled", True)
                new_state = not is_enabled
                state["daily_report_enabled"] = new_state

                atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2))

                status = "включена" if new_state else "отключена"
                await message.answer(f"📅 Ежедневная рассылка отчетов в полночь <b>{status}</b>.")
                return
            elif subcommand == "users":
                report = collect_users_summary(base_path)
                await message.answer(report or "👥 Информация о пользователях не найдена.")
                return

        # Стандартное поведение (без аргументов или неизвестный аргумент) — отчет по активности
        report = collect_daily_report(base_path)
        if report:
            await message.answer(report)
        else:
            await message.answer("📊 Активности за последние 24 часа не обнаружено.")



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
        # Сначала ищем точное совпадение очищенных имен (для уникальности)
        targets = [f for f in files_data if _clean_filename(f.get("original_name", "")) == filename]
        if not targets:
            # Если точного нет, ищем вхождения (подстроку)
            targets = [f for f in files_data if filename in _clean_filename(f.get("original_name", ""))]

        if not targets:
            await message.answer(f"❌ Файл <code>{filename}</code> не найден в вашем списке.")
            return

        # Сохраняем очередь на удаление
        pending_deletions[message.from_user.id] = {"targets": targets, "index": 0}
        await _ask_deletion_confirmation(message, targets[0], 0, len(targets))


    async def _ask_deletion_confirmation(message: Message, target: dict, idx: int, total: int):
        """Вспомогательная функция для запроса подтверждения удаления."""
        size = format_size(target.get("size", 0))
        date_str = _format_date(target.get("upload_date", ""))
        icon = _get_file_icon(target.get("stored_name", target['original_name']))

        counter = f" (файл {idx + 1} из {total})" if total > 1 else ""
        next_info = "\nЛюбое другое сообщение перейдет к следующему файлу." if idx < total - 1 else "\nЛюбое другое сообщение отменит удаление."

        await message.answer(
            f"❓ <b>Подтвердите удаление файла{counter}:</b>\n\n"
            f"<code>{icon} {_wrap_filename(target['stored_name'], indent='   ')}</code>\n"
            f"📅 {date_str} | 💾 {size}\n\n"
            f"Для удаления отправьте: <b>да</b>, <b>yes</b> или <b>так</b>.\n"
            f"{next_info}"
        )
        user_dir = await ensure_user_dir(message.from_user, create=False)
        log_user_action(user_dir, "bot_response", {"type": "delete_confirmation_request", "file": target['stored_name']})

    @dp.message(F.document | F.audio | F.video | F.voice | F.sticker | F.video_note | F.photo)
    async def incoming_files_handler(message: Message) -> None:
        """Обработчик входящих файлов и медиа."""
        if not message.from_user:
            return

        user_id = message.from_user.id
        cancelled_delete_target_name = None

        # Отменяем ожидание удаления, если пользователь прислал файл,
        # но сообщение об отмене отправим позже.
        if user_id in pending_deletions:
            state = pending_deletions.pop(user_id)
            # Берем имя первого файла из очереди для уведомления об отмене (без расширения)
            cancelled_delete_target_name = state["targets"][0]["original_name"]
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
            if not user_dir or not user_dir.exists():
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

                    old_icon = _get_file_icon(duplicate_info.get("stored_name", duplicate_info['original_name']))
                    new_icon = _get_file_icon(file_info.get("stored_name", file_info['original_name']))
                    await message.answer(
                        f"⚠️ Файл с таким именем уже был:\n"
                        f"<code>{old_icon} {_wrap_filename(duplicate_info['original_name'], indent='   ')}</code>\n"
                        f"� {dup_date} | 💾 {dup_size}\n\n"
                        f"✅ Новый файл сохранен под именем:\n"
                        f"<code>{new_icon} {_wrap_filename(file_info['original_name'], indent='   ')}</code>\n"
                        f" {new_date} | 💾 {new_size}"
                    )
                    log_user_action(user_dir, "bot_response", {"type": "file_saved_duplicate", "name": file_info['original_name']})
                else:
                    # Добавляем размер сохраненного файла
                    await message.answer(f"✅ Файл сохранен. 💾{format_size(file_info['size'])}")
                    log_user_action(user_dir, "bot_response", {"type": "file_saved", "name": file_info['original_name']})

                # Отправляем сообщение об отмене удаления, если оно было
                if cancelled_delete_target_name:
                    await message.answer(f"🚫 Удаление файлов <code>{cancelled_delete_target_name}</code> отменено.")
                    log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_media"})

            except (PermissionError, OSError) as e:
                # Вывод понятного сообщения пользователю при проблемах с местом
                await message.answer(f"⚠️ {e}")
                log_user_action(user_dir, "bot_response", {"type": "storage_error", "details": str(e)})
            except Exception as e:
                logger.exception("Ошибка при сохранении файла:")
                await message.answer(f"⚠️ Ошибка при сохранении: {type(e).__name__}: {e}")
            return

        # Если это не медиа-файл, но был pending_deletions, то отменяем и сообщаем
        elif cancelled_delete_target_name:
            user_dir = await ensure_user_dir(message.from_user, create=False)
            if user_dir:
                 log_user_action(user_dir, "user_action_cancelled_delete", {"reason": "unexpected_media"})
                 await message.answer(f"🚫 Удаление файлов <code>{cancelled_delete_target_name}</code> отменено.")
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
            state = pending_deletions[user_id]
            targets = state["targets"]
            idx = state["index"]
            target = targets[idx]

            if user_text in ["да", "yes", "так"]:
                pending_deletions.pop(user_id)
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

                    await message.answer(f"✅ Файл <code>{target['stored_name']}</code> удален.")
                    log_user_action(user_dir, "bot_response", {"type": "delete_success", "file": target['stored_name']})

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
                # Переходим к следующему файлу в очереди или отменяем
                state["index"] += 1
                if state["index"] < len(targets):
                    await _ask_deletion_confirmation(message, targets[state["index"]], state["index"], len(targets))
                else:
                    pending_deletions.pop(user_id)
                    if user_dir:
                        # Используем stem от первого файла для сообщения об отмене всей группы
                        group_name = targets[0]['original_name']
                        await message.answer(f"🚫 Удаление файлов <code>{group_name}</code> отменено.")
                        log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_text"})
                return

        # 2. Проверяем, не является ли текст ссылкой для скачивания
        url = None
        # Пытаемся найти ссылку через сущности Telegram (самый точный метод)
        if message.entities:
            for entity in message.entities:
                if entity.type == "url":
                    url = message.text[entity.offset : entity.offset + entity.length]
                    break
                elif entity.type == "text_link":
                    url = entity.url
                    break

        # Если сущности не дали результата, используем регулярное выражение
        if not url:
            url_match = re.search(r'(https?://[^\s]+)', message.text)
            if url_match:
                url = url_match.group(1)

        if url:
            if not user_dir:
                await message.answer("Пожалуйста, сначала отправьте /start.")
                return

            status_msg = await message.answer("⏳ Скачиваю файл по ссылке...")
            try:
                file_info = await download_file_from_url(url, user_dir)

                icon = _get_file_icon(file_info.get("stored_name", file_info['original_name']))
                await status_msg.edit_text(
                    f"✅ Файл успешно скачан и сохранен!\n\n"
                    f"{icon} <b>Имя:</b> <code>{file_info['original_name']}</code>\n"
                    f"💾 <b>Размер:</b> {format_size(file_info['size'])}"
                )
                log_user_action(user_dir, "url_download_success", {"url": url, "file": file_info['original_name']})
                return
            except (PermissionError, ValueError) as e:
                await status_msg.edit_text(f"⚠️ {e}")
                return
            except Exception as e:
                await status_msg.edit_text(f"❌ Произошла ошибка при скачивании: {type(e).__name__}")
                return

        # 3. Пытаемся найти файл по имени в директории пользователя
        if user_dir:
            cleaned_name = _clean_filename(message.text)
            files_data = get_user_files(user_dir)
            # 1. Сначала ищем точное совпадение (приоритет уникальности)
            targets = [f for f in files_data if _clean_filename(f.get("original_name", "")) == cleaned_name]
            if not targets:
                # 2. Если точного нет, ищем вхождения (подстроку)
                targets = [f for f in files_data if cleaned_name in _clean_filename(f.get("original_name", ""))]

            if targets:
                for target in targets:
                    file_path = user_dir / target["stored_name"]
                    if file_path.exists():
                        try:
                            # Убираем только технический префикс.
                            # Расширение оставляем, так как оно необходимо Telegram для генерации превью (thumbnail).
                            clean_name = re.sub(r'^(fwd|upl|dwn|fnd)_', '', target["original_name"])

                            # Если в визуальном имени нет расширения, добавляем его из физического для корректной отправки
                            ext = Path(target["stored_name"]).suffix
                            if ext and not clean_name.lower().endswith(ext.lower()):
                                clean_name += ext

                            icon = _get_file_icon(target.get("stored_name", target['original_name']))
                            await message.answer_document(
                                document=FSInputFile(path=file_path, filename=clean_name),
                                caption=f"{icon} Файл: <code>{target['stored_name']}</code>"
                            )
                            log_user_action(user_dir, "bot_response", {"type": "send_file", "file": target['original_name']})
                        except Exception as e:
                            logger.error(f"Ошибка при отправке документа {target['original_name']}: {e}")
                    else:
                        await message.answer(f"⚠️ Файл <code>{target['original_name']}</code> не найден на диске.")
                return

        # 3. Если файл не найден или произошла ошибка — обычное эхо
        try:
            await message.answer(f"📢 <b>Эхо:</b> {message.text}")
            if user_dir:
                log_user_action(user_dir, "bot_response", {"type": "echo", "text": message.text})
        except Exception:
            await message.answer("Я принимаю только файлы или имена ваших файлов.")

    return dp
