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
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
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
    existing.append("# Diaspas")
    existing.extend(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(existing) + "\r\n", encoding="utf-8")


def apply_files(root: Path, preset: Preset) -> None:
    """Всё, что можно сделать без прав: флаги, ipset, пользовательские списки."""
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
    if log:
        log("info", f"пресет «{preset.title}» применён")
    return {"ok": True, "preset": preset.key,
            "state": res.get("state") or {}}


def current_preset(root: Path | None, state: service.ServiceState,
                   presets: list[Preset] | None = None) -> Preset | None:
    """Какой пресет соответствует текущему состоянию (для подсветки)."""
    if not state.installed and not state.running:
        for preset in (presets or load_presets()):
            if preset.off:
                return preset
        return None
    if root is None or not state.strategy:
        return None
    root_path = Path(root)
    # Метка в реестре - имя без .bat (как пишет service.bat)
    from .paths import strategy_stem
    strategy_file = ""
    for name in _strategy_names(root):
        if strategy_stem(name) == state.strategy:
            strategy_file = name
            break
    if not strategy_file:
        return None

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

    exclude_file = root_path / "lists" / "list-exclude-user.txt"
    excludes: set[str] = set()
    if exclude_file.is_file():
        for line in exclude_file.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                excludes.add(line.lower())

    # Состояние может соответствовать нескольким пресетам (списки
    # накапливаются: и ea.com, и ubisoft.com в исключениях). Выбираем
    # того, чьи exclude_domains точнее всего покрыты - иначе подсветка
    # показывала бы всегда первый по порядку пресет.
    best: Preset | None = None
    best_score = -1
    for preset in (presets or load_presets()):
        if preset.off:
            continue
        if preset.strategy != strategy_file:
            continue
        wanted_gf = preset.game_filter if preset.game_filter in (
            "all", "tcp", "udp") else ""
        if wanted_gf != game_filter:
            continue
        if preset.ipset and preset.ipset != ipset:
            continue
        if any(d.lower() not in excludes for d in preset.exclude_domains):
            continue
        score = sum(1 for d in preset.exclude_domains
                    if d.lower() in excludes)
        if score > best_score:
            best, best_score = preset, score
    return best


def _strategy_names(root: Path) -> list[str]:
    from .paths import strategy_files
    return strategy_files(Path(root))
