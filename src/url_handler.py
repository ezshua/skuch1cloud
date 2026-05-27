import aiohttp
import asyncio
import shutil
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, unquote, urljoin

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, MAX_DISPLAY_NAME_LEN, logger
from utils import normalize_filename, unique_path, append_file_data, format_size, get_dir_size, shorten_name

def extract_og_image(html_text: str) -> str | None:
    """Извлечь URL изображения из мета-тегов Open Graph."""
    # Ищем og:image, поддерживая разные варианты атрибутов (property или name) и порядок
    patterns = [
        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']'
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

async def download_file_from_url(url: str, destination_dir: Path, is_retry: bool = False) -> dict:
    """
    Скачивает файл по ссылке и сохраняет его в директорию пользователя.

    Args:
        url (str): Прямая ссылка на файл.
        destination_dir (Path): Директория пользователя.

    Returns:
        dict: Информация о сохраненном файле.
    """
    tmp_path = None

    parsed_url = urlparse(url)
    if parsed_url.scheme not in ["http", "https"]:
        raise ValueError(f"Неподдерживаемый протокол: {parsed_url.scheme}. Поддерживаются только HTTP/HTTPS ссылки.")

    async with aiohttp.ClientSession() as session:
        try:
            # Настраиваем более детальные тайм-ауты
            timeout = aiohttp.ClientTimeout(
                total=300,      # Максимум 5 минут на всё
                connect=15,     # 15 секунд на попытку подключения
                sock_read=30    # 30 секунд ожидания новых данных из сокета
            )
            async with session.get(url, timeout=timeout) as response:
                if response.status != 200:
                    raise ValueError(f"Не удалось получить доступ к файлу (HTTP {response.status})")

                # Проверка типа контента: если это HTML, значит ссылка ведет на страницу или каталог
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    if is_retry:
                        raise ValueError("Не удалось найти файл по ссылке (даже в превью страницы).")

                    # Пытаемся вытащить картинку из превью
                    html_text = await response.text(errors='ignore')
                    preview_url = extract_og_image(html_text)

                    if preview_url:
                        # Превращаем относительную ссылку в абсолютную
                        full_preview_url = urljoin(url, preview_url)
                        return await download_file_from_url(full_preview_url, destination_dir, is_retry=True)
                    else:
                        raise ValueError("Указанная ссылка ведет на веб-страницу без превью-изображения. "
                                         "Пожалуйста, предоставьте прямую ссылку на скачивание.")

                # 1. Предварительная проверка размера по заголовкам
                content_length = response.headers.get('Content-Length')
                file_size_header = int(content_length) if content_length and content_length.isdigit() else 0

                if file_size_header > MAX_FILE_SIZE:
                    raise PermissionError(f"Файл слишком большой ({format_size(file_size_header)}). Лимит: {format_size(MAX_FILE_SIZE)}")

                # Проверка квоты хранилища
                base_data_dir = destination_dir.parent
                if file_size_header and get_dir_size(base_data_dir) + file_size_header > BOT_TOTAL_DATA_LIMIT:
                    raise PermissionError("Превышен общий лимит хранилища бота.")

                # 2. Определение имени файла
                original_name = None
                cd = response.headers.get('Content-Disposition')
                if cd:
                    # Пытаемся вытащить filename из Content-Disposition
                    names = re.findall(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';]+)["\']?', cd)
                    if names:
                        original_name = unquote(names[0])

                if not original_name:
                    # Если в заголовках нет, берем из пути URL
                    parsed_url = urlparse(url)
                    original_name = unquote(Path(parsed_url.path).name)

                if not original_name or original_name == '.':
                    original_name = datetime.now().strftime('%Y%m%d_%H%M%S')

                # Применяем префикс URL и сокращаем имя для отображения
                display_name = shorten_name(f"dwn_{original_name}", MAX_DISPLAY_NAME_LEN)

                final_name = normalize_filename(display_name)
                tmp_path = destination_dir / f"{final_name}.download"

                destination_dir.mkdir(parents=True, exist_ok=True)

                # 3. Скачивание с контролем размера в процессе
                downloaded_size = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        downloaded_size += len(chunk)
                        if downloaded_size > MAX_FILE_SIZE:
                            tmp_path.unlink(missing_ok=True)
                            raise PermissionError(f"Файл превысил лимит {format_size(MAX_FILE_SIZE)} во время загрузки.")
                        f.write(chunk)

                # Финальная проверка квоты
                if not file_size_header and get_dir_size(base_data_dir) + downloaded_size > BOT_TOTAL_DATA_LIMIT:
                    tmp_path.unlink(missing_ok=True)
                    raise PermissionError("Превышен общий лимит хранилища бота.")

                final_path = unique_path(destination_dir / final_name)
                shutil.move(str(tmp_path), str(final_path))

                file_info = {
                    "original_name": display_name,
                    "stored_name": final_path.name,
                    "upload_date": datetime.now().isoformat(),
                    "size": downloaded_size
                }
                append_file_data(destination_dir / "files_data.json", file_info)
                return file_info

        except asyncio.TimeoutError:
            raise ValueError("Сервер не ответил вовремя или соединение прервано (тайм-аут).")
        except Exception as e:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            logger.error(f"Ошибка при загрузке URL {url}: {e}")
            raise e
