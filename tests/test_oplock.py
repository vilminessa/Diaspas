"""Серилизация операций и отмена скачивания.

Регрессии: UI блокирует кнопки, но pywebview зовёт api из пула потоков, и
«применить пресет» поверх переключения версии пересоздавал службу в
середине чужой операции. Долгая закачка раньше не прерывалась вовсе.
"""

import io
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import oplock, release  # noqa: E402


def make_release(tmp_path: Path) -> release.Release:
    return release.Release(
        repo="Flowseal/zapret-discord-youtube", tag="1.10.3",
        name="zapret-discord-youtube-1.10.3",
        zip_url="https://example.invalid/x.zip", size=0, published="")


class FakeResponse:
    """Ответ urlopen с ControlledLength: читает кусками и передаёт управление."""

    def __init__(self, chunks, total):
        self._iter = iter(chunks)
        self.headers = {"Content-Length": str(total)}

    def read(self, n):
        try:
            return next(self._iter)
        except StopIteration:
            return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_operation_is_exclusive():
    started = threading.Event()
    release_it = threading.Event()
    errors = []

    def holder():
        with oplock.operation("держу"):
            started.set()
            release_it.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    started.wait(5)
    try:
        with oplock.operation("вторая"):
            pass
    except oplock.BusyError as exc:
        errors.append(str(exc))
    finally:
        release_it.set()
        t.join(5)

    assert errors and "держу" in errors[0]
    # после завершения holder блокировка свободна
    with oplock.operation("после"):
        assert oplock.current() == "после"


def test_cancel_flag_is_reset_between_operations():
    with oplock.operation("первая"):
        assert oplock.cancel() is True
        assert oplock.cancelled() is True
    with oplock.operation("вторая"):
        assert oplock.cancelled() is False


def test_cancel_without_operation_does_nothing():
    assert oplock.cancel() is False


def test_download_is_cancelled_and_partial_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    chunks = [b"x" * (1 << 16) for _ in range(4)]

    def fake_urlopen(request, timeout=None):
        oplock.cancel()          # пользователь нажал «Отменить» в процессе
        return FakeResponse(chunks, sum(len(c) for c in chunks))

    monkeypatch.setattr(release, "urlopen", fake_urlopen)

    with oplock.operation("скачивание релиза"):
        with pytest.raises(oplock.Cancelled):
            release.download(make_release(tmp_path))

    assert list(tmp_path.glob("*.zip.part")) == [], "partial-файл остался"
    assert list(tmp_path.glob("*.zip")) == [], "битый zip попал в кэш"


def test_download_reuses_valid_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    target = tmp_path / "zapret-zapret-discord-youtube-1.10.3.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("service.bat", "@echo off\r\n")
    target.write_bytes(buf.getvalue())

    def boom(request, timeout=None):
        raise AssertionError("сеть не нужна: кэш валиден")

    monkeypatch.setattr(release, "urlopen", boom)
    got = release.download(make_release(tmp_path))
    assert got == target
