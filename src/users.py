import asyncio
import json
import re
from pathlib import Path

from aiogram.types import User

from config import get_base_path
from utils import slugify_cyrillic_to_ascii, load_json_safe, atomic_write_text

_users_map_lock = asyncio.Lock()


def _get_dir_name(entry: str | dict) -> str:
    """Извлечь имя директории из записи маппинга (старый и новый формат)."""
    if isinstance(entry, dict):
        return entry["dir"]
    return entry  # старый формат: просто строка


async def ensure_user_dir(user: User, create: bool) -> Path | None:
    """Получить или создать директорию пользователя."""
    base_path = get_base_path()
    base_path.mkdir(parents=True, exist_ok=True)
    users_map_path = base_path / "users_map.json"

    async with _users_map_lock:
        mapping = load_json_safe(users_map_path)

        user_label = user.username or str(user.id)
        entry = mapping.get(user_label)
        needs_save = False

        if entry:
            dir_name = _get_dir_name(entry)
            # Дописываем user_id если его ещё нет (миграция старых записей)
            if not isinstance(entry, dict):
                mapping[user_label] = {"dir": dir_name, "id": user.id}
                needs_save = True
            elif entry.get("id") != user.id:
                mapping[user_label]["id"] = user.id
                needs_save = True
        else:
            if not create:
                return None
            existing_dirs = {_get_dir_name(v) for v in mapping.values()}
            normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", slugify_cyrillic_to_ascii(user_label)).lower() or "user"
            dir_name = f"{normalized}_{user.id}" if normalized in existing_dirs else normalized
            mapping[user_label] = {"dir": dir_name, "id": user.id}
            needs_save = True

        if needs_save:
            atomic_write_text(users_map_path, json.dumps(mapping, ensure_ascii=False, indent=2))

    user_dir = base_path / dir_name
    if create:
        user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir if user_dir.exists() else None
