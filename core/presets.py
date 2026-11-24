"""Пресеты: одна кнопка = стратегия + режимы + списки + перезапуск службы.

Пресет - это набор действий над установкой zapret:

* какая ``general*.bat`` станет стратегией службы;
* режим Game Filter (``utils/game_filter.enabled``): all/tcp/udp/нет файла;
* режим IPSet (``lists/ipset-all.txt``): any (пустой файл), none (заглушка
  203.0.113.113/32), loaded (реальный список из бэкапа);
* какие домены дописать в пользовательские списки - например ``ea.com``
  в исключения, чтобы zapret не трогал авторизацию EA;
* какие IP (CIDR) дописать в ``ipset-exclude-user.txt`` - единственное, что
  защищает адрес от десинка в правилах игрового фильтра: там нет
  ``--hostlist-exclude``, поэтому домен для них ничего не значит.

Применение пресета идёт по шагам без прав (запись файлов - пользовательские
списки), а пересоздание службы уходит в service.run_action с UAC-обходом.

Стратегия - не константа пресета: ``set_strategy`` сохраняет выбор
пользователя, а дефолт разрешается под layout папки (``general*.bat`` во
Flowseal, ``preset*.cmd`` в win-bundle). Переопределения хранятся ПО
УСТАНОВКЕ: наборы стратегий у версий zapret разные, и выбор ALT13 в 1.10.x
не должен ломать применение в 1.9.x, где такого файла нет.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import service, strategies

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
    exclude_ips: tuple[str, ...] = ()        # в ipset-exclude-user.txt (CIDR)
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

# Ключ «для любой установки»: плоское наследие одноверсионности и
# осознанный глобальный выбор живут под ним.
WILDCARD = "*"


def _root_keys(root: Path | str | None) -> list[str]:
    """Ключи, под которыми лежит выбор ЭТОЙ установки: uid, затем путь.

    Id стабилен при переименовании и переносе папки (см.
    installs.uid_of), поэтому выбор пользователя не теряется. Путь
    остаётся вторым ключом: установки, поставленные не Diaspas, своего
    id не имеют, а старые записи настроек лежат именно под путём.
    """
    if root is None:
        return []
    from . import installs
    path = str(Path(root))
    try:
        uid = installs.uid_of(path)
    except Exception:  # noqa: BLE001 - выбор не должен рвать чтение пресетов
        uid = ""
    return list(dict.fromkeys([k for k in (uid, path) if k]))


def _split_overrides(raw) -> tuple[dict, dict]:
    """Разделить переопределения на «по установке» и плоское наследие.

    Вид - ``{ключ установки: {ключ пресета: файл}}``. Плоский
    ``{ключ: файл}`` остался от одноверсионности: он перекрывал все
    установки сразу, поэтому выбор ALT13 в 1.10.x ломал применение в
    версии, где такого файла нет. Плоские записи читаются как
    «для любой установки» (то же, что ключ ``*``).
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


def _override_map(settings: dict, root: Path | str | None = None) -> dict[str, str]:
    """Что перекрыть для этой установки: наследие, wildcard, затем ключи установки."""
    nested, legacy = _split_overrides(settings.get("preset_strategies"))
    out = dict(legacy)                  # наследие == «для любой установки»
    out.update(nested.get(WILDCARD) or {})
    for key in _root_keys(root):
        out.update(nested.get(key) or {})
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
    overrides = _override_map(settings, root)

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
                exclude_ips=tuple(item.get("exclude_ips") or ()),
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
    return _override_map(paths.read_settings(), root).get(key, "")


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
    settings = paths.read_settings()
    nested, legacy = _split_overrides(settings.get("preset_strategies"))
    # Плоское наследие мигрирует в wildcard: смысл один - «для любой
    # установки», и его можно снять явным выбором.
    if legacy:
        wide = nested.setdefault(WILDCARD, {})
        for k, v in legacy.items():
            wide.setdefault(k, v)

    keys = _root_keys(root)
    own = keys[0] if keys else WILDCARD
    bucket = nested.setdefault(own, {})
    if strategy:
        bucket[key] = str(strategy)
    else:
        bucket.pop(key, None)
    if own != WILDCARD:
        # Явный выбор по установке снимает глобальную запись пресета:
        # иначе сброс «вернуть дефолт» здесь возвращал бы старую
        # глобальную стратегию.
        nested.get(WILDCARD, {}).pop(key, None)
    nested = {k: v for k, v in nested.items() if v}
    settings["preset_strategies"] = nested
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


def _merge_entries(path: Path, entries: tuple[str, ...]) -> None:
    """Дописать строки в пользовательский список, не задевая чужие.

    Работает и с доменами, и с CIDR: запись добавляется один раз, а
    комментарий-заголовок Diaspas ставится при первой вставке, чтобы потом
    можно было отличить своё от чужого.
    """
    if not entries:
        return
    existing = path.read_text(encoding="utf-8",
                              errors="replace").splitlines() if path.is_file() else []
    present = {line.strip().lower() for line in existing}
    missing = [d for d in entries if d.lower() not in present]
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


def apply_files(root: Path, preset: Preset,
                strategy_file: str | None = None) -> None:
    """Всё, что можно сделать без прав: флаги, ipset, пользовательские списки.

    ``strategy_file`` - стратегия, которую собираются ставить (выбор
    пользователя из :mod:`core.strategies`); по умолчанию - стратегия пресета.
    Проверяется существование именно того файла, который будет установлен.

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
            wanted = strategy_file or preset.strategy
            if wanted and not (winws_dir / wanted).is_file():
                names = ", ".join(available_strategies(root)) or "нет ни одной"
                raise FileNotFoundError(
                    f"в win-bundle установке нет {wanted}: аргументы "
                    f"winws задают preset*.cmd; доступны: {names} - "
                    f"выберите стратегию в селекторе")
        return

    ensure_user_lists(root)   # до всего: без них winws не стартует
    strategy = preset.strategy if strategy_file is None else strategy_file
    if not preset.off:
        if strategy and not (root / strategy).is_file():
            raise FileNotFoundError(f"нет стратегии {strategy}")
        _write_flag(root, preset.game_filter)
        _write_ipset(root, preset.ipset)
    _merge_entries(root / "lists" / "list-exclude-user.txt",
                   preset.exclude_domains)
    _merge_entries(root / "lists" / "ipset-exclude-user.txt",
                   preset.exclude_ips)
    _merge_entries(root / "lists" / "list-general-user.txt",
                   preset.general_domains)


def apply(preset: Preset, root: Path, log=None,
          strategy: str | None = None) -> dict:
    """Полное применение: файлы (без прав) + служба (через помощника).

    Стратегия берётся из выбора пользователя (:func:`strategies.resolve`),
    если такой выбор есть: ``Preset.strategy`` - значение по умолчанию, а
    не приказ. ``strategy`` переобходит оба источника (одноразовый выбор,
    например победитель перебора стратегий).
    """
    effective = strategy or strategies.resolve(preset.strategy, root)
    if log:
        log("info", f"пресет «{preset.title}»: подготовка файлов")
    try:
        apply_files(root, preset, effective)
    except (OSError, FileNotFoundError) as exc:
        if log:
            log("error", str(exc))
        return {"ok": False, "error": str(exc)}

    if preset.off:
        if log:
            log("info", "остановка и удаление службы zapret")
        res = service.remove(log=log)
        return _finish(preset, res, log)

    from . import cmdparser
    from . import installs
    info = installs.detect_layout(root)
    if info["layout"] == installs.LAYOUT_WINWS:
        # winws-layout: winws.exe рядом с preset*.cmd, аргументы из него
        winws_dir = info["winws_dir"] or Path(root)
        if log:
            log("info", f"пересоздание службы (winws-layout): "
                        f"{effective}")
        try:
            args = cmdparser.parse_preset(winws_dir / effective)
        except cmdparser.CmdParseError as exc:
            if log:
                log("error", str(exc))
            return {"ok": False, "error": str(exc)}
        binpath = f'"{winws_dir / "winws.exe"}" {args}'
        label = _label(effective)
        res = service.install_strategy(binpath, label, log=log)
    else:
        if log:
            log("info", f"пересоздание службы: {effective}")
        res = strategies.install(root, effective, preset.game_filter, log=log)
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
    return _finish(preset, res, log, effective)


def _label(strategy_file: str) -> str:
    """Короткая метка стратегии для журнала (ALT11, а не "general (ALT11)")."""
    from .paths import strategy_stem
    return strategy_stem(strategy_file)


def _finish(preset: Preset, res: dict, log=None, strategy: str = "") -> dict:
    if not res.get("ok"):
        err = str(res.get("error") or "операция не выполнена")
        if log:
            log("error", err)
        return {"ok": False, "error": err, "preset": preset.key}
    # Явный маркер активного пресета: три игровых пресета ведут себя
    # одинаково (SAFE_EXCLUDE сделал их сигнатуры идентичными), и
    # инференс по состоянию не может сказать, какой из них применили -
    # подсветка всегда показывала первый по порядку.
    remember_active(preset.key)
    # Заодно запоминаем поставленную стратегию: без этого смена пресета
    # каждый раз возвращала бы ALT11 из пресета вместо выбранной вручную.
    if strategy:
        strategies.remember(strategy)
    if log:
        log("info", f"пресет «{preset.title}» применён")
    return {"ok": True, "preset": preset.key,
            "strategy": strategy, "state": res.get("state") or {}}


def remember_active(key: str) -> None:
    """Сохранить ключ активного пресета в настройки.

    Успешное применение снимает дрейф: ``last_known_preset`` больше не
    нужен, маркер снова совпадает с фактом.
    """
    from . import paths
    settings = paths.read_settings()
    settings["active_preset"] = key
    settings.pop("last_known_preset", None)
    paths.write_settings(settings)


def suggested_preset() -> Preset:
    """Пресет, который предложить, когда службы нет: маркер или базовый.

    Маркер ``off`` означает «обход выключен намеренно» - предлагать его для
    создания службы нельзя, возвращаем базовый пресет без игровых фильтров.
    """
    all_presets = load_presets()
    key = active_preset_key()
    if key:
        marked = next((p for p in all_presets if p.key == key and not p.off),
                      None)
        if marked is not None:
            return marked
    return next((p for p in all_presets if not p.off), DEFAULT_PRESETS[0])


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
    """Что реально лежит в установке: режимы пресета + стоящая стратегия.

    Стратегию читаем для отображения и журнала: сравнение пресетов идёт по
    режимам (см. :func:`_matches`).
    """

    strategy_file: str
    game_filter: str
    ipset: str
    excludes: frozenset[str]
    excludes_ip: frozenset[str]
    layout: str = ""


def _read_list(path: Path) -> frozenset[str]:
    """Строки пользовательского списка: комментарии и пустые не считаем."""
    if not path.is_file():
        return frozenset()
    lines = {line.strip().lower()
             for line in path.read_text(encoding="utf-8",
                                        errors="replace").splitlines()}
    return frozenset(line for line in lines if line and not line.startswith("#"))


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

    return _Snapshot(strategy_file=strategy_file, game_filter=game_filter,
                     ipset=ipset,
                     excludes=_read_list(root_path / "lists" /
                                         "list-exclude-user.txt"),
                     excludes_ip=_read_list(root_path / "lists" /
                                            "ipset-exclude-user.txt"),
                     layout=layout)


def _matches(preset: Preset, snap: _Snapshot, *,
             ignore_strategy: bool = False) -> bool:
    """Сигнара пресета (то, что он обещает) содержится в снимке.

    Стратегия участвует: инференс не должен выдать пресет, чья стратегия
    не ставилась (иначе «взять ALT7» выглядел бы как чужой пресет).
    Маркер активного пресета проверяется с ``ignore_strategy=True``:
    маркер означает «пресет применялся», а выбор стратегии - отдельная
    ось (:mod:`core.strategies`) и не должен гасить подсветку после
    ручной смены ALT11 -> ALT7.
    """
    if preset.off:
        return False   # off проверяется отдельно - по факту службы
    if snap.layout == "winws":
        # winws-layout: флагов Game Filter/IPSet и user-списков нет -
        # сравниваем только стратегию (файл preset*.cmd); ignore_strategy
        # здесь не действует: без стратегии маркер совпал бы с любым
        # состоянием, а пресеты winws различаются только ей
        return preset.strategy == snap.strategy_file
    if not ignore_strategy and preset.strategy != snap.strategy_file:
        return False
    wanted_gf = preset.game_filter if preset.game_filter in (
        "all", "tcp", "udp") else ""
    if wanted_gf != snap.game_filter:
        return False
    if preset.ipset and preset.ipset != snap.ipset:
        return False
    if not all(d.lower() in snap.excludes for d in preset.exclude_domains):
        return False
    # IP-исключения: для правил игрового фильтра это единственная защита,
    # поэтому пресет считается применённым только когда CIDR действительно
    # лежит в ipset-exclude-user.txt.
    return all(cid.lower() in snap.excludes_ip for cid in preset.exclude_ips)


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

    # 1. маркер: применённый Diaspas'ом пресет, чьи ожидания совпали.
    # Стратегию при этом не спрашиваем: выбор стратегии (ALT11 -> ALT7)
    # не меняет режимы пресета и не должен гасить подсветку.
    if active_key and active_key != "off":
        marked = next((p for p in all_presets if p.key == active_key), None)
        if marked is not None and _matches(marked, snap,
                                           ignore_strategy=True):
            return marked

    # 2. инференс: состояние может соответствовать нескольким пресетам
    # (списки накапливаются), поэтому выбираем точность покрытия - и домены,
    # и IP: пресет с IP-исключением конкретнее, чем «Максимум для игр».
    best: Preset | None = None
    best_score = -1
    for preset in all_presets:
        if not _matches(preset, snap):
            continue
        score = sum(1 for d in preset.exclude_domains
                    if d.lower() in snap.excludes)
        score += sum(1 for cid in preset.exclude_ips
                     if cid.lower() in snap.excludes_ip)
        if score > best_score:
            best, best_score = preset, score
    return best


def _strategy_names(root: Path) -> list[str]:
    from .paths import strategy_files
    return strategy_files(Path(root))


# -- проверка обхода и откат -------------------------------------------------

def _quick_probe() -> dict:
    """Быстрая проверка обхода: базовые группы, короткий таймаут.

    verify не должен превращать применение пресета в минуту ожидания.
    """
    from . import probes
    return probes.probe_quick()


def _summary(report: dict) -> dict:
    return {"state": report.get("state") or "none",
            "count": int(report.get("count") or 0),
            "total": int(report.get("total") or 0)}


def restore_target(root: Path | str) -> Preset | None:
    """Пресет со стратегией, которые стоят СЕЙЧАС - чем откатывать.

    Распознанный пресет берётся как есть (с переопределённой стратегией).
    Если состояние службы не похоже ни на один пресет (правка через
    service.bat), собирается объект из цели-маркера и фактической
    стратегии: иначе откат «в дефолт ALT11» сам был бы поломкой.
    """
    root = Path(root)
    st = service.state()
    if not st.installed:
        return None
    presets = load_presets(root)
    marker = active_preset_key()
    current = current_preset(root, st, presets=presets, active_key=marker)
    if current is not None:
        return current

    strategy = current_strategy_file(root, st)
    key = (marker or last_known_preset_key()
           or next((p.key for p in presets if not p.off), ""))
    base = next((p for p in presets if p.key == key), None)
    if base is None or base.off:
        return None
    return replace(base, strategy=strategy or base.strategy)


def apply_verified(preset: Preset, root: Path, *, log=None,
                   probe=None, previous: Preset | None | bool = False) -> dict:
    """Применить пресет и, если обход пропал, вернуть прежнее состояние.

    Откат срабатывает только на доказанное ухудшение: после применения
    ни одна цель не отвечает, а до применения отвечали. При отсутствии
    сети (total == 0) пробы ничего не доказывают, и откат был бы
    ложным «починим» - поэтому он не делается.
    """
    probe = probe or _quick_probe
    prev = (restore_target(root) if previous is False else previous)
    before = _summary(probe()) if prev is not None else _summary(
        {"state": "none", "count": 0, "total": 0})

    res = apply(preset, root, log=log)
    if not res.get("ok"):
        return res
    if prev is None:
        # Службы до применения не было: сравнивать не с чем, а пробы при
        # этом дали бы ложный «ухудшили» вердикт на пустом месте.
        return res

    after = _summary(probe())
    if (prev is not None and prev.key != preset.key
            and after["total"] and after["count"] == 0
            and before["count"] > 0):
        if log:
            log("warning", f"обход пропал после «{preset.title}» "
                           f"({before['count']}/{before['total']} -> 0/"
                           f"{after['total']}): откат на «{prev.title}»")
        back = apply(prev, root, log=log)
        res.update({"ok": False, "rolled_back": True,
                    "error": "обход пропал после применения — возвращено "
                             "прежнее состояние",
                    "restore_preset": prev.key, "restore_ok": back.get("ok"),
                    "before": before, "after": after})
        return res
    res.update({"before": before, "after": after})
    return res


def try_strategy(preset: Preset, root: Path, *, log=None, probe=None) -> dict:
    """«Попробовать без применения»: поставить стратегию, проверить, вернуть.

    Смысл в том, чтобы оценить рекомендацию перебора до того, как
    пользователь согласится пересоздать службу. Результат - сравнение
    before/after, а не обещание: прежнее состояние восстанавливается
    обязательно, даже если пробы упали.
    """
    probe = probe or _quick_probe
    previous = restore_target(root)
    if previous is None:
        return {"ok": False,
                "error": "служба не установлена — применять нечего, "
                         "сначала выберите пресет"}
    if log:
        log("info", f"проба стратегии {_label(preset.strategy)} без применения")

    before = _summary(probe())
    res = apply(preset, root, log=log)
    if not res.get("ok"):
        back = apply(previous, root, log=log)
        return {"ok": False, "error": res.get("error"),
                "restored": bool(back.get("ok")),
                "restore_preset": previous.key}

    after = _summary(probe())
    back = apply(previous, root, log=log)
    better = after["count"] > before["count"]
    if log:
        log("info", f"проба: {before['count']}/{before['total']} -> "
                    f"{after['count']}/{after['total']}, возврат "
                    f"{'ок' if back.get('ok') else 'не ок'}")
    return {"ok": True, "trial": True, "better": better,
            "before": before, "after": after,
            "restored": bool(back.get("ok")),
            "restore_preset": previous.key,
            "restore_error": back.get("error")}


# -- перенос связки «пресет + стратегия + списки» ----------------------------

def export_bundle(preset: Preset) -> dict:
    """Связка пресета в переносимом виде (для другой машины/версии)."""
    return {"format": 1,
            "key": preset.key, "title": preset.title,
            "strategy": preset.strategy,
            "game_filter": preset.game_filter, "ipset": preset.ipset,
            "exclude_domains": list(preset.exclude_domains),
            "general_domains": list(preset.general_domains),
            "description": preset.description, "off": preset.off}


def import_bundle(data: dict, root: Path | str, *,
                  key: str | None = None) -> str:
    """Добавить перенесённую связку как пользовательский пресет.

    Возвращает ключ нового пресета. Стратегия проверяется по ЭТОЙ
    установке: перенос связки из 1.10.x в 1.9.x не должен оставлять
    ссылку на файл, которого нет (такая связка ложится на дефолт, и
    пользователь меняет стратегию селектором).
    """
    from . import paths
    if not isinstance(data, dict):
        raise ValueError("ожидался словарь связки")
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("в связке нет названия пресета")

    base = slug_key(str(key or data.get("key") or "").strip() or title)
    taken = {p.key for p in load_presets(root)}
    new_key, suffix = base, 2
    while new_key in taken:
        new_key = f"{base}_{suffix}"
        suffix += 1

    strategy = str(data.get("strategy") or "")
    files = available_strategies(root)
    if strategy and strategy not in files:
        # Связка перенесена из другой версии: чужой файл стратегии не
        # записывается, иначе каждое применение падало бы «нет стратегии».
        # Берётся первая стратегия этой установки - её всегда можно
        # поменять селектором.
        strategy = files[0] if files else ""
    settings = paths.read_settings()

    item = {"key": new_key, "title": title,
            "strategy": strategy,
            "game_filter": str(data.get("game_filter") or ""),
            "ipset": str(data.get("ipset") or ""),
            "exclude_domains": [str(d) for d in
                                (data.get("exclude_domains") or ())],
            "general_domains": [str(d) for d in
                                (data.get("general_domains") or ())],
            "description": str(data.get("description") or "")}
    custom = [p for p in (settings.get("presets") or [])
              if str(p.get("key")) != new_key]
    custom.append(item)
    settings["presets"] = custom
    paths.write_settings(settings)
    return new_key


def slug_key(value: str) -> str:
    """Ключ пресета: буквы/цифры/_/- ; всё остальное сворачивается в «_»."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_")
    return cleaned or "custom"

