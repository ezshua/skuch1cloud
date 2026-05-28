# Code Review: skuch1cloud

**Проект:** Telegram-бот для хранения файлов пользователей  
**Стек:** Python 3.10+, aiogram 3.x, aiohttp, python-dotenv  
**Дата ревью:** 2026-05-27  
**Ревьюер:** Senior-уровень, асинхронный Python / Telegram-боты  

---

## Общая оценка

Проект написан аккуратно, имеет понятную цель и в целом работоспособную архитектуру. Видно, что автор разобрался с aiogram 3, умеет писать атомарные операции с файлами, думает об edge-кейсах (дубликаты, квоты, тайм-ауты). Вместе с тем есть ряд серьёзных архитектурных и инженерных проблем, которые на продакшене приведут к багам, утечкам состояния и сложности поддержки.

**Итоговая оценка:** 5.5 / 10

---

## Критические проблемы

### 1. Состояние диспетчера — in-memory, не масштабируется и не персистентно

**Файл:** `handlers.py`, функция `build_dispatcher()`

```python
def build_dispatcher() -> Dispatcher:
    user_status_msgs: dict[int, int] = {}
    pending_deletions: dict[int, dict] = {}
    ...
```

`user_status_msgs` и `pending_deletions` живут как замыкания внутри функции. При перезапуске бота все незавершённые сценарии (ожидание подтверждения удаления) — обнуляются. Пользователь видел вопрос «удалить файл?», бот перезапустился, пользователь написал «да» — и это «да» попадёт в эхо или поиск файла.

**Рекомендация:** Использовать `aiogram.fsm` (встроенный FSM с хранилищем). Для простого случая подойдёт `MemoryStorage`, для продакшена — `RedisStorage`. Это стандартный паттерн aiogram 3.

```python
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class DeleteStates(StatesGroup):
    waiting_confirmation = State()
```

---

### 2. Гонка данных при параллельных загрузках (race condition)

**Файл:** `file_handler.py`, функция `save_incoming_file()`

Последовательность операций:
1. Читаем `files_data.json` → получаем список имён
2. Генерируем `display_name` с проверкой на уникальность
3. Скачиваем файл (может занять десятки секунд)
4. Записываем в `files_data.json`

Если пользователь загружает два файла одновременно (например, альбом в Telegram), оба потока прочитают одинаковый список на шаге 1, и оба получат одинаковый `display_name`. В итоге в JSON окажутся две записи с одинаковым именем.

**Рекомендация:** Нужен asyncio-лок на уровне пользователя для операции «прочитать → добавить → записать». Например:

```python
_user_file_locks: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_file_locks:
        _user_file_locks[user_id] = asyncio.Lock()
    return _user_file_locks[user_id]
```

---

### 3. Синхронный I/O в async-контексте блокирует event loop

**Файлы:** `utils.py`, `file_handler.py`, `users.py`

Практически весь файловый I/O выполняется синхронно: `path.read_text()`, `path.write_text()`, `json.loads()`, `path.stat()`, `path.exists()`, `path.iterdir()`, `get_dir_size()` (рекурсивный обход директории).

Функция `get_dir_size()` при большом количестве файлов может блокировать event loop на сотни миллисекунд, замораживая всех пользователей.

```python
# utils.py — синхронный рекурсивный обход
def get_dir_size(path: Path) -> int:
    total_size = 0
    if path.is_dir():
        for entry in path.iterdir():   # <-- блокирует event loop
            ...
```

**Рекомендация:** Оборачивать тяжёлые файловые операции в `asyncio.to_thread()`:

```python
total_size = await asyncio.to_thread(get_dir_size, base_data_dir)
```

Или использовать `aiofiles` для операций чтения/записи. Лёгкие операции (проверка существования, stat небольшого файла) допустимо оставить синхронными.

---

### 4. Небезопасная обработка `raise e` вместо `raise`

**Файл:** `file_handler.py`, `url_handler.py`

```python
except Exception as e:
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    raise e   # <-- теряется оригинальный traceback
```

`raise e` пересоздаёт исключение с новым traceback, указывающим на строку `raise e`, а не на место где оно возникло. Это существенно затрудняет отладку.

**Рекомендация:** Использовать голый `raise`:

```python
except Exception:
    if tmp_path and tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    raise
```

---

## Архитектурные замечания

### 5. `handlers.py` — God Object / нарушение принципа единственной ответственности

Файл содержит 400+ строк: диспетчер, хранение состояния, форматирование, логика удаления, пагинация, поиск файлов, скачивание URL, эхо-режим — всё в одном файле через замыкания. Это делает код трудно тестируемым и сложным для поддержки.

**Рекомендация:** Разбить на модули:
- `handlers/files.py` — загрузка и отдача файлов
- `handlers/commands.py` — /start, /status, /delete, /report
- `handlers/admin.py` — команды администратора
- `services/file_service.py` — бизнес-логика работы с файлами
- `formatters.py` — форматирование сообщений

---

### 6. Вложенные функции внутри `build_dispatcher()` — анти-паттерн

Все хендлеры (`command_start_handler`, `incoming_files_handler` и т.д.) определены как вложенные функции. Это сделано ради доступа к `user_status_msgs` и `pending_deletions`, но порождает ряд проблем:

- Невозможно написать unit-тесты на отдельные хендлеры
- IDE плохо анализирует вложенный код (автодополнение, рефакторинг)
- При росте функциональности файл становится неуправляемым

**Рекомендация:** Перейти на `Router` из aiogram 3 и передавать зависимости через `middleware` или `bot.workflow_data`.

---

### 7. Логика ротации лога — потенциальная потеря данных

**Файл:** `utils.py`, функция `log_user_action()`

```python
if log_path.exists() and log_path.stat().st_size > LOG_FILE_SIZE_LIMIT:
    if data:
        remove_count = max(1, len(data) // 50)  # 2%
        data = data[remove_count:]
```

При размере лога 5 МБ с тысячами мелких записей удаляется лишь 2% (около 20 записей), тогда как следующий вызов снова проверит размер и снова удалит 2%. Это O(N) операций для поддержания лога в пределах лимита.

**Рекомендация:** Использовать стандартный `logging.handlers.RotatingFileHandler` или при достижении лимита обрезать до 80% от лимита за один раз. Также рассмотреть SQLite вместо JSON для лога — это даст запросы по дате без загрузки всего файла в память.

---

### 8. `collect_daily_report()` и `collect_users_summary()` — N+1 проблема

**Файл:** `utils.py`

Функция итерирует по всем пользователям, для каждого читает `action_log.json` и `files_data.json` с диска, плюс вызывает `get_dir_size()` (рекурсивный обход). При 100 пользователях — 200+ синхронных файловых операций в одном вызове.

**Рекомендация:** Вести агрегированную статистику инкрементально (например, в `bot_state.json` или SQLite) вместо пересчёта с нуля каждый раз.

---

## Замечания по коду и стилю

### 9. `texts.py` содержит функцию, а не тексты

**Файл:** `texts.py`

В файле находится функция `get_welcome_message()`. Название файла подразумевает константы/шаблоны, но там бизнес-логика. Плюс в `url_handler.py` обнаружена функция `get_welcome_message` — по всей видимости, из `texts.py` она была вырезана и случайно добавлена в конец `url_handler.py` (или это баг импорта).

**Проверить:** В `handlers.py` есть `from texts import get_welcome_message`, но функция физически находится в конце `url_handler.py`. Если `texts.py` пустой или не содержит этой функции — это скрытый баг, который проявится только при вызове `/start`.

---

### 10. Отсутствие валидации `ADMIN_ID`

**Файл:** `config.py`

```python
ADMIN_ID = int(os.getenv("ADMIN_ACCOUNT_ID", 0))
```

Значение `0` используется как sentinel для «нет администратора», но `int("0")` — это валидный Telegram user ID (теоретически). Лучше использовать `None`:

```python
_admin_raw = os.getenv("ADMIN_ACCOUNT_ID")
ADMIN_ID: int | None = int(_admin_raw) if _admin_raw else None
```

---

### 11. Отсутствуют type hints в нескольких местах

Местами type hints есть, местами — нет. Например, `_extract_metadata()` возвращает `dict` без уточнения структуры. Рекомендуется использовать `TypedDict` для словарей с фиксированной структурой:

```python
from typing import TypedDict

class FileMetadata(TypedDict):
    forward_from: str | None
    forward_date: datetime | None
    caption: str | None
```

---

### 12. Магические строки вместо констант/Enum

По всему коду разбросаны строковые литералы для типов действий и статусов:

```python
log_user_action(user_dir, "bot_response", {"type": "file_saved"})
log_user_action(user_dir, "user_upload", ...)
log_user_action(user_dir, "url_download_success", ...)
```

Опечатка в любом месте не вызовет ошибку, но сломает аналитику. Рекомендуется использовать `Enum` или константы:

```python
class ActionType(str, Enum):
    BOT_RESPONSE = "bot_response"
    USER_UPLOAD = "user_upload"
    URL_DOWNLOAD = "url_download_success"
```

---

### 13. Пустые строки между функциями — непоследовательно

PEP 8 требует двух пустых строк между функциями верхнего уровня. В `utils.py` и `file_handler.py` встречается и одна, и две, и три пустые строки.

---

### 14. Комментарий в коде указывает на незаконченное решение

**Файл:** `config.py`

```python
# Если вы хотите лимит на пользователя, это будет сложнее, так как директории пользователей могут быть разных размеров.
# Для простоты пока общий лимит.
```

Комментарии вида «пока» / «для простоты» — технический долг, который надо трекать в issue tracker, а не в коде. В production-коде такие комментарии накапливаются и дезориентируют новых разработчиков.

---

### 15. Закомментированный код в `handlers.py`

```python
# log_user_action(user_dir, "user_interaction", {"action": "pagination", "page": page})
# files = get_user_files(user_dir)
# total_size = sum(...)
```

Закомментированный код нужно удалять — для этого есть git history. Если код временно отключён с намерением вернуть — добавить `TODO` комментарий с объяснением.

---

## Вопросы безопасности

### 16. Путь к данным задаётся через env и resolve() — потенциальный path traversal

**Файл:** `config.py`

```python
return Path(os.getenv("BOT_DATA_PATH", "data")).resolve()
```

Если злоумышленник сможет повлиять на `BOT_DATA_PATH` (например, через скомпрометированный `.env`), `resolve()` даст абсолютный путь за пределами ожидаемой директории. Нужна проверка, что путь находится внутри ожидаемого корня.

### 17. Имена файлов из URL не проходят полную проверку на traversal

**Файл:** `url_handler.py`

```python
original_name = unquote(Path(parsed_url.path).name)
```

`Path(...).name` отрезает путь, поэтому прямого traversal нет. Но после `unquote` может получиться имя вида `../../etc/passwd` если `name` содержит encoded слэши. `normalize_filename()` обрабатывает это, но важно убедиться, что финальный путь всегда проверяется через `is_relative_to(destination_dir)`.

---

## Положительные моменты

Стоит отметить, что в проекте есть хорошие решения, заслуживающие похвалы:

- **`atomic_write_text()`** — правильное использование временного файла с `replace()` для атомарной записи. Хорошая практика.
- **Тайм-ауты при скачивании** — и для Telegram API (`asyncio.wait_for`), и для HTTP (`aiohttp.ClientTimeout`). Многие боты забывают об этом.
- **Контроль размера файла в процессе скачивания** — потоковая проверка `downloaded_size > MAX_FILE_SIZE` предотвращает исчерпание диска.
- **`shorten_name()` с уникальностью** — нетривиальная функция, реализована аккуратно.
- **`_ensure_env_populated()`** — удобная DX-фича для первого запуска, хорошо задокументирована.
- **`scan_and_fix_files()`** — самовосстановление индекса при расхождении с диском — зрелый подход.
- **Пагинация в `/status`** — сделана через inline-кнопки, что правильно.

---

## Приоритизированный план улучшений

**Срочно (блокируют корректность работы):**
1. Заменить `user_status_msgs` / `pending_deletions` на FSM aiogram — устранит потерю состояния при рестарте
2. Добавить asyncio-лок на операции с `files_data.json` — устранит race condition
3. Исправить `raise e` → `raise` — восстановить трассировку ошибок
4. Проверить, что `get_welcome_message` корректно импортируется из `texts.py`

**Важно (влияют на производительность и надёжность):**
5. Вынести тяжёлый I/O (`get_dir_size`, чтение JSON) в `asyncio.to_thread()`
6. Заменить ротацию лога на `RotatingFileHandler` или логику «обрезать до 80%»
7. Добавить агрегированную статистику вместо пересчёта N+1 в отчётах

**Рекомендуется (улучшение качества кода):**
8. Разбить `handlers.py` на модули с использованием `Router`
9. Ввести `TypedDict` для структур `file_info`, `FileMetadata`
10. Заменить строковые константы действий на `Enum`
11. Добавить `ADMIN_ID: int | None` вместо sentinel `0`
12. Убрать закомментированный код, перенести TODO в issues

---

*Ревью охватывает все файлы проекта: `bot.py`, `config.py`, `handlers.py`, `file_handler.py`, `url_handler.py`, `users.py`, `utils.py`, `texts.py`. Тесты в проекте отсутствуют — рекомендуется добавить хотя бы тесты на `normalize_filename()`, `shorten_name()`, `slugify_cyrillic_to_ascii()` и `_clean_filename()`.*
