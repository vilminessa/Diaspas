"""Серилизация операций с службой и одна точка отмены.

UI блокирует кнопки на время операции, но pywebview вызывает api-методы
из пула потоков: двойной клик, «применить пресет» во время переключения
версии или скачивание релиза поверх обновления могут пересоздать службу
в середине другой операции. Единственный честный способ это исключить -
одна блокировка на все операции с установкой.

Отмена нужна для скачивания (большой zip тянется долго): прогресс
проверяет флаг и прерывает загрузку, частичный файл удаляется.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.Lock()
_cancel = threading.Event()
_current = ""


class BusyError(RuntimeError):
    """Другая операция уже идёт: параллельные операции с службой недопустимы."""


class Cancelled(RuntimeError):
    """Операция прервана по запросу пользователя."""


@contextmanager
def operation(name: str):
    """Выполнить `name` единолично. BusyError, если занято другой операцией."""
    global _current
    if not _lock.acquire(blocking=False):
        raise BusyError(f"уже выполняется «{_current or 'операция'}» - "
                        f"дождитесь её окончания или отмените")
    _cancel.clear()
    _current = name
    try:
        yield name
    finally:
        _current = ""
        _cancel.clear()
        _lock.release()


def cancel() -> bool:
    """Попросить текущую операцию остановиться на ближайшей проверке."""
    if _current:
        _cancel.set()
        return True
    return False


def cancelled() -> bool:
    return _cancel.is_set()


def check_cancel() -> None:
    """Точка проверки: долгий цикл прерывается здесь."""
    if _cancel.is_set():
        raise Cancelled("операция отменена")


def current() -> str:
    return _current
