import asyncio
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from aiogram import Dispatcher, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto, InputMediaVideo

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, MAX_DISPLAY_NAME_LEN, ADMIN_ID, FILE_ICONS, logger, get_base_path
from file_handler import save_incoming_file, get_user_files
from utils import (
    atomic_write_text, get_dir_size,
    load_json_safe, shorten_name, locked_file_data,
    async_log_user_action
)
from ui_formatter import (
    format_size, format_date, wrap_filename, get_file_icon,
    format_saved_file_message, strip_display_extension, format_media_mode_message,
    format_preview_caption
)
from reporting import collect_daily_report, collect_users_summary
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


def build_dispatcher() -> Dispatcher:
    """Построить диспетчер с обработчиками."""
    dp = Dispatcher()
    # Словари для хранения состояния бота
    user_status_msgs: dict[int, int] = {}
    pending_deletions: dict[int, dict] = {}
    user_media_modes: dict[int, bool] = {}
    FILES_PER_PAGE = 10


    async def scan_and_fix_files(user_dir: Path) -> None:
        """
        Синхронизирует индекс файлов с реальным содержимым папки:
        1. Удаляет записи о файлах, которые физически отсутствуют.
        2. Проверяет и исправляет отображаемые имена существующих записей (длина и уникальность).
        3. Добавляет новые файлы, найденные в директории.
        """
        files_data_path = user_dir / "files_data.json"
        async with locked_file_data(files_data_path):
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
                old_display_name = strip_display_extension(old_display_name, stored_name)

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
                    # Сохраняем исходный технический префикс, если он уже есть.
                    clean_base = file_path.stem
                    display_prefix = "fnd_"
                    m = re.match(r'^(fwd|upl|dwn|fnd)_', clean_base)
                    if m:
                        display_prefix = m.group(0)

                    # Убираем ВСЕ технические префиксы из имени файла на диске
                    # только из полезной части имени.
                    while True:
                        m = re.match(r'^(fwd|upl|dwn|fnd)_', clean_base)
                        if not m:
                            break
                        clean_base = clean_base[len(m.group(0)):]

                    # Генерируем уникальное сокращенное имя (не более MAX_DISPLAY_NAME_LEN)
                    new_display_name = shorten_name(f"{display_prefix}{clean_base}", MAX_DISPLAY_NAME_LEN, seen_visual_names)
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


    def _is_photo(stored_name: str) -> bool:
        return Path(stored_name).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')

    def _is_video(stored_name: str) -> bool:
        return Path(stored_name).suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv', '.3gp')

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

        # 7. Удаляем маркеры уникальности (..1..) и сокращения (....), созданные shorten_name
        text = re.sub(r'\.\.\d+\.\.|\.\.\.\.', '', text)

        # 8. Удаляем все пробелы, табы и переносы строк, приводим к нижнему регистру.
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
        total_all_size = await asyncio.to_thread(get_dir_size, base_data_dir)
        remaining = BOT_TOTAL_DATA_LIMIT - total_all_size
        remaining_str = format_size(max(0, remaining)) if remaining >= 0 else "<i><u>Лимит превышен</u></i>"

        header = (f"<b>Размер каталога: {format_size(total_user_size)}. Свободно: {remaining_str}</b>\n"
                  f"<b>Список файлов (стр. {page}/{total_pages}):</b>\n\n")

        lines = []
        for i, file_info in enumerate(page_files, start_idx + 1):
            name = file_info.get("original_name", "Unknown")
            size = format_size(file_info.get("size", 0))
            date_str = format_date(file_info.get("upload_date", ""))
            # Определяем иконку по физическому имени, так как в визуальном расширения может не быть
            icon = get_file_icon(file_info.get("stored_name", name))

            # Отображаем имя целиком (оно уже сокращено функцией shorten_name и содержит уникальные индексы)
            prefix = f"{i}. {icon} "
            # Динамический отступ, чтобы имя во второй строке было ровно под именем в первой
            wrapped_name = wrap_filename(name, indent=" " * len(prefix))
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
        # Сбрасываем режим медиа при запуске
        user_media_modes[message.from_user.id] = False
        # Сканируем при запуске
        await scan_and_fix_files(user_dir)

        await async_log_user_action(user_dir, "user_command", {"command": "/start"})

        # Получаем обновленный список файлов
        files = get_user_files(user_dir)
        total_size = sum(f.get("size", 0) for f in files)

        welcome_text = get_welcome_message(user_dir.name, format_size(total_size), len(files))
        await message.answer(welcome_text)

        await notify_admin(message.bot, f"👤 Пользователь {message.from_user.full_name} (@{message.from_user.username}) подключился.")
        await async_log_user_action(user_dir, "bot_response", {"type": "welcome"})

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

        await async_log_user_action(user_dir, "user_command", {"command": "/status"})
        response, kb = await _get_status_content(user_dir, page=1)
        sent_message = await message.answer(response, reply_markup=kb)
        # Сохраняем ID сообщения
        user_status_msgs[message.from_user.id] = sent_message.message_id

        files = get_user_files(user_dir)
        total_size = sum(f.get("size", 0) for f in files)
        await async_log_user_action(user_dir, "bot_response", {
            "type": "status_summary",
            "files_count": len(files),
            "total_size": format_size(total_size)
        })

    @dp.message(Command("mediaon"))
    async def command_mediaon_handler(message: Message) -> None:
        """Включить мультимедийный режим выдачи файлов."""
        if not message.from_user:
            return
        user_dir = await ensure_user_dir(message.from_user, create=True)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return
        user_media_modes[message.from_user.id] = True
        await message.answer(format_media_mode_message(True))
        await async_log_user_action(user_dir, "user_command", {"command": "/mediaon"})

    @dp.message(Command("mediaoff"))
    async def command_mediaoff_handler(message: Message) -> None:
        """Выключить мультимедийный режим (по умолчанию — документы)."""
        if not message.from_user:
            return
        user_dir = await ensure_user_dir(message.from_user, create=True)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return
        user_media_modes[message.from_user.id] = False
        await message.answer(format_media_mode_message(False))
        await async_log_user_action(user_dir, "user_command", {"command": "/mediaoff"})

    PREVIEW_PER_PAGE = 10
    PREVIEW_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.mp4', '.mov', '.avi', '.mkv', '.3gp')

    def _get_preview_files(user_dir: Path) -> list[dict]:
        files = get_user_files(user_dir)
        return [f for f in files if Path(f["stored_name"]).suffix.lower() in PREVIEW_EXTENSIONS]

    def _is_video_file(stored_name: str) -> bool:
        return Path(stored_name).suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv', '.3gp')

    async def _send_preview_album(message: Message, user_dir: Path, page: int) -> None:
        preview_files = _get_preview_files(user_dir)
        if not preview_files:
            await message.answer("Нет файлов для превью (фото и видео).")
            return

        total_pages = (len(preview_files) + PREVIEW_PER_PAGE - 1) // PREVIEW_PER_PAGE
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * PREVIEW_PER_PAGE
        page_files = preview_files[start_idx:start_idx + PREVIEW_PER_PAGE]

        media_group = []
        for file_info in page_files:
            file_path = user_dir / file_info["stored_name"]
            if not file_path.exists():
                continue
            caption = format_preview_caption(file_info)
            if _is_video_file(file_info["stored_name"]):
                media_group.append(InputMediaVideo(media=FSInputFile(path=file_path), caption=caption))
            else:
                media_group.append(InputMediaPhoto(media=FSInputFile(path=file_path), caption=caption))

        if not media_group:
            await message.answer("Файлы не найдены на диске.")
            return

        await message.answer_media_group(media_group)

        buttons = []
        if page > 1:
            buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"preview_page:{page-1}"))
        if page < total_pages:
            buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"preview_page:{page+1}"))
        kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

        info_text = f"🖼️ Превью (стр. {page}/{total_pages}) — {len(preview_files)} файлов"
        await message.answer(info_text, reply_markup=kb)

    @dp.message(Command("preview"))
    async def command_preview_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_dir = await ensure_user_dir(message.from_user, create=False)
        if not user_dir:
            await message.answer("Пожалуйста, сначала отправьте /start.")
            return

        args = message.text.split()
        page = 1
        if len(args) > 1:
            try:
                page = int(args[1])
            except ValueError:
                pass

        await _send_preview_album(message, user_dir, page)
        await async_log_user_action(user_dir, "user_command", {"command": f"/preview {page}"})

    @dp.callback_query(F.data.startswith("preview_page:"))
    async def preview_pagination_handler(callback: CallbackQuery) -> None:
        user_dir = await ensure_user_dir(callback.from_user, create=False)
        if not user_dir:
            await callback.answer("Ошибка сессии. Введите /start", show_alert=True)
            return

        try:
            page = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            page = 1

        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

        await _send_preview_album(callback.message, user_dir, page)

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
                report = await asyncio.to_thread(collect_users_summary, base_path)
                await message.answer(report or "👥 Информация о пользователях не найдена.")
                return

        # Стандартное поведение (без аргументов или неизвестный аргумент) — отчет по активности
        report = await asyncio.to_thread(collect_daily_report, base_path)
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

        text, kb = await _get_status_content(user_dir, page)

        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            # Ошибка может возникнуть, если сообщение не изменилось
            pass
        await callback.answer()


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

        await async_log_user_action(user_dir, "user_command", {"command": f"/delete {filename}"})

        if not filename:
            await message.answer("❌ Указанное имя содержит только спецсимволы и не может быть использовано для поиска.")
            return

        files_data = get_user_files(user_dir)
        # Сначала ищем точное совпадение очищенных имен (для уникальности)
        targets = [f for f in files_data if _clean_filename(f.get("original_name", "")) == filename]
        if not targets:
            if len(filename) < 3:
                await message.answer("⚠️ Имя слишком короткое для поиска по частичному совпадению (нужно минимум 3 символа).")
                return
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
        date_str = format_date(target.get("upload_date", ""))
        icon = get_file_icon(target.get("stored_name", target['original_name']))

        counter = f" (файл {idx + 1} из {total})" if total > 1 else ""
        next_info = "\nЛюбое другое сообщение перейдет к следующему файлу." if idx < total - 1 else "\nЛюбое другое сообщение отменит удаление."

        await message.answer(
            f"❓ <b>Подтвердите удаление файла{counter}:</b>\n\n"
            f"<code>{icon} {wrap_filename(target['stored_name'], indent='   ')}</code>\n"
            f"📅 {date_str} | 💾 {size}\n\n"
            f"Для удаления отправьте: <b>да</b>, <b>yes</b> или <b>так</b>.\n"
            f"{next_info}"
        )
        user_dir = await ensure_user_dir(message.from_user, create=False)
        await async_log_user_action(user_dir, "bot_response", {"type": "delete_confirmation_request", "file": target['stored_name']})

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
            await async_log_user_action(user_dir, "user_upload", {"media_type": type(media_obj).__name__, "size": media_obj.file_size})

            try:
                file_info, duplicate_info = await save_incoming_file(message, None, user_dir)

                if duplicate_info:
                    await message.answer(format_saved_file_message(file_info))
                    await async_log_user_action(user_dir, "bot_response", {"type": "file_saved_duplicate", "name": file_info['original_name']})
                else:
                    await message.answer(format_saved_file_message(file_info))
                    await async_log_user_action(user_dir, "bot_response", {"type": "file_saved", "name": file_info['original_name']})

                # Отправляем сообщение об отмене удаления, если оно было
                if cancelled_delete_target_name:
                    await message.answer(f"🚫 Удаление файлов <code>{cancelled_delete_target_name}</code> отменено.")
                    await async_log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_media"})

            except (PermissionError, OSError) as e:
                # Вывод понятного сообщения пользователю при проблемах с местом
                await message.answer(f"⚠️ {e}")
                await async_log_user_action(user_dir, "bot_response", {"type": "storage_error", "details": str(e)})
            except Exception as e:
                logger.exception("Ошибка при сохранении файла:")
                await message.answer(f"⚠️ Ошибка при сохранении: {type(e).__name__}: {e}")
            return

        # Если это не медиа-файл, но был pending_deletions, то отменяем и сообщаем
        elif cancelled_delete_target_name:
            user_dir = await ensure_user_dir(message.from_user, create=False)
            if user_dir:
                await async_log_user_action(user_dir, "user_action_cancelled_delete", {"reason": "unexpected_media"})
                await message.answer(f"🚫 Удаление файлов <code>{cancelled_delete_target_name}</code> отменено.")
                await async_log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_media_generic"})


    @dp.message(F.text)
    async def text_echo_handler(message: Message) -> None:
        """Обработчик текстовых сообщений: выдача файла, подтверждение удаления или эхо."""
        if not message.from_user or not message.text:
            return

        user_id = message.from_user.id
        user_text = message.text.strip().lower()

        user_dir = await ensure_user_dir(message.from_user, create=False)
        if user_dir:
            await async_log_user_action(user_dir, "user_text", {"text": message.text})

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

                    async with locked_file_data(files_data_path):
                        # Удаляем физически
                        if file_path.exists():
                            file_path.unlink()

                        # Обновляем JSON (удаляем запись)
                        files_data = get_user_files(user_dir)
                        updated_data = [f for f in files_data if f.get("stored_name") != target["stored_name"]]
                        atomic_write_text(files_data_path, json.dumps(updated_data, ensure_ascii=False, indent=2))

                    await message.answer(f"✅ Файл <code>{target['stored_name']}</code> удален.")
                    await async_log_user_action(user_dir, "bot_response", {"type": "delete_success", "file": target['stored_name']})

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
                        await async_log_user_action(user_dir, "bot_response", {"type": "delete_cancelled_by_text"})
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

                await status_msg.edit_text(format_saved_file_message(file_info))
                await async_log_user_action(user_dir, "url_download_success", {"url": url, "file": file_info['original_name']})
                return
            except (PermissionError, ValueError) as e:
                await status_msg.edit_text(f"⚠️ {e}")
                return
            except Exception as e:
                await status_msg.edit_text(f"❌ Произошла ошибка при скачивании: {type(e).__name__}")
                return

        # 3. Пытаемся найти файл по имени в директории пользователя
        if user_dir:
            targets = []
            cleaned_name = _clean_filename(message.text)
            if cleaned_name:
                files_data = get_user_files(user_dir)

                # Ищем все файлы, которые содержат поисковый запрос.
                # Для очень коротких запросов (меньше 3 символов) оставляем только точное совпадение, чтобы избежать спама.
                if len(cleaned_name) < 3:
                    targets = [f for f in files_data if _clean_filename(f.get("original_name", "")) == cleaned_name]
                else:
                    targets = [f for f in files_data if cleaned_name in _clean_filename(f.get("original_name", ""))]
                    # Дополнительно можно отсортировать, чтобы точные совпадения шли первыми
                    targets.sort(key=lambda f: _clean_filename(f.get("original_name", "")) != cleaned_name)

            if targets:
                media_mode = user_media_modes.get(message.from_user.id, False)
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

                            icon = get_file_icon(target.get("stored_name", target['original_name']))
                            stored = target["stored_name"]
                            if media_mode and _is_photo(stored):
                                await message.answer_photo(
                                    photo=FSInputFile(path=file_path),
                                    caption=f"{icon} Файл: <code>{stored}</code>"
                                )
                            elif media_mode and _is_video(stored):
                                await message.answer_video(
                                    video=FSInputFile(path=file_path),
                                    caption=f"{icon} Файл: <code>{stored}</code>"
                                )
                            else:
                                await message.answer_document(
                                    document=FSInputFile(path=file_path, filename=clean_name),
                                    caption=f"{icon} Файл: <code>{stored}</code>"
                                )
                            await async_log_user_action(user_dir, "bot_response", {"type": "send_file", "file": target['original_name']})
                        except Exception as e:
                            logger.error(f"Ошибка при отправке документа {target['original_name']}: {e}")
                    else:
                        await message.answer(f"⚠️ Файл <code>{target['original_name']}</code> не найден на диске.")
                return

        # 3. Если файл не найден или произошла ошибка — обычное эхо
        try:
            await message.answer(f"📢 <b>Эхо:</b> {message.text}")
            if user_dir:
                await async_log_user_action(user_dir, "bot_response", {"type": "echo", "text": message.text})
        except Exception:
            await message.answer("Я принимаю только файлы или имена ваших файлов.")

    return dp
