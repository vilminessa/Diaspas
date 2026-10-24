"""Diaspas (διάσπασις) - точка входа.

Основной интерфейс - webview (pywebview/EdgeChromium, как в Synfronia):
ui/bundle.py со встроенным HTML/CSS/JS. Если webview недоступен (нет
WebView2, нет pywebview), откатываемся на Tkinter-окно ui/window.py -
утилита должна открываться, а не падать.

Запуск из исходников:

    python main.py           # webview
    python main.py --debug   # webview с DevTools (консоль JS)
    python main.py --min     # старт скрытым: окно открывается из трея
    python main.py --tk      # Tkinter-резерв, без попытки webview

Сборка: ``pyinstaller diaspas.spec``. Ручные прогоны - debug.bat.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Позволяет запускать из исходников без установки пакета:
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    argv = sys.argv[1:]

    # 1. webview - основной путь: ui.webapp сам импортирует pywebview и
    #    ui.bundle, поэтому ImportError здесь = нет зависимости или бандла
    if "--tk" not in argv:
        try:
            from ui.webapp import run
            return run()
        except ImportError as exc:
            sys.stderr.write(f"webview недоступен ({exc}) - Tkinter-резерв\n")
        except Exception:  # noqa: BLE001 - любая ошибка webview -> резерв
            import traceback
            traceback.print_exc()
            sys.stderr.write("webview не запустился - Tkinter-резерв\n")

    # 2. Tkinter - резервное окно (тот же core, тот же трей)
    from ui.window import main as tk_main
    return tk_main(hidden="--min" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
