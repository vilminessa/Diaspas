"""Персистентный журнал Diaspas.

Окно показывает строки в памяти - при закрытии они исчезают, поэтому
любая ошибка «в журнале» без файла недоступна для диагностики. Этот модуль
пишет то же самое на диск и служит единой точкой правды:

    %LOCALAPPDATA%\\Diaspas\\diaspas.log

Ротация: при превышении LOG_LIMIT байт текущий файл переезжает в
``diaspas.log.old`` (одна копия, без бесконечной цепочки).

Потокобезопасность: запись под одним замком - в журнал пишут и главный
поток (GUI), и worker'ы, и callbacks прогресса.
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from . import paths

LOG_NAME = "diaspas.log"
LOG_LIMIT = 512 * 1024  # дальше - ротация в .old

_lock = threading.Lock()
_initialized = False


def log_path() -> Path:
    return paths.app_dir() / LOG_NAME


def _ensure_dir() -> Path:
    global _initialized
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _initialized:
        _initialized = True
        _rotate_if_needed(path)
    return path


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > LOG_LIMIT:
            old = path.with_suffix(".log.old")
            old.unlink(missing_ok=True)
            path.replace(old)
    except OSError:
        pass  # ротация не должна ронять запись


def write(level: str, text: str) -> None:
    """Одна строка журнала: [YYYY-MM-DD HH:MM:SS] [level] text.

    Файл создаётся с UTF-8 BOM: без него PowerShell 5.1 и старые редакторы
    читают кириллицу как ANSI и диагностика «посмотри лог» превращается
    в кракозябры.
    """
    path = _ensure_dir()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [{level}] {text}\n"
    with _lock:
        _rotate_if_needed(path)
        try:
            fresh = not path.is_file() or path.stat().st_size == 0
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                if fresh:
                    fh.write("﻿")
                fh.write(line)
        except OSError:
            pass  # диск недоступен - журнал не должен ронять приложение


def exception(context: str) -> None:
    """Полный traceback в журнал - ровно то, чего не хватало при отладке."""
    write("error", f"{context}: {traceback.format_exc()}")


def info(text: str) -> None:
    write("info", text)


def warning(text: str) -> None:
    write("warning", text)


def error(text: str) -> None:
    write("error", text)


def gui_sink(level: str, text: str) -> None:
    """Подпись для передачи в GUI: пишет и в окно (через callback), и в файл.

    На практике GUI собирает лог сам; эта функция - для worker'ов, которым
    нужен один вызов вместо двух.
    """
    write(level, text)


def tail(n: int = 40) -> list[str]:
    """Последние n строк журнала - для показа в окне при старте."""
    try:
        path = log_path()
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []
