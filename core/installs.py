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
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .release import ReleaseError

# Маркер установки: стабильный id переживает переименование и перенос
# папки (см. uid_of). Свой файл, а не реестр - реестр можно потерять или
# пересоздать, а привязка выбора стратегий должна остаться.
UID_FILE = ".diaspas.json"

# Имя папки релиза Flowseal: zapret-discord-youtube-<tag>
_VERSION_RE = re.compile(r"zapret-discord-youtube-(.+)$", re.IGNORECASE)
# win-bundle: папка zipball - zapret-win-bundle-<tag> (мастер/ветка)
_VERSION_RE_WINWS = re.compile(r"zapret-win-bundle-(.+)$", re.IGNORECASE)

DEFAULT_KEEP = 3


@dataclass
class Install:
    """Одна установленная версия zapret."""

    path: str
    version: str
    added: int = 0
    last_used: int = 0
    source: str = "download"   # download | autodiscover | manual
    layout: str = ""           # flowseal | winws (пусто - определить)
    uid: str = ""              # стабильный id установки (см. uid_of)

    @property
    def dir(self) -> Path:
        return Path(self.path)


def installs_file() -> Path:
    from . import paths
    return paths.app_dir() / "installs.json"


def version_of(path: Path) -> str:
    """1.10.3 из zapret-discord-youtube-1.10.3; master из win-bundle-master."""
    name = Path(path).name
    m = _VERSION_RE.search(name) or _VERSION_RE_WINWS.search(name)
    return m.group(1) if m else name


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
            layout=str(raw.get("layout") or ""),
            uid=str(raw.get("uid") or ""),
        ))
    return out


# -- стабильный id установки -------------------------------------------------

def marker_path(root: Path | str) -> Path:
    return Path(root) / UID_FILE


def read_marker(root: Path | str) -> str:
    """uid из маркерного файла установки; "" - маркера нет."""
    try:
        data = json.loads(marker_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("uid") or "") if isinstance(data, dict) else ""


def write_marker(root: Path | str, uid_value: str) -> None:
    root = Path(root)
    try:
        marker_path(root).write_text(
            json.dumps({"uid": uid_value, "created": int(time.time())},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        # папка может быть только что распакована в read-only место или
        # занята антивирусом: отсутствие маркера не должно рвать установку
        pass


def uid_of(path: Path | str) -> str:
    """Стабильный ключ установки: uid из маркера, иначе путь.

    Второй случай - установка, которую пользователь указал вручную и
    которую Diaspas не ставил: своего id у неё нет, и выбор ложится на
    путь (миграция перепишет на uid при первом register).
    """
    marker = read_marker(path)
    if marker:
        return marker
    raw = load()
    for item in raw.get("items", []):
        if isinstance(item, dict) and str(item.get("path")) == str(Path(path)):
            if item.get("uid"):
                # реестр знает id, а маркер потерян - восстановить молча
                write_marker(path, str(item["uid"]))
                return str(item["uid"])
            break
    return str(Path(path))


def path_for_uid(uid_value: str) -> Path | None:
    """Папка установки по её id (для чтения старых записей настроек)."""
    if not uid_value:
        return None
    for item in items():
        if item.uid == uid_value and is_valid(item.path):
            return Path(item.path)
    return None


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
    layout = detect_layout(path)["layout"] or ""
    uid_value = read_marker(path) or next(
        (str(r.get("uid")) for r in data["items"]
         if isinstance(r, dict) and str(r.get("path")) == str(path) and r.get("uid")),
        "")
    if not uid_value:
        uid_value = uuid.uuid4().hex[:12]
        write_marker(path, uid_value)
    for raw in data["items"]:
        if isinstance(raw, dict) and str(raw.get("path")) == str(path):
            raw["last_used"] = now
            raw["source"] = source
            raw["layout"] = layout
            raw["uid"] = uid_value
            data["active"] = str(path)
            save(data)
            return Install(path=str(path), version=str(raw.get("version") or ""),
                           added=int(raw.get("added") or 0),
                           last_used=now, source=source, layout=layout,
                           uid=uid_value)
    entry = asdict(Install(path=str(path), version=version_of(path),
                           added=now, last_used=now, source=source,
                           layout=layout, uid=uid_value))
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


# -- layout установки ---------------------------------------------------------

LAYOUT_FLOWSEAL = "flowseal"   # general*.bat + bin\winws.exe + lists\ (Flowseal)
LAYOUT_WINWS = "winws"         # preset*.cmd + winws.exe рядом (bol-van win-bundle)


def detect_layout(root: Path) -> dict:
    """Какой layout у установки: flowseal (Flowseal) или winws (win-bundle).

    Поиск winws.exe вглубь до 3 уровней (приём Synfronia _install_dirs):
    в win-bundle он лежит рядом с preset*.cmd, в Flowseal - в bin\\. Стратегии
    находятся относительно той же папки, где лежит winws.exe.
    """
    root = Path(root)
    dirs: list[Path] = [root]
    frontier = [root]
    for _ in range(3):
        nxt: list[Path] = []
        for d in frontier:
            try:
                kids = [p for p in d.iterdir() if p.is_dir()][:25]
            except OSError:
                continue
            for kid in kids:
                if kid not in dirs:
                    dirs.append(kid)
                    nxt.append(kid)
        frontier = nxt
        if not frontier:
            break

    def collect(pattern: str) -> list[tuple[Path, str]]:
        found: list[tuple[Path, str]] = []
        for d in dirs:
            try:
                for f in sorted(d.glob(pattern)):
                    if f.is_file():
                        found.append((d, f.name))
            except OSError:
                continue
        return found

    bat = collect("general*.bat")
    preset = collect("preset*.cmd")
    winws_dir = next((d for d in dirs if (d / "winws.exe").is_file()), None)

    if bat:
        return {"layout": LAYOUT_FLOWSEAL,
                "strategies": [name for _, name in bat],
                "winws_dir": winws_dir,
                "winws": str(winws_dir / "winws.exe") if winws_dir else ""}
    if preset and winws_dir is not None:
        return {"layout": LAYOUT_WINWS,
                "strategies": [name for _, name in preset],
                "winws_dir": winws_dir,
                "winws": str(winws_dir / "winws.exe")}
    return {"layout": None, "strategies": [], "winws_dir": None, "winws": ""}


# -- целостность и ремонт ----------------------------------------------------

def integrity(root: Path) -> list[str]:
    """Недостающие файлы установки - относительные пути вида BIN/winws.exe.

    Работает для обоих layout: flowseal (winws.exe в bin\\, списки в
    lists\\) и winws (winws.exe рядом с preset*.cmd, файлы в files\\ и
    windivert.filter\\). Ровно та проверка, что ловила 1067: winws
    отказывается стартовать без файлов, на которые ссылаются стратегии.
    """
    from . import batparser, cmdparser

    root = Path(root)
    info = detect_layout(root)
    missing: set[str] = set()

    if info["layout"] is None:
        missing.add("general*.bat|preset*.cmd")
        return sorted(missing)
    if not info["winws"]:
        missing.add("BIN/winws.exe")

    def add_ref(ref: str) -> None:
        p = Path(ref)
        if p.is_file():
            return
        # *-user.txt создаёт service.bat при запуске менеджера -
        # их отсутствие в свежей распаковке норма
        if p.name.endswith("-user.txt"):
            return
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        missing.add(str(rel).replace("\\", "/"))

    if info["layout"] == LAYOUT_FLOWSEAL:
        for name in info["strategies"]:
            try:
                args = batparser.parse_strategy(root / name)
            except batparser.BatParseError:
                continue                      # битая стратегия - не её файлы
            # batparser оборачивает пути в \": regex обязан их видеть,
            # иначе хвостовой \ попадает в путь и файл «не существует»
            for ref in re.findall(r'\\"([^"]+)\\"', args):
                add_ref(ref)
    else:
        winws_dir = info["winws_dir"] or root
        for name in info["strategies"]:
            try:
                args = cmdparser.parse_preset(winws_dir / name)
            except cmdparser.CmdParseError:
                continue
            # %~dp0 уже раскрыт - берём только абсолютные пути: прочие
            # кавычки в .cmd (start "zapret") - не файлы
            for ref in re.findall(r'"([A-Za-z]:[^"]+)"', args):
                add_ref(ref)
            for token in args.split():
                if token.startswith("@\""):
                    add_ref(token[2:-1])
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
