import aiohttp
import asyncio
import ipaddress
import mimetypes
import shutil
import re
import socket
from email.message import Message
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, unquote, urljoin
from uuid import uuid4

from config import MAX_FILE_SIZE, BOT_TOTAL_DATA_LIMIT, MAX_DISPLAY_NAME_LEN, logger
from utils import (
    normalize_filename, unique_path, append_file_data, get_dir_size,
    shorten_name, locked_file_data, load_json_list_safe
)
from ui_formatter import format_size


# SSRF protection:
# Blocks URL downloads from localhost, private networks and other non-public IP ranges.
# If the bot intentionally works with local-network URLs, set this to False to restore
# the previous permissive behavior.
SSRF_PROTECTION_ENABLED = True
MAX_REDIRECTS = 5
MAX_EXTENSION_LEN = 10
GENERIC_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream"}
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}


async def ensure_public_download_url(url: str) -> None:
    """
    Проверить, что URL ведет на публичный сетевой адрес.

    Это защита от SSRF: пользовательская ссылка не должна заставлять сервер бота
    обращаться к localhost, приватной сети, link-local адресам или служебным
    metadata endpoints. Если бот должен скачивать файлы из локальной сети,
    отключите SSRF_PROTECTION_ENABLED выше.
    """
    if not SSRF_PROTECTION_ENABLED:
        return

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("В ссылке не найдено имя хоста.")

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        addr_info = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise ValueError("Не удалось определить IP-адрес хоста в ссылке.")

    checked_ips = set()
    for item in addr_info:
        ip_raw = item[4][0]
        if ip_raw in checked_ips:
            continue
        checked_ips.add(ip_raw)

        ip = ipaddress.ip_address(ip_raw)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("Скачивание с локальных или служебных сетевых адресов запрещено.")


async def get_with_checked_redirects(session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientResponse:
    """Выполнить GET, проверяя SSRF-правила для исходного URL и каждого redirect."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        await ensure_public_download_url(current_url)
        response = await session.get(current_url, timeout=timeout, allow_redirects=False)
        if response.status not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("Location")
        response.release()
        if not location:
            raise ValueError("Сервер вернул redirect без Location.")

        current_url = urljoin(current_url, location)

    raise ValueError(f"Слишком много перенаправлений при скачивании файла (лимит: {MAX_REDIRECTS}).")


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


def extract_filename_from_content_disposition(content_disposition: str) -> str | None:
    """Извлечь имя файла из Content-Disposition стандартным parser'ом."""
    message = Message()
    message["Content-Disposition"] = content_disposition
    filename = message.get_filename()
    if not filename:
        return None
    return unquote(filename)


def extension_from_content_type(content_type: str) -> str:
    """Вернуть файловое расширение по Content-Type, если тип известен."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type or media_type in GENERIC_CONTENT_TYPES:
        return ""

    ext = CONTENT_TYPE_EXTENSIONS.get(media_type) or mimetypes.guess_extension(media_type) or ""
    if ext == ".jpe":
        return ".jpg"
    return ext


def filename_has_likely_extension(file_name: str) -> bool:
    """Проверить, что имя заканчивается на похожее расширение, а не на хвост ID."""
    suffix = Path(file_name).suffix
    return bool(suffix and 1 < len(suffix) <= MAX_EXTENSION_LEN and re.fullmatch(r"\.[A-Za-z0-9]+", suffix))


def add_extension_from_content_type(file_name: str, content_type: str) -> str:
    """Добавить расширение из Content-Type, если в имени его нет."""
    if filename_has_likely_extension(file_name):
        return file_name

    ext = extension_from_content_type(content_type)
    if not ext:
        return file_name

    return f"{file_name}{ext}"


def extension_from_file_signature(file_path: Path) -> str:
    """Определить расширение по первым байтам файла, если HTTP-заголовки бесполезны."""
    header = file_path.read_bytes()[:16]
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"%PDF-"):
        return ".pdf"
    if header.startswith(b"PK\x03\x04"):
        return ".zip"
    if header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"ID3"):
        return ".mp3"
    return ""


def add_extension_from_file_signature(file_name: str, file_path: Path) -> str:
    """Добавить расширение по сигнатуре скачанного файла, если в имени его нет."""
    if filename_has_likely_extension(file_name):
        return file_name

    ext = extension_from_file_signature(file_path)
    if not ext:
        return file_name

    return f"{file_name}{ext}"


def display_name_from_download_name(file_name: str) -> str:
    """Вернуть имя для списка файлов без последнего расширения."""
    if filename_has_likely_extension(file_name):
        return Path(file_name).stem
    return file_name


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
            async with await get_with_checked_redirects(session, url, timeout) as response:
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
                current_usage = await asyncio.to_thread(get_dir_size, base_data_dir)
                if file_size_header and current_usage + file_size_header > BOT_TOTAL_DATA_LIMIT:
                    raise PermissionError("Превышен общий лимит хранилища бота.")

                # 2. Определение имени файла
                original_name = None
                cd = response.headers.get('Content-Disposition')
                if cd:
                    original_name = extract_filename_from_content_disposition(cd)

                if not original_name:
                    # Если в заголовках нет, берем из пути URL
                    parsed_url = urlparse(url)
                    original_name = unquote(Path(parsed_url.path).name)

                if not original_name or original_name == '.':
                    original_name = datetime.now().strftime('%Y%m%d_%H%M%S')

                original_name = add_extension_from_content_type(original_name, content_type)

                files_data_path = destination_dir / "files_data.json"
                async with locked_file_data(files_data_path):
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    tmp_path = destination_dir / f"{uuid4().hex}.download"

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
                    if not file_size_header:
                        current_usage = await asyncio.to_thread(get_dir_size, base_data_dir)
                    if not file_size_header and current_usage + downloaded_size > BOT_TOTAL_DATA_LIMIT:
                        tmp_path.unlink(missing_ok=True)
                        raise PermissionError("Превышен общий лимит хранилища бота.")

                    original_name = add_extension_from_file_signature(original_name, tmp_path)

                    existing_files = load_json_list_safe(files_data_path)
                    existing_visual_names = [f.get("original_name") for f in existing_files]

                    # В отображаемом имени расширение скрываем. Тип файла виден по иконке.
                    display_name = shorten_name(
                        f"dwn_{display_name_from_download_name(original_name)}",
                        MAX_DISPLAY_NAME_LEN,
                        existing_visual_names,
                    )

                    storage_name = shorten_name(f"dwn_{original_name}", MAX_DISPLAY_NAME_LEN)
                    final_name = normalize_filename(storage_name)
                    final_path = unique_path(destination_dir / final_name)
                    shutil.move(str(tmp_path), str(final_path))

                    file_info = {
                        "original_name": display_name,
                        "stored_name": final_path.name,
                        "upload_date": datetime.now().isoformat(),
                        "size": downloaded_size
                    }
                    append_file_data(files_data_path, file_info)
                    return file_info

        except asyncio.TimeoutError:
            raise ValueError("Сервер не ответил вовремя или соединение прервано (тайм-аут).")
        except Exception as e:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            logger.error(f"Ошибка при загрузке URL {url}: {e}")
            raise
