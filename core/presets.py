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
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
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
        key="the_finals",
        title="The Finals",
        strategy="general (ALT11).bat",
        game_filter="all", ipset="any",
        exclude_domains=SAFE_EXCLUDE + ("embark.io", "api.embark.io"),
        exclude_ips=("34.49.47.233/32",),
        general_domains=(),
        description="Игровые порты TCP и UDP открыты: серверы матча идут "
                    "за пределами стандартных диапазонов zapret (443 и "
                    "диапазоны Discord/Stun). Бэкенд Embark отвечает и без "
                    "обхода, поэтому в исключениях и по домену, и по IP: "
                    "правила игрового фильтра не читают hostlist-exclude, "
                    "только ipset-exclude. Домены самой игры припаркованы "
                    "(лендер «for sale») и в пробах не участвуют.",
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


def load_presets() -> list[Preset]:
    """Пресеты по умолчанию + пользовательские из настроек (если есть)."""
    from . import paths
    custom = paths.read_settings().get("presets") or []
    presets: list[Preset] = list(DEFAULT_PRESETS)
    for item in custom:
        try:
            presets.append(Preset(
                key=str(item["key"]),
                title=str(item["title"]),
                strategy=str(item.get("strategy") or ""),
                game_filter=str(item.get("game_filter") or ""),
                ipset=str(item.get("ipset") or ""),
                exclude_domains=tuple(item.get("exclude_domains") or ()),
                exclude_ips=tuple(item.get("exclude_ips") or ()),
                general_domains=tuple(item.get("general_domains") or ()),
                description=str(item.get("description") or ""),
            ))
        except (KeyError, TypeError):
            continue
    return presets


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
    """
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

    if log:
        log("info", f"пересоздание службы: {effective}")
    res = strategies.install(root, effective, preset.game_filter, log=log)
    return _finish(preset, res, log, effective)


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
    """Сохранить ключ активного пресета в настройки."""
    from . import paths
    settings = paths.read_settings()
    settings["active_preset"] = key
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

    strategy_file = ""
    for name in _strategy_names(root):
        if strategy_stem(name) == state.strategy:
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
                                            "ipset-exclude-user.txt"))


def _matches(preset: Preset, snap: _Snapshot) -> bool:
    """Сигнара пресета (то, что он обещает) содержится в снимке.

    Стратегию не сравниваем: она теперь отдельная ось
    (:mod:`core.strategies`), а пресет описывает режимы. Подсветка обязана
    показывать применённый пресет даже после ручной смены ALT11 -> ALT7,
    иначе выбор стратегии «гасил» бы подсветку.
    """
    if preset.off:
        return False   # off проверяется отдельно - по факту службы
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
    """
    if not state.installed and not state.running:
        for preset in (presets or load_presets()):
            if preset.off:
                return preset
        return None
    if root is None or not state.strategy:
        return None

    all_presets = presets or load_presets()
    snap = _snapshot(root, state)

    # 1. маркер: применённый Diaspas'ом пресет, чьи ожидания совпали
    if active_key and active_key != "off":
        marked = next((p for p in all_presets if p.key == active_key), None)
        if marked is not None and _matches(marked, snap):
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
