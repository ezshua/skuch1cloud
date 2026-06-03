from datetime import datetime, timedelta
from pathlib import Path
from utils import load_json_safe, load_json_list_safe, get_dir_size
from ui_formatter import format_size

def collect_daily_report(base_path: Path) -> str:
    """
    Собирает статистику активности всех пользователей за последние 24 часа.
    """
    users_map_path = base_path / "users_map.json"
    if not users_map_path.exists():
        return ""

    mapping = load_json_safe(users_map_path)
    if not mapping:
        return ""

    report_lines = ["📊 <b>Ежедневный отчет по активности</b>"]
    total_active = 0

    now = datetime.now()
    yesterday = now - timedelta(days=1)

    for user_label, data in mapping.items():
        dir_name = data["dir"] if isinstance(data, dict) else data
        user_dir = base_path / dir_name

        # Проверяем наличие активности в логе за последние сутки
        log_path = user_dir / "action_log.json"
        actions = load_json_list_safe(log_path)

        # Очищаем tzinfo для безопасного сравнения с наивным yesterday
        recent_actions = [a for a in actions if datetime.fromisoformat(a["timestamp"]).replace(tzinfo=None) > yesterday]

        if not recent_actions:
            continue

        total_active += 1

        # Считаем только новые файлы за сутки из индекса
        files_data = load_json_list_safe(user_dir / "files_data.json")
        # Аналогично очищаем tzinfo, так как старые записи могли быть сохранены с часовым поясом
        daily_files = [
            f for f in files_data
            if datetime.fromisoformat(f["upload_date"]).replace(tzinfo=None) > yesterday
        ]

        count = len(daily_files)
        size = sum(f.get("size", 0) for f in daily_files)
        total_size = get_dir_size(user_dir)

        # Считаем только те сообщения, на которые бот ответил в режиме "эхо"
        echo_count = 0
        echo_chars = 0
        for action in recent_actions:
            details = action.get("details", {})
            if action.get("type") == "bot_response" and details.get("type") == "echo":
                echo_count += 1
                echo_chars += len(str(details.get("text", "")))

        user_row = f"👤 {user_label}: 🆕 {count} шт. | 💾 {format_size(size)} / {format_size(total_size)}"
        if echo_count > 0:
            user_row += f" | 💬 Эхо: {echo_count} ({echo_chars} симв.)"

        report_lines.append(user_row)

    if total_active == 0:
        return "📊 Активности за прошедшие сутки не зафиксировано."

    return "\n".join(report_lines)

def collect_users_summary(base_path: Path) -> str:
    """
    Собирает общую статистику по всем пользователям: кол-во файлов и физический объем папок.
    """
    users_map_path = base_path / "users_map.json"
    if not users_map_path.exists():
        return ""

    mapping = load_json_safe(users_map_path)
    if not mapping:
        return ""

    report_lines = ["👥 <b>Сводка по пользователям:</b>"]
    for label in sorted(mapping.keys(), key=lambda s: s.lower()):
        data = mapping[label]
        dir_name = data["dir"] if isinstance(data, dict) else data
        user_dir = base_path / dir_name
        if not user_dir.exists(): continue
        files_data = load_json_list_safe(user_dir / "files_data.json")
        report_lines.append(f"👤 {label}: 📁 {len(files_data)} шт. | 💾 {format_size(get_dir_size(user_dir))}")
    return "\n".join(report_lines) if len(report_lines) > 1 else ""
