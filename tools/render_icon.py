"""Ассеты метки: assets/icon.svg и assets/icon.ico из одного описания формы.

Форма живёт в :mod:`core.icons` (:data:`MARK`), поэтому SVG и ICO не могут
расойтись: генерация детерминирована, и `--check` сверяет файлы с ней - тот же
принцип, что `tools/build_ui.py --check` для бандла.

ICO берётся PyInstaller'ом (иконка exe -> панель задач), .NET (иконка окна в
webview) и Tk (`iconbitmap`). Инк в ассетах нейтральный средний тон: метка
читается и на тёмной, и на светлой панели задач; трей красит её по состоянию
(см. `core.tray.ICON_TINTS`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import icons  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SVG_PATH = REPO / "assets" / "icon.svg"
ICO_PATH = REPO / "assets" / "icon.ico"

# Инк ассета: muted из дизайн-системы - средний тон, видимый на обеих темах
ASSET_INK = icons.TINTS["off"]


def render_svg() -> bytes:
    return icons.svg_text().encode("utf-8")


def render_ico() -> bytes:
    return icons.ico_bytes(sizes=(16, 32, 48, 64), rgb=ASSET_INK)


def as_lf(data: bytes) -> bytes:
    """Текст ассета в LF: git с autocrlf отдаёт рабочую копию с CRLF.

    SVG - текст, и единица сравнения его содержимое, а не переводы строк:
    без нормализации сверка падала в CI на Windows-runner (тот же урок, что
    `build_ui.digests`). ICO - бинарный файл, git его не переписывает, поэтому
    он сравнивается по байтам как есть.
    """
    return data.replace(b"\r\n", b"\n")


def write_assets() -> list[tuple[Path, int]]:
    SVG_PATH.parent.mkdir(exist_ok=True)
    out = []
    for path, data in ((SVG_PATH, render_svg()), (ICO_PATH, render_ico())):
        path.write_bytes(data)
        out.append((path, len(data)))
    return out


def check_assets() -> int:
    """Сверка ассетов с генерацией; 1 - файлы устарели (или их нет)."""
    rc = 0
    for path, data, text in ((SVG_PATH, render_svg(), True),
                             (ICO_PATH, render_ico(), False)):
        if not path.is_file():
            print(f"FAIL {path}: нет файла (сгенерируй tools/render_icon.py)")
            rc = 1
            continue
        current = path.read_bytes()
        expected = as_lf(data) if text else data
        if as_lf(current) != expected:
            print(f"FAIL {path}: расходится с core.icons "
                  f"({len(current)} байт против {len(expected)}) - "
                  f"выполни tools/render_icon.py")
            rc = 1
    if rc == 0:
        print(f"OK  ассеты актуальны: {SVG_PATH.name} {SVG_PATH.stat().st_size} байт, "
              f"{ICO_PATH.name} {ICO_PATH.stat().st_size} байт")
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="генерация метки Diaspas")
    parser.add_argument("--check", action="store_true",
                        help="не писать, только сверить assets с core.icons")
    args = parser.parse_args(argv)
    if args.check:
        return check_assets()
    for path, size in write_assets():
        print(f"{path.relative_to(REPO)}: {size} байт")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
