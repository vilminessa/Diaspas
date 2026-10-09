"""Тесты release.py: скачивание и распаковка без сети.

Ключевые регрессии:
- install() раньше падал с AttributeError (ZipInfo.size вместо file_size)
  и оставлял пустую папку - теперь это золотой тест на реальном zip;
- download() обязан замечать обрыв (сверка с Content-Length);
- битый кэш не должен переиспользоваться.
"""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import release  # noqa: E402


def make_zip(tmp_path: Path, content: dict[str, str]) -> Path:
    """Собрать zip: {имя_в_архиве: содержимое}."""
    path = tmp_path / "r.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in content.items():
            zf.writestr(name, text)
    return path


def test_install_extract_real_structure(tmp_path):
    """Реальная структура релиза: подпапка версии + service.bat.

    Ровно тот путь, на котором раньше падало поле ZipInfo.size.
    """
    zip_path = make_zip(tmp_path, {
        "zapret-1.10.3/service.bat": "@echo off\r\n",
        "zapret-1.10.3/general (ALT11).bat": "start winws.exe\r\n",
        "zapret-1.10.3/bin/winws.exe": "MZ fake",
    })
    dest = tmp_path / "install"
    root = release.install(zip_path, dest)
    assert root.name == "zapret-1.10.3"
    assert (root / "service.bat").is_file()
    assert (root / "bin" / "winws.exe").is_file()


def test_install_progress_uses_file_size(tmp_path):
    """Прогресс получает суммарный размер (file_size, не size)."""
    zip_path = make_zip(tmp_path, {
        "x/service.bat": "a" * 1000,
        "x/general.bat": "b" * 500,
    })
    seen = []
    release.install(zip_path, tmp_path / "i",
                    progress=lambda done, total: seen.append((done, total)))
    assert seen, "progress не вызван"
    done, total = seen[-1]
    assert total == 1500, f"суммарный размер: {total}"
    assert done == total


def test_install_bad_zip_removes_cache(tmp_path):
    """Битый архив: ReleaseError + файл удалён (повтор начнётся чистым)."""
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"PK\x03\x04 this is not a real zip file" * 10)
    with pytest.raises(release.ReleaseError, match="повреждён"):
        release.install(bad, tmp_path / "i")
    assert not bad.exists(), "битый кэш не удалён"


def test_install_missing_service_bat(tmp_path):
    zip_path = make_zip(tmp_path, {"x/readme.txt": "no service here"})
    with pytest.raises(release.ReleaseError, match="service.bat"):
        release.install(zip_path, tmp_path / "i")


def test_ensure_license_fetches_or_points_to_source(tmp_path):
    """LICENSE.txt создаётся даже без сети (запасной указатель)."""
    root = tmp_path / "zapret"
    root.mkdir()
    release._ensure_license(root)
    text = (root / "LICENSE.txt").read_text(encoding="utf-8")
    # либо скачанный MIT-текст, либо указатель на первоисточник
    assert ("MIT License" in text) or ("raw.githubusercontent.com" in text)


class _FakeResponse:
    """Минимальный контекст-менеджер для мока urlopen."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def test_download_detects_truncation(tmp_path, monkeypatch):
    """Content-Length=1000, тело 100 байт -> обрыв, не «успех»."""
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    fake = _FakeResponse(b"x" * 100, {"Content-Length": "1000"})
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: fake)

    rel = release.Release(tag="v1", name="v1", zip_url="http://x/v1.zip",
                          published="")
    with pytest.raises(release.ReleaseError, match="оборвано"):
        release.download(rel)
    # недокачанный .part удалён
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / "zapret-v1.zip").exists()


def test_download_rejects_non_zip(tmp_path, monkeypatch):
    """Сервер прислал 200, но не zip -> ошибка, кэш не создаётся."""
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    body = b"<!DOCTYPE html> error page"
    fake = _FakeResponse(body, {"Content-Length": str(len(body))})
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: fake)

    rel = release.Release(tag="v2", name="v2", zip_url="http://x/v2.zip",
                          published="")
    with pytest.raises(release.ReleaseError, match="не является zip"):
        release.download(rel)
    assert not (tmp_path / "zapret-v2.zip").exists()


def test_download_reuses_valid_cache(tmp_path, monkeypatch):
    """Валидный кэш не перекачивается (urlopen не должен вызываться)."""
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    cached = tmp_path / "zapret-v3.zip"
    with zipfile.ZipFile(cached, "w") as zf:
        zf.writestr("a/service.bat", "x")

    def boom(*args, **kwargs):
        raise AssertionError("urlopen вызван при валидном кэше")

    monkeypatch.setattr(release, "urlopen", boom)
    rel = release.Release(tag="v3", name="v3", zip_url="http://x/v3.zip",
                          published="")
    result = release.download(rel)
    assert result == cached


def test_download_recovers_corrupt_cache(tmp_path, monkeypatch):
    """Битый кэш прошлой попытки -> перекачка, а не переиспользование."""
    monkeypatch.setattr(release.paths, "cache_dir", lambda: tmp_path)
    cached = tmp_path / "zapret-v4.zip"
    cached.write_bytes(b"garbage not a zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/service.bat", "x")
    body = buf.getvalue()

    monkeypatch.setattr(release, "urlopen",
                        lambda *a, **k: _FakeResponse(
                            body, {"Content-Length": str(len(body))}))
    rel = release.Release(tag="v4", name="v4", zip_url="http://x/v4.zip",
                          published="")
    result = release.download(rel)
    assert zipfile.is_zipfile(result)
