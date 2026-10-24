"""Декодирование вывода внешних инструментов для ручных прогонов.

PowerShell и schtasks на русской Windows пишут OEM-кодовку (cp866), git -
UTF-8. `subprocess.run(text=True)` использует локальную кодовку и падает
UnicodeDecodeError в потоке читателя, оставляя stdout=None - вывод пропадал
без всякой ошибки.

Порядок: UTF-8, затем кодовая страница консоли вывода (её и использует
дочерний процесс), затем cp866/cp1251, и если ничего не подошло - replace.
"""

import codecs
import ctypes

FALLBACKS = ("cp866", "cp1251")


def _output_codepage() -> int:
    """Кодовая страница, в которой консоль читает вывод дочерних процессов."""
    try:
        return int(ctypes.windll.kernel32.GetConsoleOutputCP()) or 0
    except (AttributeError, OSError):
        return 0


def _encoding_for(codepage: int) -> str | None:
    """Кодовая страница Windows -> имя кодировки Python (65001 = UTF-8)."""
    if codepage == 65001:
        return "utf-8"
    try:
        codecs.lookup(f"cp{codepage}")
        return f"cp{codepage}"
    except LookupError:
        return None


def decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    encoding = _encoding_for(_output_codepage())
    if encoding:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass

    for encoding in FALLBACKS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
