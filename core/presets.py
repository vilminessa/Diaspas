"""Пресеты: одна кнопка = стратегия + режимы + списки + перезапуск службы.

Пресет - это набор действий над установкой zapret:

* какая ``general*.bat`` станет стратегией службы;
* режим Game Filter (``utils/game_filter.enabled``): all/tcp/udp/нет файла;
* режим IPSet (``lists/ipset-all.txt``): any (пустой файл), none (заглушка
  203.0.113.113/32), loaded (реальный список из бэкапа);
* какие домены дописать в пользовательские списки - например ``ea.com``
  в исключения, чтобы zapret не трогал авторизацию EA.

Применение пресета идёт по шагам без прав (запись файлов - пользовательские
списки), а пересоздание службы уходит в service.run_action с UAC-обходом.

Стратегия - не константа пресета: ``set_strategy`` сохраняет выбор
пользователя, а дефолт разрешается под layout папки (``general*.bat`` во
Flowseal, ``preset*.cmd`` в win-bundle). Переопределения хранятся ПО
УСТАНОВКЕ: наборы стратегий у версий zapret разные, и выбор ALT13 в 1.10.x
не должен ломать применение в 1.9.x, где такого файла нет.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import batparser, service

# Заглушка IPSet-режима «none»: адрес TEST-NET, которого нет в реальном
# трафике - ни один пакет не совпадёт со списком (так делает service.bat).
_IPSET_NONE_MARKER = "203.0.113.113/32"


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    strategy: str                 # например "general (ALT11).bat"; "" = выкл
    game_filter: str = ""         # all | tcp | udp | "" (не трогать)
    ipset: str = ""               # any | none | loaded | "" (не трогать)
    exclude_domains: tuple[str, ...] = ()    # в list-exclude-user.txt
    general_domains: tuple[str, ...] = ()    # в list-general-user.txt
    description: str = ""

    @property
    def off(self) -> bool:
        return not self.strategy


# Сервисы, которые A/B-тест доказал: работают и БЕЗ обхода (DPI их не
# блокирует), а вот zapret их ломает (сброс TLS при десинке). Поэтому
# эксклюзы этих доменов безопасны всегда и входят в каждый пресет: игрок
# получает 11/11 независимо от выбора, не жертвуя ни EA, ни Ubisoft.
SAFE_EXCLUDE: tuple[str, ...] = ("ea.com", "ubisoft.com", "ubi.com")

DEFAULT_PRESETS: tuple[Preset, ...] = (
    Preset(
        key="discord_youtube",
        title="Discord / YouTube",
        strategy="general (ALT11).bat",
        game_filter="", ipset="",
        exclude_domains=SAFE_EXCLUDE,
        general_domains=(),
        description="Базовый обход для мессенджеров и видео. Игровые "
                    "фильтры не трогает.",
    ),
    Preset(
        key="apex_ea",
        title="Apex / EA",
        strategy="general (ALT11).bat",
        game_filter="all", ipset="any",
        exclude_domains=SAFE_EXCLUDE,
        general_domains=(),
        description="Игровые порты открыты; авторизация EA и Ubisoft "
                    "в исключениях - работают без обхода (проверено "
                    "A/B-тестом).",
    ),
    Preset(
        key="ubisoft",
        title="Ubisoft",
        strategy="general (ALT11).bat",
        game_filter="all", ipset="any",
        exclude_domains=SAFE_EXCLUDE,
        general_domains=(),
        description="Аккаунт Ubisoft Connect в исключениях вместе с EA: "
                    "оба сервиса чисты и без zapret. IP 216.98.x.x "
                    "закрыты на уровне сети - обход их не открывает.",
    ),
    Preset(
        key="games_max",
        title="Максимум для игр",
        strategy="general (ALT11).bat",
        game_filter="all", ipset="any",
        exclude_domains=SAFE_EXCLUDE,
        general_domains=(),
        description="Игровые порты TCP и UDP открыты полностью: подбор "
                    "игроков и UDP-трафик идут через обход.",
    ),
    Preset(
        key="off",
        title="Выкл",
        strategy="",
        game_filter="", ipset="",
        exclude_domains=(), general_domains=(),
        description="Служба zapret удалена, winws остановлен.",
    ),
)


# -- переопределение стратегии: выбор пользователя по установке ----------

def _root_key(root: Path | str | None) -> str:
    return str(Path(root)) if root is not None else ""


def _split_overrides(raw) -> tuple[dict, dict]:
    """Разделить переопределения на «по установке» и плоское наследие.

    Новый вид - ``{путь установки: {ключ пресета: файл}}``. Плоский
    ``{ключ: файл}`` остался от одноверсионности: он перекрывал все
    установки сразу, поэтому выбор ALT13 в 1.10.x ломал применение в
    версии, где такого файла нет. Плоские записи читаются как
    «для любой установки», но вытесняются явным выбором по пути.
    """
    nested: dict[str, dict] = {}
    legacy: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                nested[str(key)] = {str(k): str(v)
                                    for k, v in value.items() if v}
            elif value:
                legacy[str(key)] = str(value)
    return nested, legacy


def _override_map(settings: dict, root: str = "") -> dict[str, str]:
    """Что перекрыть для этой установки: наследие + запись по пути."""
    nested, legacy = _split_overrides(settings.get("preset_strategies"))
    out = dict(legacy)
    if root:
        out.update(nested.get(root) or {})
    elif nested:
        out.update(next(iter(nested.values()), {}))
    return out


def available_strategies(root: Path | str) -> list[str]:
    """Файлы стратегий установки в порядке меню service.bat.

    flowseal - ``general*.bat`` в корне, winws - ``preset*.cmd`` рядом с
    winws.exe. Единая точка для UI, валидации и применения: иначе каждый
    вызывающий сам гадает, какой layout у папки.
    """
    from . import cmdparser, installs, paths
    root = Path(root)
    info = installs.detect_layout(root)
    if info["layout"] == installs.LAYOUT_WINWS:
        winws_dir = info["winws_dir"] or root
        return (cmdparser.preset_files(winws_dir)
                or cmdparser.preset_files(root)
                or list(info["strategies"]))
    return paths.strategy_files(root)


def _layout_default(root: Path | str | None, strategy: str) -> str:
    """Разрешить стратегию под layout папки.

    Пресеты описаны для flowseal (``general (ALT11).bat``), а в win-bundle
    своих ``general*.bat`` нет - аргументы задаёт ``preset*.cmd``. Без
    подстановки первое же применение падало бы «нет стратегии», и
    переключать было бы нечего.
    """
    from . import installs
    if not strategy or root is None:
        return strategy
    root_path = Path(root)
    info = installs.detect_layout(root_path)
    if info["layout"] != installs.LAYOUT_WINWS:
        return strategy
    winws_dir = info["winws_dir"] or root_path
    if (winws_dir / strategy).is_file():
        return strategy
    names = available_strategies(root_path)
    return names[0] if names else strategy


def _with_strategy(preset: Preset, override: str,
                   root: Path | str | None) -> Preset:
    """Пресет со стратегией: переопределение > дефолт, плюс layout-разрешение.

    Пресет с переопределённой стратегией участвует в маркере и инференсе
    как обычный (``Preset.strategy`` везде сравнивается), поэтому подсветка
    и установка службы сходятся.
    """
    if preset.off:
        return preset
    return replace(preset,
                   strategy=_layout_default(root, override or preset.strategy))


def load_presets(root: Path | str | None = None) -> list[Preset]:
    """Пресеты по умолчанию + переопределения + пользовательские.

    ``root`` - папка установки, для которой собираются стратегии
    (по умолчанию - активная). Без неё переопределения «по установке» не
    найти, и вернулся бы дефолт.
    """
    from . import paths
    if root is None:
        root = paths.zapret_dir_info()[0]
    settings = paths.read_settings()
    overrides = _override_map(settings, _root_key(root))

    presets: list[Preset] = []
    for preset in DEFAULT_PRESETS:
        presets.append(_with_strategy(preset, overrides.get(preset.key, ""),
                                      root))

    custom = settings.get("presets") or []
    for item in custom:
        try:
            key = str(item["key"])
            strategy = str(item.get("strategy") or "")
            presets.append(_with_strategy(Preset(
                key=key,
                title=str(item["title"]),
                strategy=strategy,
                game_filter=str(item.get("game_filter") or ""),
                ipset=str(item.get("ipset") or ""),
                exclude_domains=tuple(item.get("exclude_domains") or ()),
                general_domains=tuple(item.get("general_domains") or ()),
                description=str(item.get("description") or ""),
            ), overrides.get(key, ""), root))
        except (KeyError, TypeError):
            continue
    return presets


def strategy_override(key: str, root: Path | str | None = None) -> str:
    """Переопределённая стратегия пресета (пусто - дефолт)."""
    from . import paths
    if root is None:
        root = paths.zapret_dir_info()[0]
    return _override_map(paths.read_settings(), _root_key(root)).get(key, "")


def set_strategy(key: str, strategy: str | None,
                 root: Path | str | None = None) -> None:
    """Выбрать стратегию для пресета в этой установке (None - вернуть дефолт).

    Главная кнопка после автоподбора: «взять ALT7» = set_strategy(key,
    "general (ALT7).bat", root). Файл должен существовать в установке -
    проверка на стороне вызывающего (``apply`` всё равно сверится).
    Явный выбор снимает плоское наследие этого пресета, иначе сброс
    «вернуть дефолт» возвращал бы старую глобальную запись.
    """
    from . import paths
    if root is None:
        root = paths.zapret_dir_info()[0]
    path = _root_key(root)
    settings = paths.read_settings()
    nested, legacy = _split_overrides(settings.get("preset_strategies"))
    if path:
        bucket = nested.setdefault(path, {})
        if strategy:
            bucket[key] = str(strategy)
        else:
            bucket.pop(key, None)
        # Сброс «вернуть дефолт» обязан убирать и плоское наследие: без
        # этого выбор по установке выглядел бы сброшенным, а применялась
        # бы старая глобальная запись.
        legacy.pop(key, None)
    elif strategy:
        # Установки нет (папка не найдена): выбор ложится плоско и его
        # нельзя сносить тем же pop'ом - иначе запись теряется в тот же
        # вызов, и переключение стратегии молча возвращается в дефолт.
        legacy[key] = str(strategy)
    else:
        legacy.pop(key, None)
    nested = {k: v for k, v in nested.items() if v}
    settings["preset_strategies"] = {**legacy, **nested}
    paths.write_settings(settings)


def current_strategy_file(root: Path, state: service.ServiceState) -> str:
    """Имя файла стратегии, которая реально стоит в службе ("" - неясно).

    UI обязан показывать факт, а не обещание пресета: иначе селектор
    подсвечивает то, что Diaspas *хотел* поставить, и «Применить»
    появляется на уже выбранном пункте.
    """
    return _snapshot(Path(root), state).strategy_file


# -- запись состояния в установку zapret -----------------------------------

def ensure_user_lists(root: Path) -> None:
    """Гарантировать все 4 пользовательских файла списков - как
    load_user_lists в service.bat.

    Без них winws отказывается стартовать (обязательные hostlist-файлы
    отсутствуют) и служба падает с WIN32_EXIT_CODE 1067. Файлы создаются
    только если их нет: чужие правки не затираются.
    """
    root = Path(root)
    lists = root / "lists"
    lists.mkdir(parents=True, exist_ok=True)

    defaults = {
        # заглушка IP: TEST-NET-адрес, ни один реальный пакет не совпадёт
        "ipset-exclude-user.txt": "203.0.113.113/32\r\n",
        "list-general-user.txt": "# Never leave this file empty\r\n"
                                 "domain.example.abc\r\n",
        "list-exclude-user.txt": "# Never leave this file empty\r\n"
                                 "domain.example.abc\r\n",
    }
    for name, content in defaults.items():
        path = lists / name
        if not path.is_file():
            path.write_text(content, encoding="utf-8", newline="\n")


def _write_flag(root: Path, game_filter: str) -> None:
    """Флаг Game Filter: содержимое - all/tcp/udp, отсутствие - выкл."""
    flag = root / "utils" / "game_filter.enabled"
    if not game_filter:
        if flag.exists():
            flag.unlink()
        return
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(game_filter + "\r\n", encoding="ascii")


def _write_ipset(root: Path, mode: str) -> None:
    """IPSet: any - пустой файл, none - заглушка, loaded - бэкап вместо файла."""
    if not mode:
        return
    ipset = root / "lists" / "ipset-all.txt"
    backup = ipset.with_name(ipset.name + ".backup")
    if mode == "any":
        ipset.write_text("", encoding="ascii")
    elif mode == "none":
        ipset.write_text(_IPSET_NONE_MARKER + "\r\n", encoding="ascii")
    elif mode == "loaded":
        if backup.is_file():
            shutil.copyfile(backup, ipset)
        else:
            raise FileNotFoundError(
                "бэкапа ipset-all.txt нет - сначала выполните "
                "«Update IPSet List» в service.bat или выберите any")


def _merge_domains(path: Path, domains: tuple[str, ...]) -> None:
    """Дописать домены в пользовательский список, не задевая чужие строки.

    Домен добавляется один раз; комментарий-заголовок Diaspas ставится
    при первой вставке, чтобы потом можно было отличить своё от чужого.
    """
    if not domains:
        return
    existing = path.read_text(encoding="utf-8",
                              errors="replace").splitlines() if path.is_file() else []
    present = {line.strip().lower() for line in existing}
    missing = [d for d in domains if d.lower() not in present]
    if not missing:
        return
    if existing and existing[-1].strip():
        existing.append("")
    # заголовок ставим один раз: повторные применения не плодят дубли
    if not any(line.strip() == "# Diaspas" for line in existing):
        existing.append("# Diaspas")
    existing.extend(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(existing) + "\r\n", encoding="utf-8")


def apply_files(root: Path, preset: Preset) -> None:
    """Всё, что можно сделать без прав: флаги, ipset, пользовательские списки.

    Для winws-layout (bol-van win-bundle) файловой структуры нет:
    ни lists\\ с user-списками, ни флагов Game Filter/IPSet - winws
    работает только по аргументам preset*.cmd.
    """
    from . import installs
    root = Path(root)
    layout = installs.detect_layout(root)["layout"]

    if layout == installs.LAYOUT_WINWS:
        if not preset.off:
            # стратегия winws-layout - файл preset*.cmd, а не general*.bat
            info = installs.detect_layout(root)
            winws_dir = info["winws_dir"] or root
            if preset.strategy and not (winws_dir / preset.strategy).is_file():
                names = ", ".join(available_strategies(root)) or "нет ни одной"
                raise FileNotFoundError(
                    f"в win-bundle установке нет {preset.strategy}: аргументы "
                    f"winws задают preset*.cmd; доступны: {names} - "
                    f"выберите стратегию в селекторе")
        return

    ensure_user_lists(root)   # до всего: без них winws не стартует
    if not preset.off:
        if preset.strategy and not (root / preset.strategy).is_file():
            raise FileNotFoundError(f"нет стратегии {preset.strategy}")
        _write_flag(root, preset.game_filter)
        _write_ipset(root, preset.ipset)
    _merge_domains(root / "lists" / "list-exclude-user.txt",
                   preset.exclude_domains)
    _merge_domains(root / "lists" / "list-general-user.txt",
                   preset.general_domains)


def apply(preset: Preset, root: Path, log=None) -> dict:
    """Полное применение: файлы (без прав) + служба (через помощника)."""
    if log:
        log("info", f"пресет «{preset.title}»: подготовка файлов")
    try:
        apply_files(root, preset)
    except (OSError, FileNotFoundError) as exc:
        if log:
            log("error", str(exc))
        return {"ok": False, "error": str(exc)}

    if preset.off:
        if log:
            log("info", "остановка и удаление службы zapret")
        res = service.remove(log=log)
        return _finish(preset, res, log)

    # Портовые диапазоны Game Filter: "12" у service.bat = фильтр выключен.
    gf = preset.game_filter if preset.game_filter in ("all", "tcp", "udp") else ""
    tcp = "1024-65535" if gf in ("all", "tcp") else "12"
    udp = "1024-65535" if gf in ("all", "udp") else "12"

    from . import installs
    from . import cmdparser
    info = installs.detect_layout(root)
    if info["layout"] == installs.LAYOUT_WINWS:
        # winws-layout: winws.exe рядом с preset*.cmd, аргументы из него
        winws_dir = info["winws_dir"] or Path(root)
        if log:
            log("info", f"пересоздание службы (winws-layout): "
                        f"{preset.strategy}")
        try:
            args = cmdparser.parse_preset(winws_dir / preset.strategy)
        except cmdparser.CmdParseError as exc:
            if log:
                log("error", str(exc))
            return {"ok": False, "error": str(exc)}
        binpath = f'"{winws_dir / "winws.exe"}" {args}'
    else:
        if log:
            log("info", f"пересоздание службы: {preset.strategy}")
        try:
            binpath = batparser.winws_command(root, preset.strategy, tcp, udp)
        except batparser.BatParseError as exc:
            if log:
                log("error", str(exc))
            return {"ok": False, "error": str(exc)}

    label = _label(preset.strategy)
    res = service.install_strategy(binpath, label, log=log)
    if not res.get("ok"):
        return _finish(preset, res, log)

    # Служба установлена - но установлена ≠ работает: winws может упасть
    # сразу (1067: битые пути, занятый WinDivert, антивирус). Раньше UI
    # показывал «применён», а обхода не было - теперь ждём факта.
    deadline = time.time() + 6
    st = service.state()
    while time.time() < deadline and not st.running:
        time.sleep(0.5)
        st = service.state()
    if not st.running:
        detail = (f"код выхода {st.exit_code}" if st.exit_code
                  else "код выхода неизвестен")
        msg = (f"служба установлена, но winws не запустился ({detail}) - "
               f"смотри журнал и winws в диспетчере задач")
        if log:
            log("error", msg)
        return {"ok": False, "error": msg, "preset": preset.key}
    if log:
        log("info", f"служба работает: pid {st.pid}")
    return _finish(preset, res, log)


def _label(strategy_file: str) -> str:
    from .paths import strategy_stem
    return strategy_stem(strategy_file)


def _finish(preset: Preset, res: dict, log=None) -> dict:
    if not res.get("ok"):
        err = str(res.get("error") or "операция не выполнена")
        if log:
            log("error", err)
        return {"ok": False, "error": err, "preset": preset.key}
    # Явный маркер активного пресета: три игровых пресета ведут себя
    # одинаково (SAFE_EXCLUDE сделал их сигнатуры идентичными), и
    # инференс по состоянию не может сказать, какой из них применили -
    # подсветка всегда показывала первый по порядку.
    _remember_active(preset.key)
    if log:
        log("info", f"пресет «{preset.title}» применён")
    return {"ok": True, "preset": preset.key,
            "state": res.get("state") or {}}


def _remember_active(key: str) -> None:
    """Сохранить ключ активного пресета в настройки.

    Успешное применение снимает дрейф: ``last_known_preset`` больше не
    нужен, маркер снова совпадает с фактом.
    """
    from . import paths
    settings = paths.read_settings()
    settings["active_preset"] = key
    settings.pop("last_known_preset", None)
    paths.write_settings(settings)


def active_preset_key() -> str | None:
    """Ключ пресета, применённого через Diaspas (None - неизвестно)."""
    from . import paths
    key = paths.read_settings().get("active_preset")
    return str(key) if key else None


def last_known_preset_key() -> str | None:
    """Последний известный пресет при дрейфе состояния.

    Запоминается, когда маркер не совпадает с фактом (например, после
    переключения стратегии через service.bat или автозапуска Windows).
    Используется как подсказка при восстановлении управления.
    """
    from . import paths
    key = paths.read_settings().get("last_known_preset")
    return str(key) if key else None


@dataclass(frozen=True)
class _Snapshot:
    """Что реально лежит в установке - всё, по чему сравниваются пресеты."""

    strategy_file: str
    game_filter: str
    ipset: str
    excludes: frozenset[str]
    layout: str = ""


def _snapshot(root: Path, state: service.ServiceState) -> _Snapshot:
    """Снимок состояния установки: стратегия из метки реестра + файлы."""
    root_path = Path(root)
    from .paths import strategy_stem
    from . import installs

    layout = installs.detect_layout(root_path)["layout"] or ""

    strategy_file = ""
    for name in _strategy_names(root_path):
        if strategy_stem(name) == state.strategy:
            strategy_file = name
            break
    if not strategy_file and layout == installs.LAYOUT_WINWS:
        # winws-layout: стратегии - preset*.cmd (лежат рядом с winws.exe,
        # не обязательно в корне), метка реестра = имя файла
        for name in available_strategies(root_path):
            if name == state.strategy or strategy_stem(name) == state.strategy:
                strategy_file = name
                break

    gf_flag = root_path / "utils" / "game_filter.enabled"
    game_filter = ""
    if gf_flag.is_file():
        game_filter = gf_flag.read_text(encoding="ascii",
                                        errors="replace").strip()

    ipset_file = root_path / "lists" / "ipset-all.txt"
    ipset = ""
    if ipset_file.is_file():
        text = ipset_file.read_text(encoding="ascii", errors="replace").strip()
        if not text:
            ipset = "any"
        elif text.splitlines()[0].strip() == _IPSET_NONE_MARKER:
            ipset = "none"
        else:
            ipset = "loaded"

    excludes: set[str] = set()
    exclude_file = root_path / "lists" / "list-exclude-user.txt"
    if exclude_file.is_file():
        for line in exclude_file.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                excludes.add(line.lower())

    return _Snapshot(strategy_file=strategy_file, game_filter=game_filter,
                     ipset=ipset, excludes=frozenset(excludes),
                     layout=layout)


def _matches(preset: Preset, snap: _Snapshot) -> bool:
    """Сигнара пресета (то, что он обещает) содержится в снимке."""
    if preset.off:
        return False   # off проверяется отдельно - по факту службы
    if snap.layout == "winws":
        # winws-layout: флагов Game Filter/IPSet и user-списков нет -
        # сравниваем только стратегию (файл preset*.cmd)
        return preset.strategy == snap.strategy_file
    if preset.strategy != snap.strategy_file:
        return False
    wanted_gf = preset.game_filter if preset.game_filter in (
        "all", "tcp", "udp") else ""
    if wanted_gf != snap.game_filter:
        return False
    if preset.ipset and preset.ipset != snap.ipset:
        return False
    return all(d.lower() in snap.excludes for d in preset.exclude_domains)


def current_preset(root: Path | None, state: service.ServiceState,
                   presets: list[Preset] | None = None,
                   active_key: str | None = None) -> Preset | None:
    """Какой пресет соответствует текущему состоянию (для подсветки).

    Порядок: сначала маркер ``active_preset`` (то, что реально
    применяли) - но только если его ожидания всё ещё выполняются;
    если состояние разошлось (правки через service.bat), маркер
    игнорируется и работает прежний инференс по снимку.

    ``None`` при наличии маркера = дрейф: вызывающая сторона (GUI)
    запоминает последний известный пресет, чтобы вернуть управление.
    """
    if not state.installed and not state.running:
        for preset in (presets or load_presets(root)):
            if preset.off:
                return preset
        return None
    if root is None or not state.strategy:
        return None

    all_presets = presets or load_presets(root)
    snap = _snapshot(root, state)

    # 1. маркер: применённый Diaspas'ом пресет, чьи ожидания совпали
    if active_key and active_key != "off":
        marked = next((p for p in all_presets if p.key == active_key), None)
        if marked is not None and _matches(marked, snap):
            return marked

    # 2. инференс: состояние может соответствовать нескольким пресетам
    # (списки накапливаются), поэтому выбираем точность покрытия
    best: Preset | None = None
    best_score = -1
    for preset in all_presets:
        if not _matches(preset, snap):
            continue
        score = sum(1 for d in preset.exclude_domains
                    if d.lower() in snap.excludes)
        if score > best_score:
            best, best_score = preset, score
    return best


def _strategy_names(root: Path) -> list[str]:
    from .paths import strategy_files
    return strategy_files(Path(root))
