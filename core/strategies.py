"""Смена стратегии обхода: вручную и автоматически.

Стратегия - файл ``general*.bat`` из установки zapret, чьи аргументы
становятся ``BinaryPathName`` службы. Раньше её выбор был зашит в пресеты
(везде ALT11) и переключить её из интерфейса было нельзя: пользователю,
кому ALT11 не подходит, оставался только service.bat.

Здесь стратегия отделена от пресета:

* ``Preset`` описывает режимы (Game Filter, IPSet, списки доменов) и
  стратегию по умолчанию;
* сохранённый выбор (``settings.active_strategy``) важнее значения по
  умолчанию и действует, пока файл существует в установке;
* ``settings.strategy_mode`` - ``manual`` (выбор пользователя) или ``auto``
  (подбор по пробам, см. :mod:`core.autoswitch`).

Смена стратегии НЕ трогает режимы: служба пересоздаётся с теми же флагами
Game Filter и теми же списками, что лежат в установке. Правки, сделанные
вне Diaspas (service.bat), сохраняются.

Каталог стратегий установки: что файл реально делает.

Стратегия в UI раньше была просто именем файла («ALT7»), и выбрать её
«вслепую» можно было только после перебора. Здесь разбор уже существующих
парсеров (batparser / cmdparser) превращается в читаемое описание: список
флагов winws, ключевые признаки (какие порты фильтруются, способ desync,
hostlist-файлы) и diff относительно текущей стратегии.

Ничего не применяется и не устанавливается: модуль только читает файлы
установки, поэтому его можно звать из UI на каждое открытие карточки.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import batparser, cmdparser, installs, paths, service


MANUAL = "manual"
AUTO = "auto"

# Диапазоны Game Filter: "12" у service.bat = порты не включены (фильтр выкл).
_PORT_FULL = "1024-65535"
_PORT_OFF = "12"


@dataclass(frozen=True)
class StrategyInfo:
    """Одна стратегия из установки для отображения в GUI."""

    file: str      # "general (ALT11).bat"
    label: str     # "ALT11"
    active: bool   # стоит службой прямо сейчас (метка реестра)


# -- список ---------------------------------------------------------------

def list_strategies(root: Path | None, active_stem: str = "") -> list[StrategyInfo]:
    """Все ``general*.bat`` в естественном порядке + метки для GUI.

    Если метка реестра не совпадает ни с одним файлом (службу поставили или
    пересоздали вне Diaspas), добавляем отдельный пункт: состояние должно
    показывать правду, а не ближайший файл из списка.
    """
    if root is None:
        return []
    names = paths.strategy_files(Path(root))
    infos = [StrategyInfo(name, paths.strategy_label(name),
                          paths.strategy_stem(name) == active_stem)
             for name in names]
    if active_stem and not any(info.active for info in infos):
        name = f"{active_stem}.bat"
        infos.append(StrategyInfo(name, paths.strategy_label(name), True))
    return infos


def current_file(root: Path | None, state: service.ServiceState) -> str:
    """Файл стратегии, который службы стоят прямо сейчас ("" - неизвестно)."""
    stem = getattr(state, "strategy", "") or ""
    if root is None or not stem:
        return ""
    for name in paths.strategy_files(Path(root)):
        if paths.strategy_stem(name) == stem:
            return name
    return ""


def read_game_filter(root: Path) -> str:
    """Текущий Game Filter из установки: all/tcp/udp или "" (выключен)."""
    flag = Path(root) / "utils" / "game_filter.enabled"
    if not flag.is_file():
        return ""
    try:
        value = flag.read_text(encoding="ascii", errors="replace").strip().lower()
    except OSError:
        return ""
    return value if value in ("all", "tcp", "udp") else ""


# -- сохранённый выбор и режим -------------------------------------------

def mode() -> str:
    """Режим смены стратегии: MANUAL по умолчанию (поведение прежних версий)."""
    raw = str(paths.read_settings().get("strategy_mode") or "").strip().lower()
    return AUTO if raw == AUTO else MANUAL


def set_mode(value: str) -> bool:
    if value not in (MANUAL, AUTO):
        return False
    settings = paths.read_settings()
    settings["strategy_mode"] = value
    paths.write_settings(settings)
    return True


def saved(root: Path | None = None) -> str:
    """Сохранённая стратегия; "" - если её нет или файл пропал из установки.

    Пропавший файл (обновление релиза переименовало стратегии) не должен
    ломать применение пресета: возвращаем "", и вызывающий использует
    значение по умолчанию пресета.
    """
    name = str(paths.read_settings().get("active_strategy") or "").strip()
    if not name:
        return ""
    if root is None:
        return name
    return name if name in paths.strategy_files(Path(root)) else ""


def resolve(default_file: str, root: Path | None = None) -> str:
    """Стратегия для применения пресета: выбор пользователя или default."""
    return saved(root) or default_file


def remember(strategy_file: str, mode_value: str | None = None) -> None:
    """Записать выбранную стратегию (и, если указано, режим)."""
    settings = paths.read_settings()
    if strategy_file:
        settings["active_strategy"] = strategy_file
    if mode_value:
        settings["strategy_mode"] = mode_value
    paths.write_settings(settings)


# -- установка службы -----------------------------------------------------

def validate(root: Path, strategy_file: str) -> str | None:
    """Сообщение об ошибке или None: имя обязано быть файлом из установки.

    Проверка по списку ``general*.bat`` заодно закрывает path traversal
    (``..\\evil.bat``, абсолютный путь) - GUI передаёт имя как есть.
    """
    name = str(strategy_file or "").strip()
    if not name:
        return "стратегия не указана"
    if "\\" in name or "/" in name or name != Path(name).name:
        return f"недопустимое имя стратегии: {name!r}"
    if name not in paths.strategy_files(Path(root)):
        return f"в установке нет стратегии {name!r}"
    return None


def install(root: Path, strategy_file: str, game_filter: str = "",
            log=None) -> dict:
    """Пересоздать службу с выбранной стратегией, сохранив диапазоны портов."""
    gf = game_filter if game_filter in ("all", "tcp", "udp") else ""
    tcp = _PORT_FULL if gf in ("all", "tcp") else _PORT_OFF
    udp = _PORT_FULL if gf in ("all", "udp") else _PORT_OFF
    try:
        binpath = batparser.winws_command(Path(root), strategy_file, tcp, udp)
    except batparser.BatParseError as exc:
        if log:
            log("error", str(exc))
        return {"ok": False, "error": str(exc)}
    res = service.install_strategy(binpath, paths.strategy_stem(strategy_file),
                                   log=log)
    if not res or not res.get("ok"):
        return {"ok": False,
                "error": str((res or {}).get("error") or "операция не выполнена")}
    return res


def apply(root: Path | None, strategy_file: str, base_preset=None,
          mode_value: str = MANUAL, log=None) -> dict:
    """Поставить стратегию: только служба, режимы установки не меняются.

    ``base_preset`` - пресет, по которому создаётся служба, если её нет
    (пользователь скачал релиз, но пресет ещё не применял). Без службы
    списки и флаги сначала готовятся как в :func:`core.presets.apply_files`.
    """
    from . import presets   # цикличный импорт только внутри функции

    if root is None:
        return {"ok": False, "error": "папка zapret не найдена"}
    root = Path(root)

    error = validate(root, strategy_file)
    if error:
        if log:
            log("error", error)
        return {"ok": False, "error": error}

    st = service.state(log=log)
    if st.installed:
        # Служба есть: меняем только её аргументы, файлы не трогаем.
        game_filter = read_game_filter(root)
    else:
        base = base_preset or presets.suggested_preset()
        try:
            presets.apply_files(root, base, strategy_file)
        except (OSError, FileNotFoundError) as exc:
            if log:
                log("error", str(exc))
            return {"ok": False, "error": str(exc)}
        game_filter = base.game_filter
        presets.remember_active(base.key)

    res = install(root, strategy_file, game_filter, log=log)
    if not res.get("ok"):
        return {"ok": False, "error": str(res.get("error")),
                "strategy": strategy_file}

    remember(strategy_file, mode_value)
    if log:
        label = paths.strategy_label(strategy_file)
        log("info", f"стратегия {label!r} применена"
                    + ("" if st.installed else " (служба создана)"))
    return {"ok": True, "strategy": strategy_file,
            "label": paths.strategy_label(strategy_file),
            "mode": mode_value, "state": res.get("state") or {}}


class StrategyError(ValueError):
    """Стратегию нельзя разобрать (битый файл, чужой layout)."""


def _tokenize(args: str) -> list[str]:
    """Токены строки аргументов: кавычки сохраняются целиком."""
    out: list[str] = []
    buf = ""
    quoted = False
    for ch in args:
        if ch == '"':
            quoted = not quoted
            buf += ch
        elif ch in " \t" and not quoted:
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def split_flags(args: str) -> list[tuple[str, str]]:
    """Аргументы winws -> [(флаг, значение)].

    Флаги без значения (--new, --emph) получают пустое значение, чтобы
    diff видел их наличие/отсутствие, а не «значение изменилось».

    Форма два варианта: ``--flag=value`` (так пишут в preset*.cmd) и
    ``--flag value`` (так собирает batparser из general*.bat - он
    соединяет токены пробелом, а winws принимает оба).
    """
    tokens = _tokenize(args)
    flags: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-"):
            i += 1
            continue
        name, sep, value = token.partition("=")
        name = name.lower()
        if sep:
            flags.append((name, value))
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if nxt and not nxt.startswith("-"):
            flags.append((name, nxt))
            i += 2
        else:
            flags.append((name, ""))
            i += 1
    return flags


# Флаги, которые стоит показать первыми: они и определяют «характер»
# стратегии (что фильтруется и как подменяется трафик).
_HEADLINE = ("--wf-tcp", "--wf-udp", "--dpi-desync", "--dpi-desync-fake-tls",
             "--dpi-desync-fake-http", "--dpi-desync-fooling",
             "--hostlist", "--ipset", "--profile")


def features(flags: list[tuple[str, str]]) -> dict:
    """Короткое «что это» по флагам - для подписи в списке стратегий."""
    got = dict(flags)
    names = {name for name, _ in flags}
    return {
        "wf_tcp": got.get("--wf-tcp", ""),
        "wf_udp": got.get("--wf-udp", ""),
        "desync": got.get("--dpi-desync", ""),
        "fooling": got.get("--dpi-desync-fooling", ""),
        "fake_tls": "--dpi-desync-fake-tls" in names,
        "fake_http": "--dpi-desync-fake-http" in names,
        "hostlist": "--hostlist" in names or "--hostlist-file" in names,
        "ipset": "--ipset-file" in names or "--ipset" in names,
        "count": len(flags),
    }


def describe(root: Path | str, name: str, *,
             game_filter_tcp: str = "1024-65535",
             game_filter_udp: str = "1024-65535") -> dict:
    """Описание одной стратегии установки: {file, label, layout, flags,...}.

    Ошибка разбора - тоже ответ (ok=False): UI не должен падать из-за
    битого файла в папке, пользователь увидит причину в карточке.
    """
    root = Path(root)
    info = installs.detect_layout(root)
    layout = info["layout"] or ""
    winws_dir = info["winws_dir"] or root
    target = (winws_dir / name) if layout == installs.LAYOUT_WINWS \
        else (root / name)
    if not target.is_file():
        return {"ok": False, "file": name, "layout": layout,
                "error": f"файл {name} не найден в установке"}

    try:
        if layout == installs.LAYOUT_WINWS:
            args = cmdparser.parse_preset(target)
        else:
            args = batparser.parse_strategy(target, game_filter_tcp,
                                            game_filter_udp)
    except (batparser.BatParseError, cmdparser.CmdParseError) as exc:
        return {"ok": False, "file": name, "layout": layout,
                "error": str(exc)}

    data = target.read_bytes()
    flags = split_flags(args)
    return {"ok": True, "file": name, "label": paths.strategy_label(name),
            "layout": layout, "args": args.strip(), "flags": flags,
            "features": features(flags),
            "headline": [f"{n}{f'={v}' if v else ''}" for n, v in flags
                         if n in _HEADLINE],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:12]}


def catalog(root: Path | str, names: list[str] | None = None, **kw) -> list[dict]:
    """Все стратегии установки (или выбранные) с описаниями."""
    from . import presets as presets_mod   # лениво: presets тянет installs
    chosen = names if names is not None else presets_mod.available_strategies(root)
    return [describe(root, name, **kw) for name in chosen]


def diff(current: dict | None, chosen: dict) -> dict:
    """Что изменится, если перейти с current на chosen.

    Считаются флаги, а не порядок: в bat-файлах один и тот же набор
    winws-аргументов встречается в разном порядке, и «порядок изменился»
    не был бы полезной информацией.
    """
    if not chosen.get("ok"):
        return {"ok": False, "error": chosen.get("error", "не разобрано")}
    if not current or not current.get("ok"):
        return {"ok": True, "added": [f"{n}{f'={v}' if v else ''}"
                                      for n, v in chosen.get("flags", [])],
                "removed": [], "changed": [], "unknown_baseline": True}

    before = dict(current.get("flags", []))
    after = dict(chosen.get("flags", []))
    added = [f"{n}{f'={v}' if v else ''}" for n, v in after.items()
             if n not in before]
    removed = [f"{n}{f'={v}' if v else ''}" for n, v in before.items()
               if n not in after]
    changed = [f"{n}: {before[n] or '—'} → {after[n] or '—'}"
               for n in after if n in before and after[n] != before[n]]
    return {"ok": True, "added": added, "removed": removed,
            "changed": changed, "same": not (added or removed or changed)}


def same_file(name: str, other: str) -> bool:
    """Одна и та же стратегия по имени (с учётом расширения)."""
    def stem(value: str) -> str:
        return re.sub(r"\.(bat|cmd)$", "", value or "", flags=re.I).lower()
    return stem(name) == stem(other) and bool(name and other)
