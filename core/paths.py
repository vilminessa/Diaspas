"""Расположение файлов Diaspas и обнаружение установки zapret.

Запасной путь для всего, что пишется на диск: %LOCALAPPDATA%\Diaspas.
Папка с zapret ищется по стандартным местам и по записанной в настройках —
сама Diaspas бинарники zapret не создаёт, только распаковывает релиз
пользователя в его же директорию.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "Diaspas"

# Репозиторий, релизы которого Diaspas ставит и переключает.
ZAPRET_OWNER = "Flowseal"
ZAPRET_REPO = "zapret-discord-youtube"

# Где ищем уже установленный zapret, если настройки пусты.
# Первый существующий выигрывает.
_CANDIDATE_DIRS = (
    r"C:\zapret-discord-youtube-1.10.2",
    r"C:\zapret-discord-youtube-1.9.8b",
    r"C:\zapret-discord-youtube-1.9.7b",
    r"D:\zapret-discord-youtube-1.10.2",
)


def app_dir() -> Path:
    """Корневая папка Diaspas: настройки, журнал, закачанные релизы."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def cache_dir() -> Path:
    """Куда складываются скачанные zip-релизы (до распаковки)."""
    return app_dir() / "cache"


def zapret_install_dir() -> Path | None:
    """Найденная папка установки zapret или None, если нигде нет."""
    for raw in _CANDIDATE_DIRS:
        path = Path(raw)
        if (path / "service.bat").is_file():
            return path
    return None


def is_zapret_dir(path: Path) -> bool:
    """Папка пригодна: есть service.bat, bin и хотя бы одна стратегия."""
    if not path.is_dir():
        return False
    if not (path / "service.bat").is_file():
        return False
    if not (path / "bin").is_dir():
        return False
    return bool(strategy_files(path))


def strategy_files(root: Path) -> list[str]:
    """Имена general*.bat в порядке естественной сортировки.

    Порядок совпадает с меню Install Service из service.bat: ALT, ALT2 ...
    ALT13, затем остальные — цифры выравниваются ведущими нулями.
    """
    import re

    names = [p.name for p in root.glob("general*.bat") if p.name != "service.bat"]
    if not names:
        return []
    return sorted(names, key=lambda n: [int(x) if x.isdigit() else x
                                        for x in re.split(r"(\d+)", n)])


def strategy_label(filename: str) -> str:
    """general (ALT11).bat -> ALT11; general.bat -> general."""
    name = filename
    if name.lower().endswith(".bat"):
        name = name[:-4]
    if name.lower().startswith("general"):
        name = name[7:].strip(" ()")
    return name or "general"


def read_settings() -> dict:
    """Настройки Diaspas; отсутствующий файл - пустой словарь."""
    path = app_dir() / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_settings(data: dict) -> None:
    path = app_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def zapret_dir_from_settings() -> Path | None:
    """Папка zapret из настроек, иначе автопоиск."""
    configured = str(read_settings().get("zapret_dir") or "").strip()
    if configured:
        path = Path(configured)
        if is_zapret_dir(path):
            return path
    return zapret_install_dir()
