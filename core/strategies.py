"""Каталог стратегий установки: что файл реально делает.

Стратегия в UI раньше была просто именем файла («ALT7»), и выбрать её
«вслепую» можно было только после перебора. Здесь разбор уже существующих
парсеров (batparser / cmdparser) превращается в читаемое описание: список
флагов winws, ключевые признаки (какие порты фильтруются, способ desync,
hostlist-файлы) и diff относительно текущей стратегии.

Ничего не применяется и не устанавливается: модуль только читает файлы
установки, поэтому его можно звать из UI на каждое открытие карточки.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import batparser, cmdparser, installs, paths


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
