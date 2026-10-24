#!/usr/bin/env python3
r"""Вписывает версию из тега в рабочее дерево - коммиты с бампом не нужны.

    python tools/stamp_version.py v0.1.2
    python tools/stamp_version.py b0.1.2

Что делает: version.py получает оба значения - числовую ``__version__``
(``0.1.2``) и подпись ``BUILD_LABEL`` (полный тег ``v0.1.2`` / ``b0.1.2``).
Замена байтовая внутри строки: переводы строк и кодировка файла не
трогются, лишний diff не появляется, повторный запуск идемпотентен.

Кто зовёт: workflow ``release`` и ``beta`` в джобе build перед PyInstaller -
VersionInfo exe и подпись интерфейса уезжают в бандл уже с теговой версией;
в git ничего не попадает (рабочее дерево CI одноразовое). Локально - перед
ручной сборкой exe из тега.

Формат тега: ``[vb]X.Y.Z`` или ``[vb]X.Y.Z.N`` - максимум четыре компонента
(Windows VersionInfo не принимает больше). Префикс v/b сохраняется в
BUILD_LABEL: релиз покажет ``v0.1.2``, бета - ``b0.1.2``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows-раннер печатает в cp1252; кириллица в выводе падает
# UnicodeEncodeError прямо на успешной проверке (тот же приём, что в CI).
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^([vb])(\d+\.\d+\.\d+(?:\.\d+)?)$")


def stamp(tag: str) -> tuple[str, str]:
    """(числовая версия, подпись) из тега; ValueError - формат не распознан."""
    clean = (tag or "").strip()
    match = TAG_RE.match(clean)
    if not match:
        raise ValueError(
            f"тег {clean!r} не распознан: ожидается v0.1.2 или b0.1.2")
    return match.group(2), clean


def _replace(path: Path, pattern: bytes, replacement: bytes) -> bool:
    """Байтовая замена внутри файла: кодировки и переводы строк не трогаем."""
    raw = path.read_bytes()
    updated, count = re.subn(pattern, replacement, raw, count=1)
    if count:
        path.write_bytes(updated)
    return bool(count)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    try:
        version, label = stamp(argv[1])
    except ValueError as exc:
        print(f"ошибка: {exc}")
        return 1

    path = ROOT / "version.py"
    ok_ver = _replace(path, rb'__version__\s*=\s*"[^"]*"',
                      f'__version__ = "{version}"'.encode("ascii"))
    ok_label = _replace(path, rb'BUILD_LABEL\s*=\s*"[^"]*"',
                        f'BUILD_LABEL = "{label}"'.encode("ascii"))
    if not (ok_ver and ok_label):
        print(f"ошибка: не нашёл поля для записи "
              f"(version={ok_ver}, label={ok_label})")
        return 1

    print(f"version.py: __version__ = {version}, BUILD_LABEL = {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
