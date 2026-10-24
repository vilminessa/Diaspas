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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import batparser, paths, service

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
