#!/usr/bin/env python3
"""Сборка ui_src/ в ui/bundle.py.

HTML/CSS/JS встраиваются в Python-модуль: exe в onefile не ищет ресурсы
на диске, а check_ui.py линтует исходники ДО генерации — в CI порядок
такой: check → build → pytest.

Использование:
    python tools/build_ui.py          # пересобрать bundle.py
    python tools/build_ui.py --check  # сверить актуальность (для CI)
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "ui_src"
OUT = ROOT / "ui" / "bundle.py"

# порядок встраивания: index.html ссылается на app.css и app.js
PARTS = ("index.html", "app.css", "app.js")


def digests() -> dict[str, str]:
    """SHA-256 частей с нормализацией переводов строк.

    Без нормализации check падал в CI: git отдаёт CRLF (autocrlf), а
    bundle.py хэшировался от локальных LF - «расхождение» чисто из-за
    платформы. Единица сравнения - текст, а не байты с \r\n.
    """
    result = {}
    for name in PARTS:
        data = (SRC / name).read_bytes().replace(b"\r\n", b"\n")
        result[name] = hashlib.sha256(data).hexdigest()
    return result


def build() -> str:
    index = (SRC / "index.html").read_text(encoding="utf-8")
    css = (SRC / "app.css").read_text(encoding="utf-8")
    js = (SRC / "app.js").read_text(encoding="utf-8")

    # инлайн вместо <link>/<script src>: load_html не резолвит внешние пути
    if 'href="app.css"' not in index:
        raise SystemExit("index.html: нет ссылки на app.css")
    if 'src="app.js"' not in index:
        raise SystemExit("index.html: нет ссылки на app.js")
    index = index.replace('<link rel="stylesheet" href="app.css">',
                          "<style>\n" + css + "\n</style>")
    index = index.replace('<script src="app.js"></script>',
                          "<script>\n" + js + "\n</script>")

    # защита от разрыва инлайн-скрипта
    body = index[index.index("<script>"):]
    if "</scr" + "ipt>" not in body:
        raise SystemExit("инлайн-script не закрыт")

    hashes = ", ".join(f'"{k}": "{v}"' for k, v in digests().items())
    content = (
        '"""Сгенерированный бандл UI. НЕ РЕДАКТИРОВАТЬ руками.\n'
        '\n'
        'Источник: ui_src/ (index.html, app.css, app.js).\n'
        'Пересборка: python tools/build_ui.py\n'
        '"""\n'
        "\n"
        f"HASHES = {{{hashes}}}\n"
        "\n"
        f"INDEX_HTML = {index!r}\n"
    )
    return content


def main() -> int:
    check = "--check" in sys.argv
    if not (SRC / "index.html").is_file():
        print("нет ui_src/index.html", file=sys.stderr)
        return 1

    if check:
        if not OUT.is_file():
            print("ui/bundle.py отсутствует - запустите tools/build_ui.py",
                  file=sys.stderr)
            return 1
        sys.path.insert(0, str(ROOT))
        from ui import bundle  # type: ignore
        actual = digests()
        if getattr(bundle, "HASHES", None) != actual:
            stale = [k for k in actual
                     if bundle.HASHES.get(k) != actual[k]]
            print(f"ui/bundle.py устарел (изменены: {', '.join(stale)}) - "
                  "запустите tools/build_ui.py", file=sys.stderr)
            return 1
        print("ui/bundle.py актуален")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = build()
    if OUT.is_file() and OUT.read_text(encoding="utf-8") == text:
        print("ui/bundle.py без изменений")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"ui/bundle.py: {len(text)} символов, части: {', '.join(PARTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
