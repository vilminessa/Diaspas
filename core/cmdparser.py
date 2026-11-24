"""Разбор preset*.cmd из bol-van/zapret-win-bundle в аргументы winws.

Структура winws-layout: winws.exe лежит РЯДОМ с preset*.cmd (не в bin\\),
списки - в files\\, фильтры raw - в windivert.filter\\. Реальный синтаксис
(проверен на preset1_example.cmd):

    start "zapret" /min "%~dp0winws.exe" ^
    --wf-tcp=80,443 ^
    --wf-raw-part=@"%~dp0windivert.filter\\windivert_part.discord_media.txt" ^
    --filter-tcp=443 --hostlist="%~dp0files\\list-youtube.txt" ^
    --dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com --new

Отличия от general*.bat Flowseal, которые ломали бы batparser:

* ``%~dp0`` вместо ``%BIN%``/``%LISTS%`` - раскрывается в папку самого .cmd;
* значения содержат ВЛОЖЕННЫЕ ``=`` (sni=www.google.com) и запятые -
  токенизировать по ``=``/```,`` нельзя, только по пробелам;
* ``--wf-raw-part=@<файл>`` - значение с ``@`` и кавычками.

Как и batparser, результат пригоден для BinaryPathName службы.
"""

from __future__ import annotations

from pathlib import Path


class CmdParseError(ValueError):
    """Файл пресета непригоден: нет winws.exe."""


def _split_tokens(line: str) -> list[str]:
    """Токенизация только по пробелам/табам.

    Запятые и равные знаки - часть значений (--wf-tcp=80,443,
    sni=www.google.com), их разрезание ломает аргументы.
    """
    return [tok for tok in line.replace("\t", " ").split(" ") if tok]


def parse_preset(path: Path) -> str:
    """Аргументы winws для preset*.cmd (путь к winws.exe НЕ включается).

    %~dp0 раскрывается в папку файла с завершающим разделителем путей,
    ``^`` (продолжение строки) отбрасывается, кавычка в начале токена
    обрабатывается как в batparser (относительный путь получает папку).
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CmdParseError(f"не удалось прочитать {path}: {exc}") from exc

    root = str(path.parent)
    if not root.endswith("\\"):
        root += "\\"
    # %~dp0 - папка самого скрипта; в cmd это она же
    raw = raw.replace("%~dp0", root)

    parts: list[str] = []
    for line in raw.splitlines():
        for token in _split_tokens(line):
            if token in ("^", "^^"):
                continue
            # кавычки остаются как есть: winws разбирает --opt="path"
            # сам (C-рантайм), а пути установки без пробелов
            parts.append(token)

    args = " ".join(parts)
    if "winws.exe" not in args:
        raise CmdParseError(f"в {path.name} нет ссылки на winws.exe")
    leftover = [
        tok for tok in ("%~dp0", "%BIN%", "%LISTS%") if tok in args]
    if leftover:
        raise CmdParseError(
            f"в {path.name} не раскрыты: {', '.join(leftover)}")
    for bad in ("!", "&", "|", "<", ">"):
        if bad in args:
            raise CmdParseError(
                f"в {path.name} недопустимый символ {bad!r} в аргументах")
    return args


def preset_files(root: Path) -> list[str]:
    """preset*.cmd установки в естественном порядке (preset1, preset2...)."""
    import re

    def natural(name: str) -> str:
        return re.sub(r"\d+", lambda m: m.group(0).zfill(8), name)

    return sorted([p.name for p in Path(root).glob("preset*.cmd")],
                  key=natural)
