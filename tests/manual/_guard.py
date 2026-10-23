"""Порог для живых прогонов: они трогают реальную систему.

Скрипты этого каталога пересоздают службу zapret, правят пользовательские
списки доменов и скачивают релизы в %LOCALAPPDATA%\\Diaspas. Случайный
запуск (или запуск в CI) менял бы живую установку и её настройки, поэтому
они выполняются только по явной команде:

    DIASPAS_LIVE=1 python tests/manual/run_select.py
"""

from __future__ import annotations

import os
import sys


def require_live(what: str = "этот прогон") -> None:
    """Выйти без действия, если живой прогон не подтверждён переменной."""
    if os.environ.get("DIASPAS_LIVE") == "1":
        return
    first = (what or "этот прогон").strip().splitlines()[0]
    sys.exit(f"{first}: трогает живую систему (служба, списки доменов, "
             f"настройки). Подтвердите явным DIASPAS_LIVE=1")
