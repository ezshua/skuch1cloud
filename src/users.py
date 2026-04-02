import json
import re
from pathlib import Path

from aiogram.types import User

from config import get_base_path
from utils import slugify_cyrillic_to_ascii, load_json_safe, atomic_write_text


async def ensure_user_dir(user: User, create: bool) -> Path | None:
    """Получить или создать директорию пользователя."""
    base_path = get_base_path()
    base_path.mkdir(parents=True, exist_ok=True)
    users_map_path = base_path / "users_map.json"
    
    mapping = load_json_safe(users_map_path)

    user_label = user.username or str(user.id)
    dir_name = mapping.get(user_label)

    if not dir_name:
        if not create:
            return None
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", slugify_cyrillic_to_ascii(user_label)).lower() or "user"
        dir_name = f"{normalized}_{user.id}" if any(v == normalized for v in mapping.values()) else normalized
        mapping[user_label] = dir_name
        atomic_write_text(users_map_path, json.dumps(mapping, ensure_ascii=False, indent=2))

    user_dir = base_path / dir_name
    if create:
        user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir if user_dir.exists() else None
