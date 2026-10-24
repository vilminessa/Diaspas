"""Декодирование вывода внешних инструментов - класс ошибки, который уже
ловил twice: subprocess(text=True) падает UnicodeDecodeError в потоке
читателя и оставляет stdout=None, вывод исчезает без ошибки.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402
from _console import decode  # noqa: E402


def test_utf8_passthrough():
    assert decode("ветка main".encode("utf-8")) == "ветка main"


def test_oem_codepage_decodes():
    # schtasks и PowerShell на русской Windows пишут cp866
    assert decode("задача не найдена".encode("cp866")) == "задача не найдена"


def test_console_codepage_is_preferred(monkeypatch):
    """Кодовая страница консоли вывода - то, что реально использовал процесс."""
    monkeypatch.setattr(_console, "_output_codepage", lambda: 1251)
    assert decode("нет такого файла".encode("cp1251")) == "нет такого файла"

    monkeypatch.setattr(_console, "_output_codepage", lambda: 866)
    assert decode("задача не найдена".encode("cp866")) == "задача не найдена"


def test_utf8_wins_over_codepage(monkeypatch):
    monkeypatch.setattr(_console, "_output_codepage", lambda: 866)
    assert decode("git log по UTF-8".encode("utf-8")) == "git log по UTF-8"


def test_invalid_bytes_do_not_raise():
    """Неподходящие байты - replace, а не исключение: вывод не должен теряться."""
    text = decode(b"\xff\xfe\x8f\x00 net")
    assert "net" in text


def test_empty_is_empty_string():
    assert decode(None) == ""
    assert decode(b"") == ""
