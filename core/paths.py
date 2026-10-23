"""Расположение файлов Diaspas и обнаружение установки zapret.

Запасной путь для всего, что пишется на диск: %LOCALAPPDATA%\\Diaspas.
Папка с zapret ищется по стандартным местам и по записанной в настройках -
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
    """Папка пригодна: любой поддерживаемый layout установки.

    flowseal: service.bat + bin\\ + general*.bat; winws: winws.exe и
    preset*.cmd рядом (bol-van zapret-win-bundle). detect_layout ищет
    winws.exe вглубь, поэтому проверка одинаково ловит оба layout.
    """
    from . import installs   # лениво: installs тянет paths
    if not Path(path).is_dir():
        return False
    return installs.detect_layout(path)["layout"] is not None


def strategy_files(root: Path) -> list[str]:
    """Имена general*.bat в порядке естественной сортировки.

    Порядок совпадает с меню Install Service из service.bat: ALT, ALT2 ...
    ALT13, затем остальные - цифры выравниваются ведущими нулями до 8
    разрядов (тот же приём PadLeft(8, '0') в service.bat).
    """
    import re

    def natural(name: str) -> str:
        return re.sub(r"\d+", lambda m: m.group(0).zfill(8), name)

    names = [p.name for p in root.glob("general*.bat") if p.name != "service.bat"]
    return sorted(names, key=natural)


def _strip_ext(name: str) -> str:
    """Снять расширение стратегии: general*.bat и preset*.cmd равноправны.

    Два layout установки дают два набора имён, а метка в реестре и короткая
    метка в GUI обязаны сниматься одинаково - иначе win-bundle показывался бы
    как «preset1_example.cmd».
    """
    lower = name.lower()
    for ext in (".bat", ".cmd"):
        if lower.endswith(ext):
            return name[:-len(ext)]
    return name


def strategy_label(filename: str) -> str:
    """general (ALT11).bat -> ALT11; general.bat -> general;
    preset1_example.cmd -> preset1_example.

    Короткая метка для отображения в GUI.
    """
    name = _strip_ext(filename)
    if name.lower().startswith("general"):
        name = name[7:].strip(" ()")
    return name or "general"


def strategy_stem(filename: str) -> str:
    """general (ALT11).bat -> general (ALT11): метка в реестре.

    Ровно то, что пишет service.bat (``%%~nF``) - храним так же, чтобы
    Diaspas читал установки, поставленные штатным менеджером, и наоборот.
    Для win-bundle метка - имя ``preset*.cmd`` без расширения.
    """
    return _strip_ext(filename)


def read_settings() -> dict:
    """Настройки Diaspas; отсутствующий файл - пустой словарь.

    Повреждённый JSON не должен превращаться в «настроек нет»: выбор
    стратегий и активная папка при этом исчезли бы молча, а пользователь
    увидел бы «папка не найдена». Битый файл убирается в сторону и
    попадает в журнал - восстановиться можно из него вручную.
    """
    path = app_dir() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError:
        _quarantine(path)
        return {}
    if not isinstance(data, dict):
        _quarantine(path)
        return {}
    return data


def _quarantine(path: Path) -> None:
    """Битый файл настроек - в settings.broken, с записью в журнал."""
    from . import log as dlog
    broken = path.with_name(path.stem + ".broken")
    try:
        path.replace(broken)
        dlog.warning(f"settings.json повреждён - убран в {broken.name}; "
                     f"настройки начаты заново")
    except OSError as exc:
        dlog.error(f"settings.json повреждён и не удалось убрать его "
                   f"в сторону: {exc}")


def migrate_settings() -> list[str]:
    """Привести настройки к текущему виду; вернуть список миграций.

    Формат менялся дважды: плоские ``preset_strategies`` от
    одноверсионности и ключ-путь установки. Второй терялся при
    переименовании папки, поэтому выбор переезжает под стабильный id
    (см. installs.uid_of).
    """
    data = read_settings()
    raw = data.get("preset_strategies")
    if not isinstance(raw, dict) or not raw:
        return []

    from . import installs
    from .presets import WILDCARD

    nested: dict[str, dict] = {}
    flat: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            nested[str(key)] = {str(k): str(v) for k, v in value.items() if v}
        elif value:
            flat[str(key)] = str(value)

    changes: list[str] = []
    if flat:
        wide = nested.setdefault(WILDCARD, {})
        for key, value in flat.items():
            wide.setdefault(key, value)
        changes.append("плоские записи стратегий → «для любой установки»")

    for key, bucket in list(nested.items()):
        if key in (WILDCARD, ""):
            continue
        uid = installs.uid_of(key)
        if uid and uid != key:
            merged = nested.setdefault(uid, {})
            merged.update({k: v for k, v in bucket.items() if v})
            nested.pop(key, None)
            changes.append("ключ-путь установки → её стабильный id")

    if changes:
        data["preset_strategies"] = {k: v for k, v in nested.items() if v}
        write_settings(data)
    return changes


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


def zapret_dir_info() -> tuple[Path | None, str]:
    """(папка, откуда взялась) - для честного отображения в UI.

    Приоритет: реестр установленных версий (активная версия) -> настройки
    (наследие одноверсионности) -> автопоиск. Автопоиск молча подставлял
    старую установку вместо «скачанной» - теперь источник виден.
    """
    # реестр версий: активная записана явно (см. core.installs)
    from . import installs
    active = installs.active_path()
    if active is not None:
        return active, "settings"

    configured = str(read_settings().get("zapret_dir") or "").strip()
    if configured:
        path = Path(configured)
        if is_zapret_dir(path):
            return path, "settings"
    found = zapret_install_dir()
    return found, ("autodiscover" if found else "")
