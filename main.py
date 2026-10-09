"""Diaspas (διάσπασις) - точка входа.

Запуск из исходников: ``python main.py``.
Сборка: ``pyinstaller diaspas.spec``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Позволяет запускать из исходников без установки пакета:
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.window import App  # noqa: E402


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
