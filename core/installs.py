"""Реестр установленных версий zapret: несколько папок, одна активная.

По образцу реестра установок из Synfronia (bypasses.json): версии живут
по-своему (1.9.x и 1.10.x различаются набором стратегий), поэтому каждая
версия хранится отдельно, а активная записана явно - переключение версии
это смена активной папки и переустановка службы, а не перенос файлов.

Хранение: %LOCALAPPDATA%\\Diaspas\\installs.json

    {"active": "...\\zapret-discord-youtube-1.10.3",
     "rollback": "...\\zapret-discord-youtube-1.10.2",
     "keep_versions": 3,
     "items": [{"path": ..., "version": "1.10.3", "added": ...,
                "last_used": ..., "source": "download"}]}

Поля active/rollback не удаляются никогда: активная - то, что работает,
rollback - последняя возможность отката после обновления.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .release import ReleaseError

# Имя папки релиза Flowseal: zapret-discord-youtube-<tag>
_VERSION_RE = re.compile(r"zapret-discord-youtube-(.+)$", re.IGNORECASE)

DEFAULT_KEEP = 3


@dataclass
class Install:
    """Одна установленная версия zapret."""

    path: str
    version: str
    added: int = 0
    last_used: int = 0
    source: str = "download"   # download | autodiscover | manual

    @property
    def dir(self) -> Path:
        return Path(self.path)


def installs_file() -> Path:
    from . import paths
    return paths.app_dir() / "installs.json"


def version_of(path: Path) -> str:
    """1.10.3 из имени папки zapret-discord-youtube-1.10.3."""
    m = _VERSION_RE.search(Path(path).name)
    return m.group(1) if m else Path(path).name


def load() -> dict:
    """Сырые данные реестра; отсутствие файла - пустой реестр."""
    try:
        data = json.loads(installs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active", None)
    data.setdefault("rollback", None)
    data.setdefault("keep_versions", DEFAULT_KEEP)
    data.setdefault("items", [])
    if not isinstance(data["items"], list):
        data["items"] = []
    return data


def save(data: dict) -> None:
    path = installs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def items() -> list[Install]:
    """Все записи реестра (порядок - как в файле, обычно по времени)."""
    out = []
    for raw in load().get("items", []):
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        out.append(Install(
            path=str(raw["path"]),
            version=str(raw.get("version") or ""),
            added=int(raw.get("added") or 0),
            last_used=int(raw.get("last_used") or 0),
            source=str(raw.get("source") or "download"),
        ))
    return out


def is_valid(path) -> bool:
    from . import paths
    return paths.is_zapret_dir(Path(path))


def active_path() -> Path | None:
    """Папка активной установки; None - реестр пуст/битый."""
    raw = load().get("active")
    if raw and is_valid(raw):
        return Path(raw)
    return None


def register(path, source: str = "download") -> Install:
    """Добавить установку в реестр (идемпотентно) и сделать активной."""
    path = Path(path)
    data = load()
    now = int(time.time())
    for raw in data["items"]:
        if isinstance(raw, dict) and str(raw.get("path")) == str(path):
            raw["last_used"] = now
            raw["source"] = source
            data["active"] = str(path)
            save(data)
            return Install(path=str(path), version=str(raw.get("version") or ""),
                           added=int(raw.get("added") or 0),
                           last_used=now, source=source)
    entry = asdict(Install(path=str(path), version=version_of(path),
                           added=now, last_used=now, source=source))
    data["items"].append(entry)
    data["active"] = str(path)
    save(data)
    return Install(**entry)


def set_active(path) -> None:
    """Переключить активную версию; прежняя становится точкой отката."""
    path = str(Path(path))
    data = load()
    current = data.get("active")
    if current and current != path:
        # прежняя активная - будущий откат (если она вообще существует)
        data["rollback"] = current if is_valid(current) else data.get("rollback")
    data["active"] = path
    for raw in data["items"]:
        if isinstance(raw, dict) and str(raw.get("path")) == path:
            raw["last_used"] = int(time.time())
    save(data)


def rollback_path() -> Path | None:
    """Папка версии для отката; None - откатывать некуда."""
    raw = load().get("rollback")
    if raw and is_valid(raw) and raw != load().get("active"):
        return Path(raw)
    return None


def forget(path) -> None:
    """Убрать запись (и rollback, если указывает на эту папку)."""
    path = str(Path(path))
    data = load()
    data["items"] = [r for r in data["items"]
                     if not (isinstance(r, dict) and str(r.get("path")) == path)]
    if data.get("rollback") == path:
        data["rollback"] = None
    if data.get("active") == path:
        data["active"] = None
    save(data)


def remove(path) -> bool:
    """Удалить установку: только неактивную и не-откатную.

    Папка должна реально уйти: если файлы заняты (запущенный winws из
    этой папки при рассинхроне реестра и службы) - возвращаем False и
    запись сохраняем, иначе реестр разошёлся бы с диском.
    """
    import shutil as _shutil
    path = Path(path)
    data = load()
    if str(path) == str(data.get("active")):
        return False
    if str(path) == str(data.get("rollback")):
        return False
    if path.is_dir():
        _shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            return False
    forget(path)
    return True


def service_points_to(path) -> bool:
    """Строит ли служба zapret на эту папку (без прав, по реестру)."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"System\CurrentControlSet\Services\zapret") as key:
            binpath = str(winreg.QueryValueEx(key, "ImagePath")[0])
    except OSError:
        return False
    try:
        exe = Path(binpath.split('"')[1])
        return exe.resolve().is_relative_to(Path(path).resolve())
    except (ValueError, IndexError, OSError):
        return False


def scan_candidates() -> list[Path]:
    """Все валидные папки zapret на известных местах + в реестре.

    Глубина 1 (как у Synfronia): папка установки и её соседи, без
    глубокого обхода - иначе чистка превращается в блуждание.
    """
    from . import paths
    found: list[Path] = []
    root = paths.app_dir() / "zapret"
    if root.is_dir():
        try:
            for kid in sorted(root.iterdir()):
                if kid.is_dir() and paths.is_zapret_dir(kid):
                    found.append(kid)
        except OSError:
            pass
    for cand in paths._CANDIDATE_DIRS:
        p = Path(cand)
        if p.is_dir() and paths.is_zapret_dir(p):
            found.append(p)
    for inst in items():
        p = inst.dir
        if p.is_dir() and paths.is_zapret_dir(p):
            found.append(p)
    # уникальные, сохраняя порядок
    seen: set[str] = set()
    out: list[Path] = []
    for p in found:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def ensure_registry() -> dict:
    """Заполнить реестр при первом запуске (миграция со старых данных).

    Источники: настройки Diaspas (записанная папка), автопоиск. Всё
    найденное попадает в реестр, активная - из настроек либо первая
    найденная.
    """
    from . import paths
    data = load()
    known = {str(r.get("path")) for r in data["items"] if isinstance(r, dict)}

    configured = str(paths.read_settings().get("zapret_dir") or "").strip()
    for p in scan_candidates():
        if str(p) not in known:
            source = "download" if str(p) == configured else "autodiscover"
            data["items"].append(asdict(Install(
                path=str(p), version=version_of(p), added=int(time.time()),
                last_used=0, source=source)))
            known.add(str(p))

    if not data.get("active"):
        if configured and configured in known:
            data["active"] = configured
        elif data["items"]:
            data["active"] = str(data["items"][0]["path"])
    save(data)
    return data


def prune(keep: int | None = None) -> list[Install]:
    """Версии сверх лимита, кандидаты на удаление.

    Активная и откатная не предлагаются никогда; кандидаты - самые
    давние по last_used (но не новее добавленных, если лимит1).
    """
    data = load()
    limit = int(keep or data.get("keep_versions") or DEFAULT_KEEP)
    all_items = items()
    if len(all_items) <= limit:
        return []
    protected = {str(data.get("active")), str(data.get("rollback"))}
    # сортировка: самые давние (малый last_used) вперёд
    ordered = sorted(all_items, key=lambda i: (i.last_used, i.added))
    surplus = len(all_items) - limit
    out = [i for i in ordered if str(i.path) not in protected][:surplus]
    return out


def set_keep(value: int) -> None:
    """Сколько версий держать (>=1)."""
    data = load()
    data["keep_versions"] = max(1, int(value))
    save(data)


# -- целостность и ремонт ----------------------------------------------------

def integrity(root: Path) -> list[str]:
    """Недостающие файлы установки - относительные пути вида BIN/winws.exe.

    Проверяются самодостаточно (без реестра службы): winws.exe и все
    файлы, на которые ссылаются стратегии через %BIN%/%LISTS%. Ровно
    та проверка, что ловила 1067: winws отказывается стартовать без
    обязательных hostlist-файлов и без фейков, на которые ссылается
    --dpi-desync-fake-*.
    """
    from . import batparser

    root = Path(root)
    missing: set[str] = set()

    exe = root / "bin" / "winws.exe"
    if not exe.is_file():
        missing.add("BIN/winws.exe")

    strategies = [p.name for p in root.glob("general*.bat")]
    if not strategies:
        missing.add("general*.bat")
        return sorted(missing)

    for name in strategies:
        # файлы из аргументов стратегии (списки, .bin-фейки)
        try:
            args = batparser.parse_strategy(root / name)
        except batparser.BatParseError:
            continue                      # битая стратегия - не её файлы ищем
        # batparser оборачивает пути в \": regex обязан их видеть, иначе
        # хвостовой \ попадает в путь и файл «не существует» всегда
        for ref in re.findall(r'\\"([^"]+)\\"', args):
            p = Path(ref)
            if p.is_file():
                continue
            # *-user.txt создаёт service.bat при запуске менеджера -
            # их отсутствие в свежей распаковке норма, winws дождётся
            if p.name.endswith("-user.txt"):
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            missing.add(str(rel).replace("\\", "/"))
    return sorted(missing)


def repair(root: Path, zip_path: Path) -> int:
    """Восстановить недостающее из архива той же версии. Возвращает счёт.

    Приём Synfronia: антивирус регулярно съедает winws.exe и DLL - нет
    смысла переустанавливать всю версию ради одного файла. Файлы
    дописываются поверх, чужие (пользовательские списки) не трогаются.
    """
    root = Path(root)
    missing = integrity(root)
    if not missing:
        return 0
    version = version_of(root)
    prefix = ""
    restored = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # имена в zip регистрозависимы, а .bat пишет bin/Lists по-разному
            by_lower = {n.lower(): n for n in zf.namelist()}
            # архив распаковывается в <корень-релиза>/...; определяем префикс
            for n in zf.namelist():
                if n.lower().endswith("service.bat"):
                    prefix = n[: -len("service.bat")]
                    break
            for rel in missing:
                member = by_lower.get(f"{prefix}{rel}".lower())
                if member is None:
                    raise ReleaseError(
                        f"ремонт {version}: в архиве нет {rel}")
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(root / rel, "wb") as fh:
                    shutil.copyfileobj(src, fh)
                restored += 1
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ReleaseError(
            f"ремонт {version}: не удалось восстановить из архива: {exc}"
        ) from exc
    return restored
