"""Diaspas (διάσπασις) - точка входа.

Основной интерфейс - webview (pywebview/EdgeChromium, как в Synfronia):
ui/bundle.py со встроенным HTML/CSS/JS. Если webview недоступен (нет
WebView2, нет pywebview), откатываемся на Tkinter-окно ui/window.py -
утилита должна открываться, а не падать.

Запуск из исходников: ``python main.py``.
Сборка: ``pyinstaller diaspas.spec``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Позволяет запускать из исходников без установки пакета:
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    # 1. webview - основной путь: ui.webapp сам импортирует pywebview и
    #    ui.bundle, поэтому ImportError здесь = нет зависимости или бандла
    try:
        from ui.webapp import run
        return run()
    except ImportError as exc:
        sys.stderr.write(f"webview недоступен ({exc}) - Tkinter-резерв\n")
    except Exception:  # noqa: BLE001 - любая ошибка webview -> резерв
        import traceback
        traceback.print_exc()
        sys.stderr.write("webview не запустился - Tkinter-резерв\n")

    # 2. Tkinter - резервное окно (тот же core)
    from ui.window import App
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
